"""PisoWiFi Deployer — one-window GUI to provision Orange Pi One PisoWiFi machines.

Two ways to get a board running, both fully automated from this app:

  * Network Deploy — scan the LAN for boards, pick one, set the rates/name/
    password, and it uploads the whole project + runs the installer over SSH,
    then verifies the services came up. (auto line-ending fix + a local
    py_compile check of the app before upload = "auto fix / compile".)

  * SD Card — flash an Armbian image to a microSD, or CLONE a fully-working
    "golden" card: read the whole card to an image, then write that image to
    as many new cards as you like — each one boots ready to use.

The entire PisoWiFi project is bundled inside this .exe, so it is fully
self-contained: nothing else to download.

Run headless self-check (used by the build to verify the exe):  --selftest
"""
import concurrent.futures as futures
import ctypes
import io
import json
import lzma
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback

APP_TITLE = "PisoWiFi Deployer"
APP_VERSION = "1.0"

# Project files uploaded to a board (relative to the payload root).
PAYLOAD_ITEMS = ["app", "network", "systemd", "install.sh", "seal.sh",
                 "README.md", "server"]
TEXT_EXT = {".py", ".html", ".conf", ".json", ".service", ".sh", ".md", ".cfg", ".txt"}


# ---------------------------------------------------------------------------
# bundled-resource location (works both as .py in dev and inside PyInstaller)
# ---------------------------------------------------------------------------

def payload_root():
    """Directory that contains app/, network/, install.sh, ..."""
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "payload")  # noqa: SLF001
        if os.path.isdir(base):
            return base
        return sys._MEIPASS  # noqa: SLF001
    # dev: this file lives in <project>/deployer/, payload is the project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def payload_ok():
    root = payload_root()
    return all(os.path.exists(os.path.join(root, i)) for i in PAYLOAD_ITEMS)


# ---------------------------------------------------------------------------
# network scan
# ---------------------------------------------------------------------------

def local_ipv4s():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return {ip for ip in ips if not ip.startswith("127.")}


def local_subnets():
    return sorted({ip.rsplit(".", 1)[0] for ip in local_ipv4s()})


