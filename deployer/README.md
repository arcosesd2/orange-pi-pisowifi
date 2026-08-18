# PisoWiFi Deployer (Windows GUI, `.exe`)

A single self-contained Windows app that sets up PisoWiFi machines two ways:

1. **Network Deploy** — scans your LAN for Orange Pi boards, you pick one, set
   the rates / hotspot name / admin password, and it uploads the whole project
   over SSH and runs the installer — then verifies the services came up.
2. **SD Card** — flash an Armbian image to a microSD, **or clone a golden
   card**: read a fully-working card into an image, then write that image to as
   many new cards as you like (each boots ready to use).

The entire PisoWiFi project is baked into the `.exe`, so there is nothing else
to download or copy alongside it.

![tabs: Network Deploy | SD Card, with a shared log + progress bar]

## Get the exe

**Option A — use the prebuilt one:** `dist\PisoWiFi-Deployer.exe` (built from
this folder). Copy it anywhere.

**Option B — build it yourself:**

```powershell
cd deployer
pip install -r requirements.txt
.\build.ps1            # -> dist\PisoWiFi-Deployer.exe  (also runs a self-test)
```

`build.ps1` bundles `..\app`, `..\network`, `..\systemd`, `..\install.sh`,
`..\server` and `..\README.md` into the exe as `payload/`. Rebuild whenever you
change the project so the exe ships your latest software. (The build makes a
console-visible exe so `--selftest` and crash output are readable; add
`--windowed` in `build.ps1` if you'd rather hide the console.)

## Using it

### Network Deploy tab
1. **Run the app as Administrator** (needed for the SD Card tab; harmless for
   deploy).
2. Pick your **subnet** (auto-filled) and click **Scan network**. Boards show
   up as any host with SSH open — or type an **IP** and **Add** it manually.
3. Select a board. **Identify board** logs in and prints its model
   (confirms it's an "Orange Pi One"). One-time SSH setup:
   - Easiest: put your SSH key on the board once, leave the password blank.
     `type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@<ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"`
   - Or just type the board's **SSH password** in the field.
4. Fill in **hotspot name, admin password, minutes per ₱1, denominations**
   (and optionally the remote-dashboard URL/key). Leave *"Write these settings
   to the board"* ticked to push them into `/etc/pisowifi/config.json`.
5. **Deploy PisoWiFi ▶**. The log streams every step:
   - local `py_compile` of the app (catches errors before touching the board),
   - upload with automatic Windows→Unix line-ending fixup,
   - `install.sh --yes` (auto-detects the WAN/LAN interfaces),
   - writes your settings, restarts, and verifies all four services + the
     portal responds `HTTP 200`.
   When it finishes it prints the board's `http://<ip>:8080/admin` URL.

### SD Card tab
> ⚠ **Writing or cloning ERASES the target card.** The app only lists
> **removable** (USB/SD) disks and hides the Windows system disk. Always
> re-check the disk number, model and size before confirming. Requires
> **Administrator**.

- **Flash an image:** pick an Armbian `.img` or `.img.xz` (xz is decompressed
  on the fly), select the card, **FLASH ▶**. Boot the card, then use the
  Network Deploy tab to install the software.
- **Clone a golden card** (fastest way to make many identical machines):
  1. Set up ONE board fully (Armbian + a Network Deploy). Power it off, put its
     card in the PC.
  2. On this tab, select that card, choose an output file
     (`golden.img.xz` recommended — compressed), **READ / CLONE ▶**.
  3. Swap in a blank card, pick `golden.img.xz` under *Flash*, **FLASH ▶**.
     Repeat for every new card. Each boots as a complete, ready-to-run PisoWiFi.

## How it works / notes

- **SSH** uses paramiko (bundled) — key or password auth. Unknown host keys are
  auto-accepted (it's your LAN).
- **Scanning** looks for TCP port 22 across the `/24`; a full sweep takes a few
  seconds. Manual IP entry covers anything the sweep misses.
- **Raw disk I/O** uses the Windows physical-drive API directly (locks and
  dismounts the card's volumes first, sector-aligned writes, flush + property
  refresh at the end). No third-party imager needed.
- Cloning reads the **entire** card (e.g. all 16 GB), so it can take 10–30 min
  on a USB-2 reader; compressed output keeps the file small.
- **Line endings**: text files are converted to Unix `\n` during upload, so
  editing the project on Windows never breaks the shell/Python on the board.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "run the app as Administrator" on the SD tab | Right-click the exe → Run as administrator. Raw disk access needs it. |
| Board not found by Scan | It may be on a different subnet, or SSH not up yet. Use the **IP + Add** box, or check the board is booted and on the network. |
| SSH auth fails | Fill the **SSH password**, or install your key on the board (command above). |
| Card not listed | Click **Refresh disks**. USB card readers sometimes enumerate slowly; reinsert. Tick *show fixed disks* only if you know what you're doing. |
| Portal shows `HTTP 000` right after deploy | Transient — the service is still starting; the verify step retries for 10 s. Reload `http://<ip>:8080` if needed. |
