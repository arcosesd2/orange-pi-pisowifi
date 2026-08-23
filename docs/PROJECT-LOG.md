# PisoWiFi — project log

A running record of what was changed, what broke, and what was decided.
Append a dated section per working session. Rules distilled out of these
incidents live in [`.claude/skills/pisowifi/SKILL.md`](../.claude/skills/pisowifi/SKILL.md);
this file is the evidence behind them.

---

## 2026-08-22/23 — first hardware bring-up, and making the card plug-and-play

Branch: `claude/pisowifi-existing-app-6c1b6a` (not merged to `master`).

### Starting point

v2.4 was complete and unit-testable but had **never run on hardware**. The
project scaffolded a full coin-operated hotspot: Flask portal + admin, nftables
enforcement, per-client shaping, vouchers, wallet, PPPoE, scheduler, watchdog,
cloud heartbeats, and a Windows GUI deployer.

### Goal that drove the work

Make an SD card that can be cloned across many machines: **insert, power on,
set a password from a phone.** No SSH.

### What was rebuilt

Everything machine-specific used to be decided by `install.sh` and written to
disk — the dongle's MAC pinned into a udev rule, interface names baked into
unit files, network configs rendered once. A card built that way only ever
worked on the machine the installer ran on.

| New | Purpose |
|---|---|
| `network/detect.sh` | Re-derives interfaces and re-renders nftables/dnsmasq/hostapd on **every boot**. Bus type from `device/modalias`; a NIC already holding the default route wins. `--dry-run` reports without changing anything. |
| `network/firstboot.sh` | Per-card identity: machine-id, SSH host keys, hostname, and `growpart`+`resize2fs` (Armbian's own resize is a one-shot that already fired on the master). |
| `seal.sh` | Turns a working board into a master image. |
| `provision.ps1` | Builds machine #1 in one command: finds the board, handles the SSH key, installs, verifies, and can seal. |
| `.claude/skills/pisowifi/SKILL.md` | The rules distilled from everything below. |

`pisowifi-net.service` was removed (it re-applied the old board's interface
names); hostapd/pppoe/ppp now install unconditionally with their units gated on
flag files the detector drops, so one image covers every topology.

**Sealing keeps configuration, drops identity.** An early version deleted the
whole database — which also deletes the coin pin, rates, denominations and
branding, since the admin UI writes them there. A clone would silently revert
to `config.json` defaults: a coin line wired to pin 7 would come up listening
on pin 3 and never count a coin. It now clears the customer tables plus exactly
four identity keys (`admin_pw_hash`, `admin_pw_default`, `SECRET_KEY`,
`device_id`).

### Bring-up, in the order things broke

1. **Install died, root filesystem went read-only.** Transient — never recurred,
   and no mmc errors were logged (journald could not write). Card is a generic
   Phison `SD16G` from 02/2019; power is a buck converter. **Unresolved** — if
   it repeats on a different card, it is the power rail.
2. **nftables would not load.** `define CLIENT_NET = "10.0.0.0/24"` — nft 1.1.3
   resolves quoted values as hostnames. Pre-existing; Debian 12's 1.0.6
   tolerated it.
3. **Total lockout.** With nftables finally loading, `iifname $WAN_IF drop`
   sealed the uplink — the installer killed its own management path and every
   verification step timed out against a healthy board. Recovered by
   re-flashing. Fixed with the `wan_management` toggle.
4. **App restarted 35 times.** `ModuleNotFoundError: No module named 'shape'`.
   `deploy.ps1`'s CRLF strip had deleted a trailing `r` from every line of every
   file — **7 of 10 modules corrupted, silently**. See SKILL.md §1e.
5. **"GPIO not usable"** — twice, both times my own PowerShell quoting, on a
   board where GPIO works fine.

### The GPIO question, answered

**Debian 13 does not break the coin slot.** Kernel 6.18.44-current-sunxi still
exposes `/sys/class/gpio`; `OPi.GPIO` arms pin 3 with edge detection; coins
register and credit correctly. This had been the main risk since the first
question about Trixie.

`CoinSlot._setup_gpio()` was still hardened: it runs at import time and only
guarded `ImportError`, so a non-import exception would take the whole app down
over a coin acceptor.

### Admin UI rebuild

Reported as "cannot manage, edit or add time per peso". Root cause was not the
UI: `const TIERS = {{ tiers }}` HTML-escaped its quotes, breaking the only
script block on the page — which also stamped the CSRF token into every form.
**Every POST on the dashboard failed silently.** The tell was an empty
`settings` table.

Fixed with `| tojson`, CSRF stamping moved into its own block in a new shared
layout. Eight templates each carrying a private `<style>` block and hand-written
nav were replaced by `app/static/admin.css` + `templates/_admin_base.html`.
The coin→time and pulses→peso editors were rebuilt as row editors that compute
the resulting duration and hourly rate live.

### Portal unreachable after paying

Reported: insert coin, browse, return to portal → "refused to connect".
`prerouting` accepts `@allowed` clients before the port-80 redirect, and nothing
listens on :80, so the input chain answered with a TCP reset. A customer could
not check time, pause, or insert another coin until their session expired.
Fixed by redirecting gateway-addressed port 80 above the short-circuits.

### Open items

- [ ] Redeploy `47d357f` (portal-after-paying fix) — board was off the network.
- [ ] Train the coin acceptor and verify denominations end to end.
- [ ] Set admin password, rates, hotspot name; whitelist the owner's phone.
- [ ] Then `.\provision.ps1 -Target <ip> -Seal -ZeroFill` to cut the master.
- [ ] **Unresolved:** the read-only filesystem event. Suspect the buck converter
      or the 2019 no-name SD card. Retest with a branded card and a 5 V/2 A supply.
- [ ] Root password is cloned into every card (`/etc/shadow`); treat as a fleet
      secret, or set key-only SSH before imaging.
- [ ] Branch is not merged to `master`; the prebuilt deployer `.exe` is stale.
