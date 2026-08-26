---
name: pisowifi-orange-pi-project
description: "DIY coin-operated WiFi vending machine on an Orange Pi One; running on hardware as of 2026-08-26, being turned into a cloneable master SD card"
metadata: 
  node_type: memory
  type: project
  modified: 2026-08-26T00:00:00.000Z
  originSessionId: f9d04533-9903-4fd1-99c1-9635ef3b286c
---

The user is building a **pisowifi** (coin-operated WiFi vending) business
machine on an **Orange Pi One** (Allwinner H3, reports 991 MB RAM). Custom
software lives in `C:\Users\Adiel\Desktop\Orange Pi One\` - Flask portal +
admin, SQLite, nftables enforcement, per-client shaping, vouchers, PPPoE,
scheduler, watchdog, cloud heartbeats, plus a Windows GUI deployer.

**Status 2026-08-26: running on real hardware and taking coins.** Board is
Armbian 26.8.1 trixie, kernel 6.18.44-current-sunxi, hostname
`pisowifi-42af48`. Coin credit verified with real coins; real GPIO armed
(gpio12 coin, gpio1 relay). Rebooted under load and verified: session time,
held coins, tc shaping and the nft `allowed` set all survive. The goal now is
a **cloneable master SD card** - insert, power on, set a password from a
phone, no SSH - via boot-time hardware detection (`network/detect.sh`),
per-card identity (`network/firstboot.sh`) and `seal.sh`. Machine #1 is built
with `.\provision.ps1 -Target <ip>`.

Working on this project: **load the `pisowifi` skill first** - see
[[pisowifi-project-references]], and note [[pisowifi-gpio-interrupts-dead]]
and [[pisowifi-anti-tethering]]. Detailed history is in the repo's
`docs/PROJECT-LOG.md`, not here.

Facts that are not derivable from the repo:

- Work is on branch `claude/pisowifi-existing-app-6c1b6a` in a worktree,
  **not merged to `master`**; the user chose to leave `master` alone. The
  prebuilt `deployer/dist/PisoWiFi-Deployer.exe` is stale (Jul 20).
- **Bench mode is currently ON** on the board (`wan_management`), which keeps
  SSH open on the WAN side. `seal.sh` clears it; do not ship a card with it.
  Kept deliberately until the card is sealed, at the owner's choice.
- **Hardened 2026-08-26 and left that way:** SSH is key-only (`/etc/shadow` is
  cloned to every card, so a password was a fleet secret); the plaintext admin
  password is out of `config.json`; `admin_lan_access` is `off` so the customer
  network can never reach `/admin`; `wan_admin` serves the admin page on the
  uplink port and survives sealing. Verified: customer LAN 403, uplink 302,
  customer portal 200.
- **Admin over "the cable" means the uplink port, and only that.** Wi-Fi
  clients and anything cabled into the AP arrive on the same interface in the
  same subnet -- the box cannot tell a cable from a radio down there.
- The board's admin password is the install-minted one (verified against the
  stored hash, not guessed). It is no longer in `config.json`; only the PBKDF2
  hash in the DB. If it is lost, recovery is clearing `admin_pw_hash`.
- Board takes DHCP and **moves**; it has been .116, .117 and .118. Hostname
  `orangepione` resolves before install; after install it renames itself
  `pisowifi-<mac6>`. A router DHCP reservation would save time.
- Onboard NIC is **`end0`** (not `eth0`); customer LAN is a USB-ethernet
  dongle, which has been swapped more than once and changes name each time.
- **The reference machine is a WiFi5-Soft commercial pisowifi**, reverse-
  engineered into reports on the user's Desktop. Its rate table is a
  *floor lookup of per-peso multipliers*, not per-amount totals - every RE
  report got this wrong, and so did I on first reading. Settled empirically
  against 10,860 of the user's own session logs.
- Powered from a **buck converter**, and the SD card is a no-name Phison
  `SD16G` from 02/2019. One unexplained read-only-filesystem event during an
  install; never reproduced. Suspect one of those two. Retest with a branded
  card and a real 5 V/2 A supply before trusting the machine with money.
- Claude's shell on this PC is **not elevated**; IP changes and `wsl --mount`
  need an admin PowerShell the user must run. Norton AV/firewall/VPN all run
  here and can filter a newly-classified network.
- The laptop's Ethernet port has carried a stale static config in the past
  (`192.168.254.100/24` + dead gateway `.253`) that collides with the Wi-Fi
  subnet and, because its interface metric is lower, steals the route. Check
  it before any direct laptop-to-board link work. It currently holds a
  deliberate static `10.0.0.2/24` for reaching the board's customer LAN.
- **Discovery recipe for a direct-connected board with unknown IP** (no DHCP,
  no static, no admin rights needed): `ping -6 ff02::1%<ifIndex>`, then read
  `Get-NetNeighbor -InterfaceIndex <ifIndex>` for the link-local + MAC, then
  `ssh root@fe80::...%<ifIndex>`.
