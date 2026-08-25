"""Per-client bandwidth shaping via tc (HTB) + an ifb device.

Download (internet -> client) is egress on the LAN interface and is shaped
directly. Upload (client -> internet) is ingress on the LAN interface, which
can't be shaped directly, so LAN ingress is redirected to `ifb0` and shaped
there. Each client gets an HTB class + a u32 filter matched on its IP; the
class id AND the filter priority are both derived from the IP's last two
octets, so a client's rule can be removed deterministically with no bookkeeping.

No-ops automatically when `tc` is unavailable (dev PC / mock), so the rest of
the app is hardware-agnostic. All settings come from the caller (main.py maps
speed-profile names -> up/down rate strings like "5mbit").
"""
import ipaddress
import shutil
import subprocess
import sys

IFB = "ifb0"
ROOT_RATE = "1000mbit"          # ceiling for unclassified / default traffic
_LAN = None
_GAMING = False                 # CAKE diffserv4 (prioritize game/VoIP) vs besteffort
HAVE_TC = sys.platform.startswith("linux") and shutil.which("tc") is not None


def configure(gaming=False):
    """Set QoS options (called from main at boot/reconcile)."""
    global _GAMING
    _GAMING = bool(gaming)


def _cake_opts():
    # diffserv4 honors DSCP so latency-sensitive traffic (games/VoIP) beats bulk;
    # ack-filter thins bulk ACKs. Otherwise flat best-effort fairness.
    return ["diffserv4", "ack-filter"] if _GAMING else ["besteffort"]


def _run(args, check=False):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _tc(*args, check=False):
    return _run(["tc", *args], check=check)


def _minor(ip):
    """A per-client 16-bit id from the last two octets of the IP (unique within
    a /16 — fine for the 10.0.0.0/19 or /24 client LAN). Used as both the HTB
    class minor and the filter priority. Never 0."""
    o = ipaddress.ip_address(ip).packed
    return ((o[2] << 8) | o[3]) or 1


# ---- lifecycle ----

def setup(lan_if):
    """Idempotently arm HTB on the LAN egress (download) and on ifb0 (upload,
    fed by LAN ingress). Safe to call on every boot."""
    global _LAN
    _LAN = lan_if
    if not HAVE_TC:
        return
    # tear down any previous state
    _tc("qdisc", "del", "dev", lan_if, "root")
    _tc("qdisc", "del", "dev", lan_if, "ingress")
    _run(["ip", "link", "del", IFB])

    # download shaping: HTB egress on the LAN interface
    _tc("qdisc", "add", "dev", lan_if, "root", "handle", "1:", "htb", "default", "1")
    _tc("class", "add", "dev", lan_if, "parent", "1:", "classid", "1:1",
        "htb", "rate", ROOT_RATE)

    # ifb for upload shaping
    _run(["ip", "link", "add", IFB, "type", "ifb"])
    _run(["ip", "link", "set", IFB, "up"])
    _tc("qdisc", "add", "dev", lan_if, "handle", "ffff:", "ingress")
    _tc("filter", "add", "dev", lan_if, "parent", "ffff:", "protocol", "all",
        "u32", "match", "u32", "0", "0",
        "action", "mirred", "egress", "redirect", "dev", IFB)
    _tc("qdisc", "add", "dev", IFB, "root", "handle", "1:", "htb", "default", "1")
    _tc("class", "add", "dev", IFB, "parent", "1:", "classid", "1:1",
        "htb", "rate", ROOT_RATE)


def _root_ready():
    """True when the HTB root is armed on both shaping devices."""
    if not HAVE_TC or not _LAN:
        return False
    for dev in (_LAN, IFB):
        if "qdisc htb 1:" not in _tc("qdisc", "show", "dev", dev).stdout:
            return False
    return True


def ensure_setup():
    """Re-arm the root qdiscs if they have disappeared. Returns True if it did.

    tc state belongs to the interface, so anything that recreates the LAN
    device takes the entire shaping tree with it -- unplugging the USB-ethernet
    dongle is enough, and on this project the dongle gets swapped. limit() then
    adds classes under a parent that no longer exists, and because these tc
    calls are not checked, every one fails quietly: paying customers run
    unshaped and nothing reports it until someone restarts the service.

    Only rebuilds when the root is genuinely missing, since setup() tears down
    every existing client class.
    """
    if _LAN and not _root_ready():
        setup(_LAN)
        return True
    return False


def limit(ip, up, down):
    """Cap `ip` to `up`/`down` (rate strings, e.g. '5mbit'). Idempotent —
    replaces any existing cap for that IP."""
    if not HAVE_TC or not _LAN:
        return
    ensure_setup()
    up = up or ROOT_RATE
    down = down or ROOT_RATE
    m = _minor(ip)
    cid = f"1:{m:x}"
    hnd = f"{m:x}:"
    # download on LAN egress (match dst ip); HTB sets the rate, a CAKE leaf
    # kills bufferbloat + prioritizes per _cake_opts().
    _tc("class", "replace", "dev", _LAN, "parent", "1:", "classid", cid,
        "htb", "rate", down, "ceil", down)
    _tc("qdisc", "replace", "dev", _LAN, "parent", cid, "handle", hnd,
        "cake", "bandwidth", down, *_cake_opts())
    _tc("filter", "replace", "dev", _LAN, "protocol", "ip", "parent", "1:",
        "prio", str(m), "u32", "match", "ip", "dst", f"{ip}/32", "flowid", cid)
    # upload on ifb (match src ip)
    _tc("class", "replace", "dev", IFB, "parent", "1:", "classid", cid,
        "htb", "rate", up, "ceil", up)
    _tc("qdisc", "replace", "dev", IFB, "parent", cid, "handle", hnd,
        "cake", "bandwidth", up, *_cake_opts())
    _tc("filter", "replace", "dev", IFB, "protocol", "ip", "parent", "1:",
        "prio", str(m), "u32", "match", "ip", "src", f"{ip}/32", "flowid", cid)


def unlimit(ip):
    """Remove any cap on `ip` (return it to full speed). Safe if none exists."""
    if not HAVE_TC or not _LAN:
        return
    m = _minor(ip)
    cid = f"1:{m:x}"
    for dev in (_LAN, IFB):
        _tc("filter", "del", "dev", dev, "parent", "1:", "prio", str(m))
        _tc("class", "del", "dev", dev, "classid", cid)


def unlimit_minor(minor_hex):
    """Remove a cap identified by its class minor (hex string from active_ips())
    — used by the reconcile loop to clear caps for clients whose time expired."""
    if not HAVE_TC or not _LAN:
        return
    cid = f"1:{minor_hex}"
    try:
        prio = str(int(minor_hex, 16))
    except ValueError:
        return
    for dev in (_LAN, IFB):
        _tc("filter", "del", "dev", dev, "parent", "1:", "prio", prio)
        _tc("class", "del", "dev", dev, "classid", cid)


def active_ips():
    """IPs that currently have a shaping class (parsed from `tc class show`),
    so the reconcile loop can drop caps for clients whose time expired."""
    if not HAVE_TC or not _LAN:
        return set()
    ips = set()
    out = _tc("class", "show", "dev", _LAN).stdout
    for line in out.splitlines():
        # "class htb 1:1f3 parent 1: ..."  -> minor 0x1f3
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "class" and parts[2].startswith("1:"):
            minor = parts[2].split(":")[1]
            if minor not in ("1", ""):
                ips.add(minor)  # compared via _minor() hex by the caller
    return ips
