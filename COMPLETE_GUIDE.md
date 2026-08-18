# PisoWiFi — Complete Guide

This one document covers everything built in this project:

1. **Your custom PisoWiFi** — a clean, owner‑controlled coin/voucher hotspot for the Orange Pi One (Python 3 + Flask + nftables on Armbian). Currently **v2.4**. *(This folder: `~/Desktop/Orange Pi One`.)*
2. **The reverse‑engineering** of the commercial **WiFi5‑Soft** firmware it's modeled on. *(Reports in `~/Desktop/Reverse Engineer`.)*
3. **The CF‑EW73 antenna** analysis + how to VLAN‑sync it. *(Report `EW73_ANTENNA_ANALYSIS.md`.)*

The guiding principle throughout: **do everything WiFi5‑Soft does, minus its phone‑home, ngrok/ZeroTier backdoors, and cloud lock‑in.** Every feature is a settings toggle that defaults to *off / current behavior*.

---

## PART A — The custom PisoWiFi software

### A1. Stack & philosophy
- **OS:** Armbian (Debian) on Orange Pi One (Allwinner H3, 4× Cortex‑A7, 512 MB).
- **Serving:** `hostapd` (optional on‑board AP) + `dnsmasq` (DHCP/DNS) + `nftables` (captive portal + firewall) + **Flask app** (portal/admin/billing), served by **waitress** in production.
- **Billing truth = the kernel.** Paid access is an element in an nftables set with a **timeout**; the kernel revokes access on its own when time runs out. The app only manages credit + bookkeeping, and a **reconcile loop** keeps QoS/wallet/pppoe state aligned with that silent expiry.
- **No cloud dependency.** Optional remote monitoring is an outbound **HMAC‑signed** push to *your* server; there is no inbound control channel, no vendor backend.

### A2. Component / file map (`app/`)
| File | Role |
|---|---|
| `main.py` | Flask app: portal, admin, all routes, the `_grant()` credit path, reconcile loop, boot |
| `coinslot.py` | Coin/bill acceptor driver (pulse‑train → pesos), insert window, relay, mock mode |
| `db.py` | SQLite: sales, sessions, settings, vouchers, devices, audit, trials, wallets, pppoe_accounts; WAL + indexes; password hashing |
| `firewall.py` | nftables set control: `allowed` (paid), `whitelist`, `blocked`, `walled` |
| `shaper.py` | Per‑client bandwidth (tc HTB + **CAKE** leaf + ifb); no‑op without `tc` |
| `watchdog.py` | Health watchdog — recover WAN/hostapd/dnsmasq |
| `scheduler.py` | Cron‑like jobs: reboot / set_rate / backup / blocklist_refresh / report |
| `pppoe.py` | PPPoE monthly accounts → `chap‑secrets`, plan expiry |
| `remote.py` | HMAC heartbeat push to your dashboard (see `server/`) |
| `diagnostics.py` | GPIO pin tester, coin monitor, noise scan |
| `templates/` | `portal, admin, admin_login, diag, vouchers, devices, audit, branding, schedules, pppoe .html` |
| `config.json` | All defaults (see A6) |

Network templates live in `network/` (`nftables.conf`, `dnsmasq.conf`, `hostapd.conf`, `tune.sh`); `install.sh` renders and deploys them; `systemd/` holds the service unit.

### A3. How billing works (the pipeline)
```
 Coin acceptor ─pulse→ GPIO (coin_gpio_pin, via optocoupler)  ┐
 Bill acceptor ─pulse→ GPIO                                    ├─ counted by coinslot.py
 Relay        ────────  GPIO (locks the slot between sessions) ┘   (edge-detected)
        │  pulses in one window → peso value (denominations table)
        ▼
 _grant(mac, minutes, source, pesos, speed)   ← shared by coins, vouchers, trial, wallet
        │  minutes = bonus_tiers[amount]  or  amount × minutes_per_peso
        ▼
 db: session expires_at   +   sale recorded
 firewall.allow(mac, seconds)  → nft `allowed` set (kernel timeout)
 shaper.limit(ip, up, down)    → per-client tc/CAKE cap (from the speed profile)
        │  on expiry the kernel drops the MAC → client returns to the captive portal
        ▼
 reconcile loop (every ~20 s): re-applies QoS on IP change, clears caps for expired
        clients, refreshes the walled set + DNS blocklist, expires PPPoE plans
```
Pause/Resume banks remaining time; buying while paused resumes; coins in one insert accumulate before conversion (tiers double as promos).

### A4. Full feature list (v2.3 + v2.4)
**Billing & selling**
- Coin + bill acceptor billing; configurable denominations (pulses→pesos) and rates (peso→minutes, with per‑amount bonus tiers).
- **Vouchers** — generate/print code batches, redeem on the portal, CSV export, void.
- **Free trial** — X free minutes per device per period (`trial_*`).
- **Credit wallet** — save leftover time to the device, use it later (`wallet_*`).
- **PPPoE monthly subscriptions** — username/password accounts with plan expiry (`pppoe_enabled`).

