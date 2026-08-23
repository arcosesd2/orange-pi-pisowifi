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

**Two supported topologies** — you don't choose, the board works it out at every
boot from the hardware plugged into it:

| Topology | Customer LAN | Detected because | Who broadcasts WiFi |
|---|---|---|---|
| **Wired antenna** (recommended) | the USB-ethernet dongle | it's on the USB bus, the uplink is on the SoC bus | an external AP/antenna (e.g. CF-EW73) |
| Own radio | the USB WiFi adapter | no ethernet dongle present, so hostapd is switched on | the Pi itself (hostapd) |

The wired layout is the standard vendo build: **onboard NIC = internet uplink**,
**USB-ethernet dongle = the antenna's LAN**, no hostapd (the antenna is the
access point). No VLAN is needed when the antenna has a dedicated port; it just
bridges its SSID untagged into the dongle.

Nothing about a specific board is stored on the card — no MAC pinning, no
interface names in unit files — which is what lets one SD card image be cloned
across a whole fleet. Override with `install.sh --lan <if> --wan <if>` if you'd
rather pin them. See [section 8](#8-rolling-out-more-machines-plug-and-play-cards).

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
  LAN clients can only reach DHCP/DNS and the portal on the box — **SSH is no
  longer exposed to customers** (whitelisted devices keep full access), plus
  source anti-spoofing. (The *admin panel* shares the portal port and so is
  still reachable at the TCP level by customers; it is gated in the app
  instead — see [Admin exposure](#admin-exposure-read-this-before-you-deploy).)
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

## 1b. Turn on client isolation at the access point

**This is the one security control the Pi cannot enforce for you.** Two
customers on `10.0.0.0/24` are on a single switched segment, so their traffic
never reaches the Pi at all — nftables cannot filter what it does not see. A
customer can otherwise scan, ARP-spoof and attack the phone next to them.

- **Own-radio topology** (USB WiFi + hostapd): already handled —
  [`network/hostapd.conf`](network/hostapd.conf) sets `ap_isolate=1`.
- **Wired topology** (external AP/antenna, the recommended build): you must
  enable it on the AP itself. On the CF-EW73 and most cheap APs the setting is
  called **AP Isolation**, **Client Isolation** or **Station Isolation**, under
  the wireless or multi-SSID page. Turn it on and verify: connect two phones
  and try to ping one from the other. It should fail.

Customers are separately blocked from routing into your own network — the
firewall rejects RFC1918, link-local and CGNAT destinations for paying clients,
so the ISP router's admin page, your NAS and your CCTV are unreachable from the
hotspot. Whitelisted devices and anything in the walled garden are exempt, so
add a local host to `walled_hosts` if you deliberately want customers to reach
it.

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

> **Reusing a WiFi5-Soft harness?** That firmware numbers GPIOs by **sysfs
> line**, this project numbers them by **physical header position**. They
> coincide for the coin wire and diverge for the relay:
>
> | Signal | WiFi5-Soft `vendo.json` | = SUNXI | = physical pin | Set here as |
> |---|---|---|---|---|
> | Coin | `coinpin: 12` | PA12 | **3** | `coin_gpio_pin: 3` ✅ same wire |
> | Relay | `relaypin: 11` | PA11 | **5** | `relay_gpio_pin: 5` ⚠️ **not 11** |
> | Bill | `bill: 6` | PA6 | **7** | *(bill acceptor not implemented)* |
>
> Physical pin 11 is PA1 — a different pin entirely. The coin line works
> unchanged, so the mismatch shows up only as a relay that never clicks.

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

This is only needed for **machine #1**. Once it works you seal it into a master
image and later cards need none of this — see
[section 8](#8-rolling-out-more-machines-plug-and-play-cards).

1. Download **Armbian Bookworm (minimal/server)** for *Orange Pi One* from armbian.com.
2. Flash the `.img.xz` to the microSD with **USBImager**, **balenaEtcher**, or the
   **Armbian installer app** (from this Windows PC).
3. Boot with ethernet plugged into your modem/router. Find its IP (router DHCP list) and `ssh root@<ip>` (first-boot wizard: set root password, create user).
4. `apt update && apt upgrade -y`, then `armbian-config` → set timezone.

**If your flashing tool offers an autoconfig / first-boot profile** (Armbian
writes it to `/root/.not_logged_in_yet`), fill it in — it replaces the
interactive first-boot wizard in step 3, so the board comes up straight to a
login the deployer can use:

| Field | Value |
|---|---|
| Root password | a real password — this is what the deployer connects with |
| First user → username / password | fill both in; **leaving the username empty makes Armbian stop and ask on first boot**, which blocks unattended setup |
| Login shell | `bash` |
| Timezone | `Asia/Manila` — scheduled tasks fire at local `HH:MM` and sales are stamped in local time |
| Locale | `en_US.UTF-8` |
| Configure network | leave **off**: `eth0` must take DHCP from your router, and PisoWiFi configures the customer LAN itself |
| Remote config URL | leave empty |
| SSH key URLs | optional; a key is nicer than a password if you have one on GitHub |

> That file stores the passwords **in plain text**. `seal.sh` deletes it, so
> master images never carry them — but until you seal, treat the card as
> holding your root password in the clear.

**On Debian 13 / Trixie:** it should install and run, but coin counting is the
risk. `OPi.GPIO` uses the deprecated sysfs GPIO interface (see the note in
[app/coinslot.py](app/coinslot.py)), which newer kernels may not expose. Verify
with **Admin → Diagnostics → coin pulse monitor** before trusting a machine with
real coins; Bookworm is the known-good option.

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

## 5d. Admin exposure — read this before you deploy

The captive portal only works if the portal port is open to every client, and
the admin panel lives on that same port. So **customers can open a TCP
connection to `/admin`** — the firewall can't separate them. Access is gated in
the application instead:

| Where you are | Can you open `/admin`? |
|---|---|
| A customer on the hotspot | **No** — 403, once any device is whitelisted |
| A **whitelisted** device on the hotspot | Yes |
| The machine itself (`127.0.0.1`) | Yes |
| Tailscale / any non-customer interface | Yes |

**The bootstrap:** after install, SSH is blocked on *both* interfaces (customers
are rejected, and the WAN side is dropped outright). So on a fresh machine there
would be no way in at all — which is why admin stays reachable from the customer
network **until you whitelist your first device**. Adding that device is what
locks the machine down.

> **Whitelist your own phone first.** If you whitelist the CCTV first, you lock
> yourself out of the hotspot path. The first-run setup page offers to whitelist
> the device you're using — leave it ticked.
>
> **To recover a lockout:** set `"admin_lan_access": "any"` in
> `/etc/pisowifi/config.json` and `systemctl restart pisowifi`.

**Passwords.** The installer generates a random admin password and prints it
once (it also stays in the root-only config file). If a machine is somehow still
on the shipped `changeme`, the admin panel refuses to open anything but the
password-change page. Login lockouts are keyed on the client's MAC and back off
1 → 2 → 4 → … minutes, so guessing can't be reset by picking a new IP.

**The one thing this does not fix:** the SSID is open, so an admin login over the
hotspot crosses the air in cleartext and can be sniffed. For real remote
administration install **Tailscale** on the board (see
[server/README.md](server/README.md)) and use that instead — it's encrypted end
to end and reaches the box through CGNAT.

**Accepted risks**, worth knowing rather than fixing:

- **MAC-based access on an open network.** A sniffed MAC can be cloned to ride a
  paid session — or a whitelisted one to gain trusted access. This is inherent to
  every MAC-based captive portal, commercial ones included.
- **The app runs as root** (it needs nft/tc/GPIO), so any bug in it is a root bug.
- **Physical access to the SD card** yields the database (PPPoE passwords are
  necessarily plaintext for CHAP) and the config. Nothing is encrypted at rest.

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

## 8. Rolling out more machines (plug-and-play cards)

Machine #1 is the only one that needs an install. After that you seal it into a
master image, and every card written from that image is **insert-and-go**: no
SSH, no config file, no installer.

**Build the master, once:**

1. Get machine #1 fully working (sections 3–5).
2. Seal it — from the deployer's Network Deploy tab press **Seal for imaging…**,
   or on the board run:

   ```bash
   bash /root/pisowifi/seal.sh --yes
   ```

   This wipes everything that makes it *that* box — sales database, admin
   password, SSH host keys, machine-id, logs, and the rendered network
   configs — then powers off. Your rates, hotspot name, branding and feature
   toggles are kept. Add `--zero` to make the image compress much smaller.
3. Pull the card and read it to an image: deployer → **SD Card → READ / CLONE**
   (or USBImager → *Read*). That `.img` is your master.

**Every machine after that:**

Write the master to a card, put it in a board, power on. On first boot the card
gives itself a fresh machine-id, SSH host keys and a unique hostname
(`pisowifi-xxxxxx`), and on *every* boot it re-detects its own hardware. Connect
a phone to the hotspot, open the portal, go to **Admin**, log in with `changeme`
— it immediately forces you to set a real password and offers to whitelist the
phone you are holding. Rates and the hotspot name are on the same admin pages.

> Do that first-login step before the machine faces customers. Until it is done
> the box is on the shipped password, and the setup page is deliberately
> reachable from the customer LAN so you can reach it from a phone.

**Why a clone works on different hardware now.** Nothing board-specific is
written to the card. [`network/detect.sh`](network/detect.sh) runs at every boot
(`pisowifi-detect.service`) and picks the interfaces from what is physically
present — onboard NIC = internet uplink, USB-ethernet dongle = customer LAN,
WiFi adapter = LAN with hostapd — then regenerates `nftables.conf`, the dnsmasq
config, `hostapd.conf` and the LAN address to match. A different dongle with a
different MAC just works. To see what it would choose without changing
anything:

```bash
pisowifi-detect.sh --dry-run
```

Pin the interfaces by hand instead with `install.sh --lan eth1 --wan eth0`, or
by setting `"auto_detect_interfaces": false` in the config.

**Pushing software updates to a fleet** is still `.\deploy.ps1 -Target <ip>` per
machine; `/etc/pisowifi/config.json` is preserved across re-deploys.

## 9. Legal / business notes

- Register the business locally (barangay/DTI) as required in your area.
- You are reselling your ISP connection — check your plan allows it (PLDT/Converge
  have SME plans for vending).