def _port_open(ip, port=22, timeout=1.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(prefix, progress=None, stop=None):
    """Return [{'ip','host','ssh'}] for hosts with SSH (:22) open in prefix.0/24.

    1.5 s timeout: a cold /24 sweep triggers ARP resolution for hundreds of
    dead hosts at once, which delays the SYN to live hosts — a tighter timeout
    silently misses the board on the first scan."""
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    found = []
    done = 0
    with futures.ThreadPoolExecutor(max_workers=64) as ex:
        fut = {ex.submit(_port_open, ip, 22, 1.5): ip for ip in hosts}
        for f in futures.as_completed(fut):
            if stop and stop():
                break
            done += 1
            if progress:
                progress(done, len(hosts))
            if f.result():
                ip = fut[f]
                try:
                    host = socket.gethostbyaddr(ip)[0]
                except OSError:
                    host = ""
                found.append({"ip": ip, "host": host, "ssh": True})
    found.sort(key=lambda h: tuple(int(x) for x in h["ip"].split(".")))
    return found


# ---------------------------------------------------------------------------
# SSH deploy (paramiko)
# ---------------------------------------------------------------------------

class Deployer:
    def __init__(self, log):
        self.log = log
        self._client = None

    def connect(self, host, user, password=None, key_path=None, timeout=15):
        import paramiko
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": host, "username": user, "timeout": timeout,
              "allow_agent": False, "look_for_keys": False}
        if key_path:
            kw["key_filename"] = key_path
            kw["look_for_keys"] = True
        if password:
            kw["password"] = password
        # also try the user's default key if nothing explicit was given
        if not password and not key_path:
            kw["look_for_keys"] = True
            kw["allow_agent"] = True
        self._client.connect(**kw)
        return self._client

    def run(self, cmd, echo=True):
        """Run a command, stream stdout+stderr to the log, return exit status."""
        if echo:
            self.log(f"$ {cmd}")
        stdin, stdout, stderr = self._client.exec_command(cmd, get_pty=True)
        for line in iter(stdout.readline, ""):
            if line:
                self.log(line.rstrip("\n"))
        return stdout.channel.recv_exit_status()

    def probe_model(self):
        _in, out, _err = self._client.exec_command(
            "cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'")
        return out.read().decode(errors="replace").strip()

    def upload_tree(self, remote_base):
        """SFTP the bundled payload to remote_base, fixing CRLF on text files."""
        sftp = self._client.open_sftp()
        root = payload_root()
        self._mkdir(sftp, remote_base)
        count = 0
        for item in PAYLOAD_ITEMS:
            local = os.path.join(root, item)
            if os.path.isdir(local):
                count += self._put_dir(sftp, local, f"{remote_base}/{item}")
            elif os.path.isfile(local):
                self._put_file(sftp, local, f"{remote_base}/{item}")
                count += 1
        sftp.close()
        return count

    def _put_dir(self, sftp, local_dir, remote_dir):
        self._mkdir(sftp, remote_dir)
        count = 0
        for name in sorted(os.listdir(local_dir)):
            lp = os.path.join(local_dir, name)
            rp = f"{remote_dir}/{name}"
            if os.path.isdir(lp):
                if name in ("__pycache__", ".git"):
                    continue
                count += self._put_dir(sftp, lp, rp)
            elif name.endswith((".pyc", ".db")):
                continue
            else:
                self._put_file(sftp, lp, rp)
                count += 1
        return count

    def _put_file(self, sftp, local, remote):
        ext = os.path.splitext(local)[1].lower()
        if ext in TEXT_EXT:
            with open(local, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")  # auto line-ending fix
            with sftp.open(remote, "wb") as rf:
                rf.write(data)
        else:
            sftp.put(local, remote)
        self.log(f"  -> {remote}")

    def _mkdir(self, sftp, path):
        try:
            sftp.stat(path)
        except IOError:
            sftp.mkdir(path)

    def push_config(self, remote_path, cfg):
        sftp = self._client.open_sftp()
        with sftp.open(remote_path, "wb") as rf:
            rf.write(json.dumps(cfg, indent=2).encode())
        # holds the admin password and the remote dashboard key — root only
        try:
            sftp.chmod(remote_path, 0o600)
        except IOError as e:
            self.log(f"  (warn) could not chmod {remote_path}: {e}")
        sftp.close()
        self.log(f"  → wrote {remote_path} (mode 600)")

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


def local_compile_check(log):
    """py_compile the bundled app before upload — catches syntax errors early."""
    import py_compile
    app_dir = os.path.join(payload_root(), "app")
    ok = True
    for name in sorted(os.listdir(app_dir)):
        if name.endswith(".py"):
            try:
                py_compile.compile(os.path.join(app_dir, name), doraise=True)
            except py_compile.PyCompileError as e:
                ok = False
                log(f"  COMPILE ERROR in {name}: {e}")
    log("  app compiles cleanly" if ok else "  app has compile errors — aborting")
    return ok


def build_config(fields):
    """Merge GUI fields onto the shipped config.json defaults."""
    with open(os.path.join(payload_root(), "app", "config.json")) as f:
        cfg = json.load(f)
    cfg.update(fields)
    return cfg


# ---------------------------------------------------------------------------
# raw disk I/O (Windows physical drives) for SD-card flash / clone
# ---------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
INVALID_HANDLE = ctypes.c_void_p(-1).value
CHUNK = 1024 * 1024  # 1 MiB, multiple of 512


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p]
    k.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    return k


