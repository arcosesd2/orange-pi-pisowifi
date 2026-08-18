# PisoWiFi — Orange Pi One (from scratch)

A complete DIY coin-operated WiFi hotspot: Armbian + hostapd + dnsmasq + nftables,
with custom portal/vending software (Python 3 + Flask) in [`app/`](app/).

```
                        ┌──────────────────────────────┐
 Internet ──Router/─────┤ eth0   ORANGE PI ONE    eth1 ├──── antenna/AP ──))) clients
           Starlink     │        (Armbian)             │     (USB-ethernet)
                        │  pin 3 ◄── coin acceptor     │
                        │  pin 11 ──► relay (optional) │
                        └──────────────────────────────┘
```

**Two supported topologies** (chosen at install time):

| Topology | LAN interface | Install command | Who broadcasts WiFi |
|---|---|---|---|
| **Wired antenna** (recommended) | `eth1` (USB-ethernet) | `bash install.sh --wired` | an external AP/antenna (e.g. CF-EW73) |
| Own radio | `wlan0` (USB WiFi) | `bash install.sh --hostapd 1` | the Pi itself (hostapd) |

`--wired` is the standard vendo layout: **eth0 = internet uplink**, **eth1 = the antenna's
LAN**. It renames the USB-ethernet dongle to `eth1` and pins it by MAC (udev) so it stays
`eth1` across reboots, and it skips hostapd (the antenna is the access point). No VLAN is
needed when the antenna has its own dedicated port; the antenna simply bridges its SSID
untagged into `eth1`.

---

## What's new in v2.4

Security, efficiency, and more of the WiFi5-Soft feature set — everything below
is a config toggle that **defaults to off / current behavior**.

**Security**
- **Hashed admin password** (PBKDF2; the old plaintext is auto-upgraded on first
  login), **CSRF-protected** admin forms + `SameSite`/`HttpOnly` cookies + idle
  logout, and an **audit log** (Admin → Audit) of every admin action.