**Access control (captive portal, nftables)**
- Unpaid clients: DNS forced local, HTTP hijacked to the portal, HTTPS left alone; fast‑fail so the OS "sign‑in" popup appears; RFC 8908 capport API.
- **Whitelist** (always‑free devices + trusted box access) / **Blacklist** (banned).
- **Walled garden** — hosts unpaid clients may still reach (`walled_hosts`/`walled_domains`).
- **Client isolation** (`ap_isolate=1`) so customers can't attack each other.

**QoS**
- Per‑client speed caps via **named profiles** (`speed_profiles`, `default_speed`, `rate_speed`); **CAKE** leaf kills bufferbloat; `gaming_priority` prioritizes game/VoIP (DSCP).

**Security**
- nftables **input chain**: LAN clients can only reach DHCP/DNS/portal — **SSH/admin are not exposed to customers**; source anti‑spoofing.
- **Anti‑tethering** (TTL=1, `anti_tether`), **outbound SMTP block**, **per‑client connection cap**, **SYN‑flood** limits.
- **Hashed admin password** (PBKDF2; legacy plaintext auto‑upgrades), **CSRF** on admin forms, `SameSite`/`HttpOnly` cookies, idle logout, login rate‑limit.
- **Audit log** of every admin action.
- **DNS ad/malware filter** (`dns_filter`).

**Efficiency**
- **waitress** WSGI server, **SQLite WAL + indexes**, **packet steering (RPS/XPS)** across 4 cores, conntrack/sysctl/CPU‑governor tuning, optional **flow offload** (`flow_offload`).

**Operations**
- Sales analytics (14‑day chart) + CSV; **config/DB backup & restore** (clone machines); **health watchdog**; **scheduled tasks**; HMAC **remote monitoring** to your own dashboard.

**Portal & network**
- **Branding** (logo/banner/color + button toggles); optional **real‑time SSE** portal; **VLAN mode** (`lan_vlan` → `eth0.5`) to pair with a VLAN AP/antenna.

### A5. Admin panel — page by page  (`http://10.0.0.1:8080/admin`, password `changeme`)
| Page | What it does |
|---|---|
| **Dashboard** | Sales (today/7d/all) + 14‑day chart, active clients + kick, rate/tier settings, admin password, remote monitoring, vouchers summary, backup/restore, watchdog status |
| **Vouchers** | Generate batches (count/minutes/price/speed/expiry), list, void, CSV, printable cards |
| **Devices** | Whitelist (free) / Blacklist (ban) by MAC |
| **Branding** | Logo/banner upload, accent color, show/hide portal buttons |
| **Schedule** | Cron jobs + DNS ad/malware filter toggle |
| **PPPoE** | Monthly subscriber accounts (add/enable/disable/remove) |
| **Audit** | Chronological log of admin actions |
| **Diagnostics** | GPIO pin tester, live coin‑pulse monitor, noise scan, relay/pulse injection, hardware config |

### A6. Configuration reference (`/etc/pisowifi/config.json`; DB settings from the admin UI override these)
| Group | Keys |
|---|---|
| Identity/net | `hotspot_name`, `lan_if`, `wan_if`, `lan_vlan`, `gateway_ip`, `portal_port`, `db_path` |
| Coin hardware | `coin_gpio_pin`, `coin_edge`, `coin_bounce_ms`, `relay_gpio_pin`, `relay_active_low`, `denominations`, `pulse_value_pesos`, `pulse_end_gap_s`, `insert_window_s` |
| Rates | `minutes_per_peso`, `bonus_tiers` |
| QoS | `speed_profiles`, `default_speed`, `rate_speed`, `gaming_priority` |
| Firewall | `anti_tether`, `flow_offload`, `walled_hosts`, `walled_domains` |
| Trial/wallet | `trial_enabled`, `trial_minutes`, `trial_period_hours`, `trial_speed`, `wallet_enabled`, `wallet_save_leftover` |
| Portal | `branding{color,logo,banner,show_redeem,show_trial,show_pause}`, `sse_enabled` |
| DNS | `dns_filter`, `dns_blocklist_url` |
| Ops | `reconcile_interval_s`, `schedules`, `watchdog_*`, `remote_*` |
| PPPoE | `pppoe_enabled` |
| Admin | `admin_password` (initial only — becomes a PBKDF2 hash after first login) |

### A7. Deploy
From Windows (after installing your SSH key on the board once):
```powershell
.\deploy.ps1 -Target <board-ip>
```
On the board: `cd /root/pisowifi && bash install.sh`. Useful flags:
```bash
bash install.sh --wan eth0 --lan eth1                 # explicit interfaces
bash install.sh --wan eth0 --lan eth0 --vlan 5        # customer traffic on VLAN 5 (antenna)
```
The installer: installs packages (+ waitress; + pppoe when `pppoe_enabled`), renders `nftables.conf` (filling the `anti_tether`/`flow_offload` tokens from config), sets up the LAN address / VLAN device, packet‑steering/sysctl tuning unit, and enables the services. It keeps `config.json` in sync with the chosen interfaces.