def list_disks():
    """Physical disks via PowerShell. Returns list of dicts; system disk flagged."""
    ps = (
        "Get-Disk | Select-Object Number,FriendlyName,Size,BusType,"
        "IsSystem,IsBoot | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        data = json.loads(out) if out.strip() else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    disks = []
    for d in data:
        disks.append({
            "number": d.get("Number"),
            "name": d.get("FriendlyName") or "?",
            "size": int(d.get("Size") or 0),
            "bus": str(d.get("BusType") or ""),
            "system": bool(d.get("IsSystem") or d.get("IsBoot")),
            "removable": str(d.get("BusType") or "") in ("USB", "SD", "MMC"),
        })
    return disks


def disk_drive_letters(number):
    ps = (f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | "
          "Where-Object DriveLetter | Select-Object -ExpandProperty DriveLetter")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


class RawDisk:
    """Locks + dismounts a physical disk's volumes, then raw read/writes it."""

    def __init__(self, number, log):
        self.number = number
        self.log = log
        self.k = _k32()
        self._vol_handles = []

    def _open(self, path, access):
        h = self.k.CreateFileW(path, access,
                               FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                               OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE or h is None:
            raise OSError(f"cannot open {path} (err {ctypes.get_last_error()}) — "
                          "run the app as Administrator")
        return h

    def lock(self):
        for letter in disk_drive_letters(self.number):
            try:
                h = self._open(f"\\\\.\\{letter}:", GENERIC_READ | GENERIC_WRITE)
            except OSError as e:
                self.log(f"  (warn) {e}")
                continue
            ret = ctypes.c_uint32(0)
            self.k.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0,
                                   ctypes.byref(ret), None)
            self.k.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
                                   ctypes.byref(ret), None)
            self._vol_handles.append(h)
            self.log(f"  dismounted volume {letter}:")

    def write(self, stream, total, progress, stop=None):
        h = self._open(f"\\\\.\\PhysicalDrive{self.number}",
                       GENERIC_READ | GENERIC_WRITE)
        written = 0
        t0 = time.time()
        try:
            while True:
                if stop and stop():
                    raise RuntimeError("cancelled")
                buf = stream.read(CHUNK)
                if not buf:
                    break
                if len(buf) % 512:
                    buf += b"\x00" * (512 - len(buf) % 512)  # sector-align tail
                n = ctypes.c_uint32(0)
                cbuf = ctypes.create_string_buffer(buf, len(buf))
                if not self.k.WriteFile(h, cbuf, len(buf), ctypes.byref(n), None):
                    raise OSError(f"write failed at {written} "
                                  f"(err {ctypes.get_last_error()})")
                written += n.value
                progress(written, total, time.time() - t0)
            self.k.FlushFileBuffers(h)
            ret = ctypes.c_uint32(0)
            self.k.DeviceIoControl(h, IOCTL_DISK_UPDATE_PROPERTIES, None, 0,
                                   None, 0, ctypes.byref(ret), None)
        finally:
            self.k.CloseHandle(ctypes.c_void_p(h))
        return written

    def read_to(self, out_stream, total, progress, stop=None):
        h = self._open(f"\\\\.\\PhysicalDrive{self.number}", GENERIC_READ)
        read = 0
        t0 = time.time()
        try:
            while read < total:
                if stop and stop():
                    raise RuntimeError("cancelled")
                want = min(CHUNK, total - read)
                if want % 512:
                    want += 512 - want % 512
                cbuf = ctypes.create_string_buffer(want)
                n = ctypes.c_uint32(0)
                if not self.k.ReadFile(h, cbuf, want, ctypes.byref(n), None):
                    raise OSError(f"read failed at {read} "
                                  f"(err {ctypes.get_last_error()})")
                if n.value == 0:
                    break
                out_stream.write(cbuf.raw[:n.value])
                read += n.value
                progress(read, total, time.time() - t0)
        finally:
            self.k.CloseHandle(ctypes.c_void_p(h))
        return read

    def close(self):
        for h in self._vol_handles:
            self.k.CloseHandle(ctypes.c_void_p(h))
        self._vol_handles = []


def open_image_stream(path):
    """Return (stream, uncompressed_size_estimate). Supports .img and .img.xz."""
    if path.lower().endswith(".xz"):
        # size unknown until decompressed; estimate ~2.7x the compressed size
        est = int(os.path.getsize(path) * 2.7)
        return lzma.open(path, "rb"), est
    return open(path, "rb"), os.path.getsize(path)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# self-test (headless) — used by the build to verify the exe works
# ---------------------------------------------------------------------------