- **Firewall hardening**: outbound **SMTP block** (anti-spam), **per-client
  connection cap** + **SYN-flood** limits, and optional **anti-tethering**
  (`anti_tether`, TTL=1 so paid WiFi can't be re-shared).
- **DNS ad/malware filter** (Admin → Schedule): block a hosts blocklist for all
  clients — safer *and* less bandwidth.

**Efficiency**
- **CAKE** leaf on the per-client shaper (kills bufferbloat; `gaming_priority`
  adds DSCP prioritization for games/VoIP).
- **waitress** production server (replaces the Flask dev server), **SQLite WAL +
  indexes**, **packet steering (RPS)** across the 4 cores, conntrack/sysctl
  tuning, and optional **flow offload** (`flow_offload`).

**Customer features**
- **Free trial** (X min/device/day), **credit wallet** (save leftover time, use
  it later), **portal branding** (logo/banner/colour + button toggles, Admin →
  Branding), optional **real-time portal** (`sse_enabled`), **scheduled tasks**
  (auto-reboot / rate changes / backups / blocklist refresh, Admin → Schedule),
  and **PPPoE monthly subscriptions** (`pppoe_enabled`, Admin → PPPoE).

## What's new in v2.3

- **Vouchers** — generate/print batches of redeem codes (Admin → Vouchers), sell
  or promo them; customers enter a code on the portal ("HAVE A CODE?"). CSV export.
- **Per-client speed limits (QoS)** — `tc`+`ifb` bandwidth caps via **speed
  profiles** (`speed_profiles` in config); map coin amounts to profiles with
  `rate_speed`, or set `default_speed`. Default is `unlimited` (unchanged behavior).
- **Free devices & bans** — Admin → Devices: **whitelist** (always-on free
  internet + trusted box access, e.g. your phone/CCTV/POS) and **blacklist**.
- **Client isolation** — `ap_isolate=1` in hostapd; customers can't attack each
  other. (For an external AP/antenna, also enable isolation there.)
- **Walled garden** — `walled_hosts`/`walled_domains` let unpaid clients reach
  chosen free sites.
- **Analytics & backup** — 14-day revenue chart, sales CSV, one-click
  config/DB backup & restore (clone machines).
- **Health watchdog** — opt-in (`watchdog_enabled`): auto-recovers WAN/services.
- **Security hardening** — the nftables ruleset now has an **input chain**:
  LAN clients can only reach DHCP/DNS and the portal on the box — **SSH/admin are
  no longer exposed to customers** (whitelisted devices keep full access), plus
  source anti-spoofing.
- **VLAN mode (pairs with a VLAN AP/antenna)** — set `lan_vlan` (e.g. `5`) or
  `install.sh --vlan 5`: the LAN becomes `<lan_if>.<vlan>` (e.g. `eth0.5`) so
  customer traffic arrives **tagged** from the antenna and is segmented from the
  untagged WAN. Match the antenna's SSID VLAN to the same ID.

## 1. Hardware (bill of materials)

| Item | Notes |
|---|---|
| Orange Pi One (Allwinner H3, 512 MB) | the brain |
| microSD 16–32 GB, class 10/A1 | for Armbian |
| 5 V / 2 A PSU, 4.0×1.7 mm barrel | OPi One is picky about power — don't use phone chargers |
| Multi-coin acceptor (CH-926 / "universal 1236") | pulse output, needs **12 V DC** |
| 12 V / 1 A PSU | powers the coin acceptor (and relay) |
| PC817 optocoupler + 1 kΩ + 10 kΩ resistors | isolates the 12 V coin pulse line from the 3.3 V GPIO |
| USB WiFi adapter **with AP-mode support** | e.g. RT5372, AR9271, MT7612U — check `iw list \| grep -A5 "Supported interface modes"` shows `AP` |
| *(optional)* 5 V relay module | cuts coin-acceptor power so only the paying client's window accepts coins |
| *(optional)* enclosure, coin box, LED pilot light | |

> **Alternative topology (recommended for real deployments):** skip the USB WiFi
> and instead plug a cheap router/AP (in AP/bridge mode) into a **USB-to-LAN
> adapter** on the Orange Pi. Far better range and client capacity. In that case
> your LAN interface is the USB ethernet (e.g. `enx...` / `eth1`) instead of
> `wlan0` — just change `lan_if` in the config and skip hostapd.

## 2. Wiring the coin acceptor

Common ground between the 12 V PSU and the Orange Pi GND.

```
12V+ ──► coin acceptor DC12V
12V+ ──► 1kΩ ──► PC817 pin1 (LED anode)
PC817 pin2 (LED cathode) ──► coin acceptor COIN (pulse) line
PC817 pin4 (collector) ──► OPi physical pin 3 (PA12)  +  10kΩ pull-up to pin 17 (3.3V)
PC817 pin3 (emitter)   ──► OPi pin 6 (GND) ──► 12V PSU GND
```

Each coin pulse pulls GPIO low → falling edge counted by `app/coinslot.py`.

**Optional relay** (locks the slot between sessions): relay IN → physical pin 11
(PA1), relay switches the acceptor's 12 V line. Set `"relay_gpio_pin": 11` in
config (or `null` to disable).

**Train the acceptor** (CH-926: press SET, follow manual): sample each coin ~20×
and program pulses = peso value → ₱1 = 1 pulse, ₱5 = 5, ₱10 = 10, ₱20 = 20.
Set the switch on the side to **NO (normally open)** and fast pulse speed.

All of this is configurable at runtime from **Admin → Diagnostics → Hardware
configuration**: which physical pin the coin line is on, pulse edge
(falling/rising), debounce, the **denominations table** (pulses → peso value,
so you can use any pulse scheme, e.g. `{"1":1, "2":5, "3":10}`), pulse-train
end gap, insert window, relay pin and polarity. Saving re-arms the GPIO live —
no restart needed. Use the **coin pulse monitor** on the same page while
inserting real coins to verify counting and timing (it shows every pulse and
the gap between pulses in ms).

## 3. Install Armbian

1. Download **Armbian Bookworm (minimal/server)** for *Orange Pi One* from armbian.com.
2. Flash the `.img.xz` to the microSD with **USBImager** or **balenaEtcher** (from this Windows PC).
3. Boot with ethernet plugged into your modem/router. Find its IP (router DHCP list) and `ssh root@<ip>` (first-boot wizard: set root password, create user).
4. `apt update && apt upgrade -y`, then `armbian-config` → set timezone.

## 4. Install this software

**One command from Windows** (after installing your SSH key on the board once —
see the comment at the top of [deploy.ps1](deploy.ps1)):

```powershell
.\deploy.ps1 -Target <board-ip>
```

It copies the project, fixes line endings, and runs the installer, which
**auto-detects** the interfaces: WAN = whichever holds the default route,
LAN = the other wired interface (USB-ethernet), or `wlan*` (hostapd then
enabled automatically). Override with
`-InstallArgs "--yes","--lan","eth1","--wan","eth0","--hostapd","1"`.

Manual alternative on the board itself: `cd /root/pisowifi && bash install.sh`
(interactive — shows the detected plan and asks before proceeding).

The installer: installs hostapd/dnsmasq/nftables/Flask, copies
[network/nftables.conf](network/nftables.conf), [network/hostapd.conf](network/hostapd.conf),
[network/dnsmasq.conf](network/dnsmasq.conf), deploys the app to `/opt/pisowifi`,
config to `/etc/pisowifi/config.json`, and enables the
[systemd service](systemd/pisowifi.service).

Then edit `/etc/pisowifi/config.json` (SSID shown on portal, rates, admin
password) and reboot.

## 5. Use it

- Clients join the open SSID → any HTTP page (or the OS "sign in to network"
  popup) lands on the portal at `http://10.0.0.1:8080`.
- Tap **INSERT COIN** → the slot unlocks for that client's MAC for 60 s → coins
  credit live → **START BROWSING** applies the time and unblocks the MAC in the
  nftables `allowed` set (kernel auto-expires it — survives even if the app dies).
- Client can **Pause/Resume** remaining time from the portal.
- Admin dashboard: `http://10.0.0.1:8080/admin` — sales, active clients, kick,
  password, remote monitoring, and the **time-per-denomination table**: each
  peso amount → minutes of internet (e.g. ₱1 → 20 min, ₱5 → 120 min), edited
  as a simple add/remove-row table; amounts not listed fall back to
  *minutes per ₱1*. Coins inserted in one window add up before conversion,
  so 5×₱1 earns the ₱5 time — tiers double as promos.
- Admin diagnostics: `http://10.0.0.1:8080/admin/diag` — see next section.

## 5b. Diagnostics page (Admin → Diagnostics)

Everything you need to debug the electronics without a multimeter:

- **Coin acceptor monitor** — live logic level of the coin line, pulses in the
  current train, last coin decoded (`N pulses → ₱X`, and whether it was
  credited), total pulses since boot, and the **gap between recent pulses in
  ms** — this is how you calibrate `pulse end gap` (if a ₱5 shows as five
  separate ₱1 trains, raise the gap above the largest in-coin gap you see).
- **Pin tester** — a clickable map of the whole 26-pin header (drawn like the
  physical board, with SUNXI names like PA12). Click any free GPIO pin to:
  - **Watch** it as an input: live HIGH/LOW level plus an **edge counter**
    that catches pulses faster than the screen refresh — touch a jumper from
    the pin to GND and watch it flip.
  - **Drive** it as an output (3.3 V / 0 V) — e.g. click a relay or LED.
  - Pins used by the coin slot / relay are marked reserved so nothing fights
    over them.
- **Noise scan** — one toggle arms edge detection on *every* free GPIO pin at
  once and records per-pin activity into a table (total edges, edges in the
  last 60 s, first/last activity, live level). Insert a coin while scanning
  and the pin carrying the pulses jumps to the top — the fastest way to find
  a mystery wire or a noisy line. Results persist until the next scan.
- **Relay test** — force the acceptor-power relay on/off.
- **Inject test pulses** — software-inject 1/5/10 pulses to verify the
  counting and denomination mapping end-to-end without touching hardware.
- **Hardware configuration** — coin pin, edge, debounce, denominations
  (pulses → pesos), timings, relay pin/polarity. Applies live.

## 5c. Remote monitoring — earnings on your phone, anywhere

The machine pushes a signed report (earnings, active clients, CPU temp,
uptime) every few minutes to a small dashboard website you host — see
[server/README.md](server/README.md) for the ready-made website and one-command
deploy. Configure it under **Admin → Remote monitoring** (URL + shared key,
then *Send test report now*). Push works through CGNAT — no port forwarding,
any ISP.

For full *control* from anywhere (open the machine's own admin/diagnostics
from your phone), install Tailscale on the board — instructions at the bottom
of [server/README.md](server/README.md).

## 6. How the enforcement works (the important trick)

- `nftables` NAT `prerouting`: MACs in the `allowed` set are accepted (skip
  redirect); everyone else's port-80 traffic is redirected to the portal, and
  their DNS is redirected to local dnsmasq (so hardcoded 8.8.8.8 still resolves).
- `forward` chain drops everything from non-allowed MACs → no internet until paid.
- Paying adds `{ <mac> timeout <seconds> }` to the set — **the kernel handles
  expiry**, the Python app only manages credit and bookkeeping.
- HTTPS is *not* redirected (it can't be without cert errors); the OS captive
  portal probes (`/generate_204`, `/hotspot-detect.html`, `/ncsi.txt`…) are HTTP
  and are answered with a redirect, which is what makes the sign-in popup appear.
- The prompt is made *persistent* three ways:
  1. Unpaid traffic is **rejected fast** (TCP reset / ICMP unreachable), not
     silently dropped — connectivity checks complete instantly as "captive",
     so the OS keeps re-showing the sign-in notification instead of timing out
     and giving up. This also re-triggers the prompt the moment paid time runs
     out mid-browsing.
  2. **RFC 8910/8908 captive-portal API**: DHCP option 114 points phones at
     `/api/capport`, which reports per-client `captive` status and remaining
     seconds. Android 11+/iOS 14+ pin a persistent "Sign in" notification
     until it reports non-captive.
  3. Short DHCP leases (2 h) — every renewal/reconnect re-runs the OS
     captive check while unpaid.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| No AP appears | `systemctl status hostapd` — Debian ships it masked; installer unmasks it. Check adapter AP support with `iw list`. |
| Portal popup doesn't appear | Check `nft list ruleset` has the `pisowifi` table; check dnsmasq is answering on the LAN if. |
| Coins not counting | `journalctl -u pisowifi -f` while inserting. Check common ground, check acceptor switch is NO. If `OPi.GPIO` edge detect fails on newer kernels, see the libgpiod note in `app/coinslot.py`. |
| Clients get IP but no portal | NetworkManager fighting for the LAN interface — installer writes an unmanaged rule; verify with `nmcli device`. |
| Wrong pesos counted | Re-train acceptor; increase `pulse_end_gap_s` if a ₱5 counts as five ₱1 coins. |

## 8. Rolling out more machines

Two ways, fastest first:

1. **Clone the golden SD card.** Once machine #1 is fully working, power it off,
   put its SD card in the PC and read it to an image (USBImager → *Read*), then
   write that image to a new card. The clone is identical — change the admin
   password per site if you want, and note each board gets its own MAC/IP
   automatically. This includes the OS, all configs, and the software.
2. **Fresh install per board.** Flash plain Armbian, boot it, install your SSH
   key (deploy.ps1 header comment), then `.\deploy.ps1 -Target <new-board-ip>`.
   ~10 minutes per machine, always uses your latest software version.

Per-machine settings live only in `/etc/pisowifi/config.json` (name, rates,
admin password) and are preserved on re-deploys, so pushing software updates to
a fleet is just running deploy.ps1 against each machine's IP again.

## 9. Legal / business notes

- Register the business locally (barangay/DTI) as required in your area.
- You are reselling your ISP connection — check your plan allows it (PLDT/Converge
  have SME plans for vending).
