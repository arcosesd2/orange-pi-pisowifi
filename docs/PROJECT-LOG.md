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

---

## 2026-08-23 (later) — full functional audit

Every customer and admin feature exercised in mock mode, plus a firewall
reachability model and concurrency at 10 and 50 simultaneous customers.
Suite: `python tests/run_all.py` (7 suites, all passing).

**Found: `pytest tests` runs nothing.** The files are standalone scripts with a
module-level `sys.exit()`; pytest aborts collection with an INTERNALERROR and
exits reporting success. The suite had effectively never been run as a suite.
`tests/run_all.py` now runs each in its own interpreter.

**Found and fixed — coins were being destroyed.** A pulse train completing with
no insert window open discarded the money: no session, no sale, nothing but
`last_train.credited = False` on the diagnostics page. Proven directly:

```
sales before: 0 sales / P0
sales after : 0 sales / P0
last_train  : {'pulses': 5, 'pesos': 5, 'credited': False}
--> MONEY LOST
```

The relay is supposed to keep the acceptor dead between windows, but it is
optional and can fail. Coins now go to a pending pot, are claimed by the next
window within `uncredited_hold_s` (300 s), shown on the portal as money
waiting, and logged; anything older stays on the books rather than being handed
to an unrelated customer.

**Found and fixed — schedules could be set to a time that never fires.**
`admin_schedules_add` did no validation, and the scheduler matches
`job["time"]` against `strftime("%H:%M")`, so a malformed value is stored,
listed in the table, and never runs. No crash, no warning — the nightly backup
just never happens. Times and job types are now validated, with the error shown.

**Documented, not fixed — a coin inserted during another customer's window is
credited to that customer.** The acceptor reports pulses, not who dropped them.
Reproduced: customer A opened a window, customer B's ₱10 went to A. Only the
relay closes this.

**Verified sound:** expiry and re-purchase (the reported bug class), top-up
stacking, pause/resume including buying while paused, voucher redemption /
reuse / expiry / revocation, trial once-per-period, wallet arithmetic,
backup-restore round-trip with secrets excluded, CSRF enforcement, login
lockout, admin gating of CSV exports, rate limiting, and the whole admin
surface. Concurrency: 50 customers in ~1.6 s, no thread errors, no duplicate
sessions, 200 concurrent status polls clean.

### Audit recommendations closed (same day)

Everything from the audit that software could close:

- **Unclaimed coins are visible, not just held.** Running tally on the
  dashboard plus the claimable pot; a climbing figure is the signal the relay
  is not gating the acceptor.
- **Dashboard warns when no relay pin is set** -- the only defence against F3.
- **Hold policy editable from Diagnostics**, with 0 (discard) allowed and its
  cost spelled out, instead of being config-file-only on a box with no SSH.
- **provision.ps1 runs the suite before uploading** and refuses a failing
  build (`-SkipTests` to override).
- **seal.sh --lock-ssh** disables password auth, since /etc/shadow is cloned
  onto every card. Refuses without an authorized_keys, which would otherwise
  produce unreachable cards.
- **tests/test_templates.py** renders all 11 templates and runs `node --check`
  over every script block. Its entity check is deliberately narrow -- quote
  entities only -- because flagging intentional glyphs made it cry wolf.

8/8 suites pass. Not closed, because they are not software: fit the relay,
settle the SD card / power question, redeploy to the board.

---

## 2026-08-25/26 — WiFi5-Soft cross-check, rate formula, speed limiting

Read the reverse-engineering reports in `~/Desktop/Reverse Engineer` in full
(~27k words) and cross-checked every actionable item against the code. Three
had already been fixed since that Aug-1 review; two had not, and both are now
closed (`2f5f42a`): the cloud dashboard published earnings when `DASH_PASSWORD`
was unset, and the relay pin mapping is documented where it will be read.

**The rate formula was wrong in every report, and in my first reading.**
`vendo.json`'s `minute` is a per-peso multiplier applied to the highest tier
<= the amount, not a total. Settled empirically in `PISOWIFI_RATE_FORMULA.md`
against 10,860 session logs. I compounded it by assuming exact-match lookup and
telling the user their ladder was non-monotonic when it is not. Corrected in
SKILL.md §8a.

`minutes_for()` now takes the best rate at or below the amount, which is
monotonic by construction. On the live board P11 was granting 220 min where the
commercial machine gives 264 — and the one recorded sale on that board is
exactly P11, so a real customer was 44 minutes short.

**Speed limiting** was fully plumbed since v2.3 but shipped as
`default_speed: unlimited` with no UI, so every machine ran uncapped. Added the
dashboard control. Testing it on hardware then found two more silent failures:
the tc tree dies with the LAN device (a dongle unplug is enough) and never came
back, and held coins lived only in RAM so a reboot destroyed them.

**Restart survival, verified on hardware.** Reboot with a live session, a held
coin and a cap in force: session time correct to the second (5654 -> 5536 s over
118 s), P7 restored with a log line, shaping rebuilt on both directions, nft
`allowed` set restored, zero failures. Also added `After=time-sync.target` and a
clock checkpoint, because the board has no RTC and session expiry is absolute.

### Live board state at end of session

`pisowifi-42af48` · WAN `end0` 192.168.254.118 · LAN `enx00e04c6806a8` 10.0.0.1
Real GPIO armed (gpio12 coin / gpio1 relay, `mock=False`), 9/9 suites green,
`nft -c` valid, private-net isolation live at handle 43 above `@allowed` at 46.
Bench mode (`wan_management`) is ON — `seal.sh` clears it.

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