def selftest():
    print(f"{APP_TITLE} {APP_VERSION} self-test")
    print("payload present:", payload_ok(), "at", payload_root())
    for i in PAYLOAD_ITEMS:
        p = os.path.join(payload_root(), i)
        print(f"  {'ok ' if os.path.exists(p) else 'MISSING'} {i}")
    print("local subnets:", local_subnets() or "(none detected)")
    try:
        import paramiko  # noqa: F401
        print("paramiko: ok")
    except ImportError as e:
        print("paramiko: MISSING", e)
    disks = list_disks()
    print(f"disks visible: {len(disks)}")
    for d in disks:
        tag = "SYSTEM" if d["system"] else ("removable" if d["removable"] else "fixed")
        print(f"  disk {d['number']}: {d['name']} {human(d['size'])} "
              f"[{d['bus']}] {tag}")
    ok = payload_ok()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # Beach theme, the same palette as the portal and admin: sea blues sampled
    # from the resort photo, sunset accents from the logo. The deployer is the
    # first thing an owner opens and the admin is what they use afterwards --
    # they should not look like two different products.
    BG = "#062033"      # deep sea
    CARD = "#0c3550"    # card / field
    CARD_HI = "#10425f"  # hover
    LINE = "#1a5a7d"
    ACCENT = "#ffc233"  # sunset yellow
    ACCENT_HI = "#ffb020"
    TXT = "#eaf6ff"
    MUTED = "#9fc6dd"
    FIELD = "#082a41"

    root = tk.Tk()
    root.title(f"{APP_TITLE} {APP_VERSION}")
    root.geometry("880x660")
    root.configure(bg=BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=TXT, fieldbackground=CARD)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=CARD, foreground=TXT, padding=(14, 7))
    style.map("TNotebook.Tab", background=[("selected", ACCENT)],
              foreground=[("selected", "#111111")])
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TXT)
    style.configure("Card.TLabel", background=CARD, foreground=TXT)
    style.configure("TButton", background=CARD, foreground=TXT, padding=6)
    style.map("TButton", background=[("active", CARD_HI)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#111111")
    style.map("Accent.TButton", background=[("active", ACCENT_HI)])
    style.configure("Danger.TButton", background="#c2410c", foreground="white")
    style.map("Danger.TButton", background=[("active", "#9a3412")])
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TXT,
                    rowheight=24)
    style.configure("Treeview.Heading", background=LINE, foreground=TXT)
    style.configure("TEntry", fieldbackground=FIELD, foreground=TXT,
                bordercolor=LINE, insertcolor=TXT)
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                troughcolor=FIELD, bordercolor=LINE)

    msg_q = queue.Queue()

    def log(line=""):
        msg_q.put(("log", str(line)))

    def set_status(text):
        msg_q.put(("status", text))

    def set_progress(frac, text=None):
        msg_q.put(("progress", (frac, text)))

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))

    # ---- shared log panel at the bottom ----
    logframe = ttk.Frame(root)
    logframe.pack(fill="both", expand=False, padx=10, pady=(0, 6))
    ttk.Label(logframe, text="Log").pack(anchor="w")
    logbox = tk.Text(logframe, height=11, bg=FIELD, fg=MUTED,
                     insertbackground=TXT, relief="flat", wrap="none",
                     font=("Consolas", 9))
    logbox.pack(fill="both", expand=True)
    pbar = ttk.Progressbar(root, mode="determinate", maximum=1000)
    pbar.pack(fill="x", padx=10)
    statusbar = ttk.Label(root, text="Ready", anchor="w")
    statusbar.pack(fill="x", padx=10, pady=(2, 8))

    def pump():
        try:
            while True:
                kind, payload = msg_q.get_nowait()
                if kind == "log":
                    logbox.insert("end", payload + "\n")
                    logbox.see("end")
                elif kind == "status":
                    statusbar.config(text=payload)
                elif kind == "progress":
                    frac, text = payload
                    pbar["value"] = max(0, min(1000, int(frac * 1000)))
                    if text:
                        statusbar.config(text=text)
        except queue.Empty:
            pass
        root.after(80, pump)

    def worker(fn):
        threading.Thread(target=_guarded(fn), daemon=True).start()

    def _guarded(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: {e}")
                log(traceback.format_exc())
                set_status(f"Error: {e}")
        return run

    # =====================================================================
    # TAB 1 — Network Deploy
    # =====================================================================
    t1 = ttk.Frame(nb)
    nb.add(t1, text="  Network Deploy  ")

    # scan row
    top = ttk.Frame(t1); top.pack(fill="x", pady=6)
    ttk.Label(top, text="Subnet:").pack(side="left")
    subnet_var = tk.StringVar(value=(local_subnets() or ["192.168.1"])[0] + ".0/24")
    subnet_combo = ttk.Combobox(top, textvariable=subnet_var, width=18,
                                values=[s + ".0/24" for s in local_subnets()])
    subnet_combo.pack(side="left", padx=6)

    tree = ttk.Treeview(t1, columns=("ip", "host", "model"), show="headings", height=7)
    for c, w, txt in (("ip", 130, "IP address"), ("host", 240, "Hostname"),
                      ("model", 300, "Board (after Identify/Deploy)")):
        tree.heading(c, text=txt); tree.column(c, width=w)
    tree.pack(fill="x", pady=4)

    stop_flag = {"stop": False}

    def do_scan():
        stop_flag["stop"] = False
        for r in tree.get_children():
            tree.delete(r)
        prefix = subnet_var.get().split("/")[0].rsplit(".", 1)[0]
        set_status(f"Scanning {prefix}.0/24 for SSH hosts…")
        log(f"Scanning {prefix}.0/24 …")

        def prog(done, total):
            set_progress(done / total, f"Scanning… {done}/{total}")
        hosts = scan_subnet(prefix, progress=prog, stop=lambda: stop_flag["stop"])
        for h in hosts:
            tree.insert("", "end", values=(h["ip"], h["host"] or "—", ""))
        set_progress(0)
        set_status(f"Found {len(hosts)} SSH host(s). Select one, set options, Deploy.")
        log(f"Found {len(hosts)} host(s) with port 22 open.")

    ttk.Button(top, text="Scan network", command=lambda: worker(do_scan)).pack(side="left")
    ttk.Button(top, text="Stop", command=lambda: stop_flag.__setitem__("stop", True)
               ).pack(side="left", padx=4)
    ttk.Label(top, text="  or IP:").pack(side="left")
    manual_ip = ttk.Entry(top, width=15); manual_ip.pack(side="left")

    def add_manual():
        ip = manual_ip.get().strip()
        if ip:
            tree.insert("", "end", values=(ip, "(manual)", ""))
    ttk.Button(top, text="Add", command=add_manual).pack(side="left", padx=4)

    # credentials + settings
    opts = ttk.Frame(t1); opts.pack(fill="x", pady=8)

    def field(parent, label, default="", show=None, width=22, r=0, c=0):
        ttk.Label(parent, text=label).grid(row=r, column=c, sticky="w", padx=4, pady=3)
        v = tk.StringVar(value=default)
        e = ttk.Entry(parent, textvariable=v, width=width, show=show)
        e.grid(row=r, column=c + 1, sticky="w", padx=4, pady=3)
        return v

    ssh_user = field(opts, "SSH user", "root", r=0, c=0)
    ssh_pass = field(opts, "SSH password", "", show="•", r=0, c=2)
    ttk.Label(opts, text="(blank = use your SSH key)").grid(row=0, column=4, sticky="w")

    name_var = field(opts, "Hotspot name", "AJ PisoWiFi", r=1, c=0)
    admin_pw = field(opts, "Admin password", "changeme", r=1, c=2)
    mpp_var = field(opts, "Minutes per ₱1", "20", width=10, r=2, c=0)
    denom_var = field(opts, "Denominations", '{"1":1,"5":5,"10":10,"20":20}',
                      width=30, r=2, c=2)
    remote_url = field(opts, "Remote URL", "", width=30, r=3, c=0)
    remote_key = field(opts, "Remote key", "", show="•", width=22, r=3, c=2)

    seed_cfg = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts, text="Write these settings to the board "
                    "(overwrite /etc/pisowifi/config.json)",
                    variable=seed_cfg).grid(row=4, column=0, columnspan=4,
                                            sticky="w", padx=4, pady=4)

    def selected_ip():
        sel = tree.selection()
        if not sel:
            return None
        return tree.item(sel[0])["values"][0]

    def gather_cfg():
        fields = {
            "hotspot_name": name_var.get().strip() or "PisoWiFi",
            "admin_password": admin_pw.get() or "changeme",
            "minutes_per_peso": int(mpp_var.get() or 20),
        }
        try:
            fields["bonus_tiers"] = {str(int(k)): int(v) for k, v in
                                     json.loads(denom_var.get()).items()}
        except Exception:
            raise ValueError("Denominations must be JSON like {\"5\":120}")
        if remote_url.get().strip():
            fields["remote_enabled"] = True
            fields["remote_url"] = remote_url.get().strip()
            fields["remote_key"] = remote_key.get().strip()
        return build_config(fields)

    def do_identify():
        ip = selected_ip()
        if not ip:
            messagebox.showinfo(APP_TITLE, "Select a host in the list first.")
            return

        def run():
            dep = Deployer(log)
            set_status(f"Connecting to {ip}…")
            dep.connect(ip, ssh_user.get().strip(),
                        ssh_pass.get() or None)
            model = dep.probe_model() or "(unknown)"
            log(f"{ip} → {model}")
            for r in tree.get_children():
                if str(tree.item(r)['values'][0]) == ip:
                    vals = list(tree.item(r)["values"]); vals[2] = model
                    tree.item(r, values=vals)
            dep.close()
            set_status(f"{ip} is: {model}")
        worker(run)

    def do_deploy():
        ip = selected_ip()
        if not ip:
            messagebox.showinfo(APP_TITLE, "Select a host in the list first.")
            return
        try:
            cfg = gather_cfg()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e)); return
        if not messagebox.askyesno(
                APP_TITLE, f"Deploy PisoWiFi to {ip}?\n\n"
                "This uploads the software and runs the installer "
                "(auto-detecting the network interfaces)."):
            return

        def run():
            set_progress(0)
            log("=" * 60)
            log("Pre-flight: compiling the app locally…")
            if not local_compile_check(log):
                set_status("Aborted — local compile errors."); return
            dep = Deployer(log)
            set_status(f"Connecting to {ip}…")
            dep.connect(ip, ssh_user.get().strip(), ssh_pass.get() or None)
            model = dep.probe_model()
            log(f"Connected. Board: {model or 'unknown'}")
            if model and "orange pi" not in model.lower():
                log(f"  NOTE: '{model}' is not an Orange Pi — continuing anyway.")
            log("Uploading project (auto-fixing line endings)…")
            dep.run("rm -rf /root/pisowifi.new && mkdir -p /root/pisowifi.new", echo=False)
            n = dep.upload_tree("/root/pisowifi.new")
            log(f"Uploaded {n} files.")
            dep.run("rm -rf /root/pisowifi && mv /root/pisowifi.new /root/pisowifi "
                    "&& find /root/pisowifi -type f -exec sed -i 's/\\r$//' {} +",
                    echo=False)
            set_status("Running installer on the board…")
            rc = dep.run("cd /root/pisowifi && bash install.sh --yes")
            if rc != 0:
                set_status(f"Installer exited {rc} — see log."); dep.close(); return
            if seed_cfg.get():
                log("Writing your settings to /etc/pisowifi/config.json…")
                dep.push_config("/etc/pisowifi/config.json", cfg)
                dep.run("systemctl restart pisowifi", echo=False)
            log("Verifying services…")
            dep.run("systemctl is-active pisowifi pisowifi-detect nftables dnsmasq "
                    "| tr '\\n' ' '; echo")
            # retry: Flask needs a moment to rebind the port after the restart
            dep.run("for i in $(seq 1 10); do "
                    "c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/); "
                    "[ \"$c\" = 200 ] && break; sleep 1; done; "
                    "echo \"portal HTTP $c\"", echo=False)
            dep.close()
            set_progress(1.0)
            set_status(f"Done. Admin: http://{ip}:8080/admin")
            log("DEPLOY COMPLETE.")
            log(f"Portal http://{ip}:8080/   Admin http://{ip}:8080/admin")
        worker(run)

    def do_seal():
        """Turn a finished machine into a master image.

        Everything that makes the box *this* box is removed, so cards written
        from the resulting image boot ready to use on any board with no SSH:
        each one re-detects its own hardware at boot and asks for an admin
        password through the portal."""
        ip = selected_ip()
        if not ip:
            return
        if not messagebox.askyesno(
                APP_TITLE,
                f"Seal {ip} for imaging?\n\n"
                "This WIPES the sales database, admin password, SSH host keys "
                "and logs on that machine, then powers it off.\n\n"
                "Do this only on a machine you are turning into a master card. "
                "Afterwards: pull its SD card and read it with the SD Card tab.",
                icon="warning"):
            return

        def run():
            set_progress(0)
            log("=" * 60)
            dep = Deployer(log)
            set_status(f"Connecting to {ip}…")
            dep.connect(ip, ssh_user.get().strip(), ssh_pass.get() or None)
            log("Sealing — the board powers itself off when this finishes.")
            # The board cuts the connection mid-command by design, so a
            # non-zero status here is expected and not an error.
            try:
                dep.run("cd /root/pisowifi && bash seal.sh --yes")
            except Exception as e:
                log(f"(connection closed during poweroff: {e})")
            dep.close()
            set_progress(1.0)
            set_status("Sealed. Pull the card and read it on the SD Card tab.")
            log("SEALED. Next: power off, pull the SD card, then")
            log("SD Card tab -> READ / CLONE to save it as your master .img.")
        worker(run)

    btns = ttk.Frame(t1); btns.pack(fill="x", pady=6)
    ttk.Button(btns, text="Identify board", command=do_identify).pack(side="left", padx=4)
    ttk.Button(btns, text="Deploy PisoWiFi ▶", style="Accent.TButton",
               command=do_deploy).pack(side="left", padx=4)
    ttk.Button(btns, text="Seal for imaging…", command=do_seal).pack(side="left", padx=16)

    # =====================================================================
    # TAB 2 — SD Card
    # =====================================================================
    t2 = ttk.Frame(nb)
    nb.add(t2, text="  SD Card  ")

    warn = ("⚠ Writing/cloning ERASES the target card. Only removable (USB/SD) "
            "disks are shown by default. Run this app as Administrator. "
            "Double-check the disk number and size before you confirm.")
    ttk.Label(t2, text=warn, wraplength=820, foreground="#fca5a5").pack(
        anchor="w", pady=6)

    dtop = ttk.Frame(t2); dtop.pack(fill="x")
    show_all = tk.BooleanVar(value=False)
    disk_tree = ttk.Treeview(t2, columns=("n", "name", "size", "bus", "kind"),
                             show="headings", height=5)
    for c, w, txt in (("n", 50, "Disk"), ("name", 300, "Model"),
                      ("size", 100, "Size"), ("bus", 90, "Bus"), ("kind", 110, "Type")):
        disk_tree.heading(c, text=txt); disk_tree.column(c, width=w)
    disk_tree.pack(fill="x", pady=4)

    def refresh_disks():
        for r in disk_tree.get_children():
            disk_tree.delete(r)
        for d in list_disks():
            if d["system"]:
                continue
            if not show_all.get() and not d["removable"]:
                continue
            kind = "removable" if d["removable"] else "FIXED"
            disk_tree.insert("", "end", values=(
                d["number"], d["name"], human(d["size"]), d["bus"], kind))
        set_status("Disk list refreshed.")

    ttk.Button(dtop, text="Refresh disks", command=refresh_disks).pack(side="left")
    ttk.Checkbutton(dtop, text="show fixed disks too (dangerous)",
                    variable=show_all, command=refresh_disks).pack(side="left", padx=8)

    def selected_disk():
        sel = disk_tree.selection()
        if not sel:
            return None
        v = disk_tree.item(sel[0])["values"]
        return {"number": int(v[0]), "name": v[1], "size": v[2]}

    def confirm_erase(disk, verb):
        return messagebox.askyesno(
            APP_TITLE,
            f"{verb} disk {disk['number']} — {disk['name']} ({disk['size']})?\n\n"
            "ALL DATA ON THIS DISK WILL BE DESTROYED.\n\nContinue?",
            icon="warning", default="no")

    # --- flash ---
    fl = ttk.LabelFrame(t2, text="Flash an image to the card")
    fl.pack(fill="x", pady=8)
    img_var = tk.StringVar()
    ttk.Entry(fl, textvariable=img_var, width=70).pack(side="left", padx=6, pady=6)

    def pick_img():
        p = filedialog.askopenfilename(
            title="Select Armbian / PisoWiFi image",
            filetypes=[("Disk images", "*.img *.img.xz *.xz *.iso"), ("All", "*.*")])
        if p:
            img_var.set(p)
    ttk.Button(fl, text="Browse…", command=pick_img).pack(side="left")

    def do_flash():
        disk = selected_disk()
        if not disk:
            messagebox.showinfo(APP_TITLE, "Select a target disk first."); return
        img = img_var.get().strip()
        if not img or not os.path.isfile(img):
            messagebox.showerror(APP_TITLE, "Pick a valid image file."); return
        if not confirm_erase(disk, "FLASH"):
            return

        def run():
            set_progress(0)
            log("=" * 60)
            log(f"Flashing {os.path.basename(img)} → disk {disk['number']}")
            stream, total = open_image_stream(img)
            rd = RawDisk(disk["number"], log)
            rd.lock()

            def prog(done, tot, secs):
                rate = done / secs / 1e6 if secs else 0
                set_progress(done / tot if tot else 0,
                             f"Flashing… {human(done)} / ~{human(tot)}  ({rate:.1f} MB/s)")
            try:
                written = rd.write(stream, total, prog)
            finally:
                stream.close(); rd.close()
            set_progress(1.0)
            log(f"Wrote {human(written)}. Flush complete — safe to remove.")
            set_status("Flash complete. Card is ready to boot.")
        worker(run)

    ttk.Button(fl, text="FLASH ▶", style="Danger.TButton",
               command=do_flash).pack(side="left", padx=8)

    # --- clone ---
    cl = ttk.LabelFrame(t2, text="Clone a golden card (read a working card → image, "
                                 "then flash it to new cards)")
    cl.pack(fill="x", pady=8)
    clone_out = tk.StringVar()
    ttk.Entry(cl, textvariable=clone_out, width=70).pack(side="left", padx=6, pady=6)

    def pick_out():
        p = filedialog.asksaveasfilename(
            title="Save card image as", defaultextension=".img.xz",
            filetypes=[("Compressed image", "*.img.xz"), ("Raw image", "*.img")])
        if p:
            clone_out.set(p)
    ttk.Button(cl, text="Save as…", command=pick_out).pack(side="left")

    def do_clone():
        disk = selected_disk()
        if not disk:
            messagebox.showinfo(APP_TITLE, "Select the SOURCE card to read."); return
        out = clone_out.get().strip()
        if not out:
            messagebox.showerror(APP_TITLE, "Choose an output file."); return
        raw = [d for d in list_disks() if d["number"] == disk["number"]]
        total = raw[0]["size"] if raw else 0
        if not total:
            messagebox.showerror(APP_TITLE, "Could not read disk size."); return
        if not messagebox.askyesno(
                APP_TITLE, f"Read ALL {human(total)} from disk {disk['number']} "
                f"({disk['name']}) into:\n{out}\n\nThis can take 10–30 min."):
            return

        def run():
            set_progress(0)
            log("=" * 60)
            log(f"Cloning disk {disk['number']} ({human(total)}) → {out}")
            rd = RawDisk(disk["number"], log)
            comp = out.lower().endswith(".xz")
            fout = lzma.open(out, "wb", preset=1) if comp else open(out, "wb")

            def prog(done, tot, secs):
                rate = done / secs / 1e6 if secs else 0
                set_progress(done / tot if tot else 0,
                             f"Reading… {human(done)} / {human(tot)}  ({rate:.1f} MB/s)")
            try:
                read = rd.read_to(fout, total, prog)
            finally:
                fout.close(); rd.close()
            set_progress(1.0)
            log(f"Read {human(read)} → {out}")
            set_status("Clone complete. Flash this image to new cards.")
        worker(run)

    ttk.Button(cl, text="READ / CLONE ▶", command=do_clone).pack(side="left", padx=8)

    # startup
    log(f"{APP_TITLE} {APP_VERSION}")
    log(f"Bundled project: {'OK' if payload_ok() else 'MISSING'} ({payload_root()})")
    if not payload_ok():
        log("WARNING: project payload not found — network deploy will fail.")
    admin = False
    try:
        admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        pass
    log(f"Administrator: {'yes' if admin else 'NO — SD card writing needs admin'}")
    refresh_disks()
    pump()
    root.mainloop()


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if sys.platform != "win32":
        print("This GUI targets Windows. Running self-test instead.")
        return selftest()
    launch_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