**Hardening checklist for a live site:** change the admin password; add your own phone/CCTV to the **whitelist** (they also get SSH access); leave `flow_offload` **off** unless you've verified it with your QoS; enable `anti_tether` if you don't want customers re‑sharing; turn on `dns_filter` + a daily `blocklist_refresh` schedule.

### A8. Develop & test (no hardware needed)
On any PC (Windows/Mac/Linux), the app auto‑detects "no GPIO/nft" and runs **fully mocked**:
```bash
cd "Orange Pi One/app" && python main.py     # http://localhost:8080  +  /admin (changeme)
```
The admin **Simulator** injects coins; every feature works in mock. A full end‑to‑end self‑test lives in the scratchpad as `final_smoke.py` (hashed login, CSRF, vouchers, trial, wallet, SSE, scheduler, PPPoE, audit, backup, CSV) — run it to regression‑check after changes.

### A9. Troubleshooting
| Symptom | Check |
|---|---|
| No portal popup | `nft list ruleset` has table `pisowifi`; dnsmasq answering on the LAN |
| Coins not counting | `journalctl -u pisowifi -f` while inserting; common ground; acceptor switch = NO; raise `pulse_end_gap_s` if ₱5 counts as five ₱1 |
| Client can't reach box admin | expected — add the device to the **whitelist** for full access |
| QoS not limiting | `tc -s class show dev ifb0`; ensure `default_speed`/`rate_speed` isn't `unlimited` |
| Flow offload + QoS conflict | disable `flow_offload` (it bypasses conntrack); re‑test |
| PPPoE won't auth | `pppoe_enabled` true + installer re‑run; account **enabled** and not expired |

---

## PART B — The reverse‑engineered WiFi5‑Soft (context)
Your build mirrors a commercial firmware I fully reverse‑engineered. Reports in `~/Desktop/Reverse Engineer`:
- **`MASTER_ANALYSIS.md`** — the consolidated everything (boot → OS → app → C2 → antenna → security → rebuild spec).
- `DRIVE_D_INDEPENDENT_ANALYSIS.md` (boot partition), `PARTITION_2_AND_3_ANALYSIS.md` (rootfs + data), `CROSS_REFERENCE_vendor_vs_clean.md` (vendor vs your clean build), plus the earlier vendor/Gemini reports.

**What WiFi5‑Soft is:** ImmortalWrt (kernel 6.6.69) + a proprietary **Node.js `pkg`** app (`/soft/index.o`). **Why avoid it:** it phones home to **`w5.ozs.icu`** (e‑load/license backend) and ships **four remote‑access channels** — ngrok (public SSH + admin), ZeroTier (`12ac4a1e71b7237b`), WireGuard, plus a weak MD5 root hash and a cleartext admin password (`wifi5soft`). Your Python build reproduces its *useful* features (coin billing, vouchers, per‑client QoS, VLAN, captive portal) with none of that. Two things stayed out of reach in the RE: `public.tar.gz` (portal SPA — AES key is device‑serial‑bound) and clean source of the `.jsa` V8‑bytecode modules (logic reconstructed from constants).

## PART C — The CF‑EW73 antenna
Your Wi‑Fi is broadcast by a **Comfast CF‑EW73** (CF‑E355AC‑class, Atheros ar71xx), running stock **`CF‑EW73‑V2.6.1.5`**. Details + the VLAN‑sync procedure are in `~/Desktop/Reverse Engineer/EW73_ANTENNA_ANALYSIS.md`. Essentials:
- Stock firmware **phones home** (`120.26.131.108`, `cflogin.cn`) and has **root SSH open on WAN** + a weak shared hash — consider flashing the OpenWrt image in `~/Downloads/Openwrt-EW73` for a clean AP. **Don't** flash the `CF‑EW73‑VLAN‑V2.6.0.2` file you downloaded — it's *older* than what's on the unit; VLAN already works on 2.6.1.5.
- **VLAN sync:** set the customer SSID's **VLAN ID = 5** on the antenna (its `ssid_vlan`), AP/bridge mode, tagged uplink → lands on the Pi's `vlan.05` (set `lan_vlan: 5` / `install.sh --vlan 5`). Both speak plain 802.1Q, so they interoperate. Confirmed feasible from the firmware.

---

## File manifest
- **Software (this folder):** `app/` (11 modules + 10 templates), `network/`, `systemd/`, `install.sh`, `deploy.ps1`, `config.json`, `server/` (cloud dashboard), `README.md`, **this guide**.
- **Analysis (`~/Desktop/Reverse Engineer`):** `MASTER_ANALYSIS.md` + the per‑layer reports + `EW73_ANTENNA_ANALYSIS.md`.
- **Plan of record:** `~/.claude/plans/mighty-watching-peach.md` (the approved v2.4 build).
- **Version:** PisoWiFi **v2.4**.
