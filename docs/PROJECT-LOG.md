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

---

## 2026-08-26 — the coin slot was deaf, and everything said it was fine

Reported as "it does not read my inserted coin". This turned out to be the most
serious defect the project has had: **the machine accepted coins and credited
nothing**, while every diagnostic reported health.

### What was actually wrong

`OPi.GPIO.add_event_detect()` arms the *deprecated sysfs* edge interface. On
kernel 6.18.44-current-sunxi the kernel accepts the arming and never delivers
an interrupt. Not once. No exception, no log line, no restart, no flag.

All of this was true simultaneously with zero coins counted:

```
systemctl is-active pisowifi   active, 0 restarts
/sys/class/gpio/gpio12         direction=in, edge=falling
/proc/1024/fd/6                -> .../gpio12/value      (open, armed)
slot.mock                      False        slot.gpio_error  None
```

### How it was found

Level-polled the coin pin from a separate process while the app ran with its
own fd open on the same pin. Five clean 50 ms pulses landed on the line:

```
edge @11.1388s 0 -> 1      edge @11.1889s 1 -> 0
edge @12.9900s 0 -> 1      edge @13.0400s 1 -> 0
   ... 5 pulses, ~50 ms wide, 1.4-2.5 s apart
```

The app's pending pot did not move off `[7, ...]`. Line pulses, app deaf —
which separates "broken acceptor" from "broken kernel API" in one coin drop.
That is the test to reach for; it is now SKILL.md §5a.

### The fix (`86f2929`)

The coin line no longer goes near `add_event_detect()`. It reads through
libgpiod v2 character-device edge events (`python3-libgpiod`, installed on the
board), with level polling as a fallback that is known to work here.

The first deploy exposed a second trap: a **sysfs export outlives its process**
and holds the line against the character device, so gpiod got `EBUSY` and the
app silently fell back to polling. Left alone, every machine upgrading to this
commit would have quietly never used the good backend. `_unexport()` now clears
it first.

Both edge directions are counted and surfaced on Diagnostics, because a line
pulsing the wrong way looks exactly like a dead acceptor.

### Verified with real money

`P5 coin -> 120 minutes`, sale #3, credited 238 s after the gpiod backend armed
— through a real insert window on the portal, at the correct P5 = 2 h tier.

### Two hardware facts found in the traces

- **The coin line is active-HIGH** (idles 0, pulses to 1), the opposite of the
  README's PC817 + pull-up wiring. `falling` catches the trailing edge so it
  works; `rising` is correct and should be set.
- **The acceptor is in slow-pulse mode** — 1.4-2.5 s between pulses against
  `pulse_end_gap_s = 0.7`, so one P5 coin arrives as five P1 trains. Totals
  survive only because the denomination table is linear. Set the acceptor to
  fast pulse, or raise the gap to ~3 s.

### Also checked, at the user's request

Portal UI sync is correct end to end: `/api/status` -> `slot.status(mac)`
carries `open/busy/pesos/seconds_left/pending`, and `portal.html` renders all
of them, polling at 1 s (5 s alongside SSE). Note the inserted total only
appears in the *insert view* — with no window open the portal shows the pending
note instead, which is why a coin dropped without tapping INSERT COIN looks
like nothing happened even when it was counted.

### Production readiness — checked live 2026-08-26

Holds up (verified against the running ruleset):

- Customers cannot reach the owner's router or any private network. Handles
  43/45 reject RFC1918 + IPv6 private destinations, **above** `@allowed accept`
  at 46, so a fully paid customer is still blocked.
- The Pi exposes only DHCP 67, DNS 53, portal 8080 and ICMP echo to customers;
  anti-spoof (`ip saddr != 10.0.0.0/24 drop`), SYN rate limit, conntrack cap
  2000/IP, mail ports 25/465/587 dropped so the uplink cannot be used to relay
  spam.
- Admin auth is PBKDF2 200k + CSRF + escalating lockout (1,2,4,8..60 min).

Blocks production until fixed:

1. **Customer-to-customer traffic is not filtered by this machine and cannot
   be.** One L2 segment on 10.0.0.0/24 — the AP switches it, so it never
   reaches the forward chain. Depends entirely on **AP client isolation on the
   CF-EW73**. Unverified. Without it: ARP spoofing, MITM, device scanning.
2. **Bench mode is ON** — `iifname end0 tcp dport {22,8080} accept`. `seal.sh`
   clears it.
3. **/admin is reachable from the customer LAN** because 0 devices are
   whitelisted, and `_admin_lan_denied()` deliberately opens up on a machine
   with an empty whitelist so a fresh board cannot lock itself out. Whitelisting
   the owner's phone flips it to whitelist-only.
4. **SSH password auth is on** and `/etc/shadow` is cloned to every card —
   `seal.sh --lock-ssh`.
5. Admin password is still the install-minted one, plaintext in
   `/etc/pisowifi/config.json` (root-only).

Accepted, not fixable in software: MAC spoofing inherits a paying customer's
time; DNS is open to unpaid clients for captive-portal detection and is
tunnelable at low bandwidth.

---

## 2026-08-26 (later) — production lockdown, and anti-tethering backed out

### Done and kept

- **SSH is key-only.** `/etc/ssh/sshd_config.d/99-pisowifi.conf`, verified on a
  fresh connection before trusting it. `/etc/shadow` is cloned onto every card,
  so a password was a fleet-wide secret.
- **The plaintext admin password is out of `config.json`.** The DB holds the
  PBKDF2 hash, so the seed was dead weight — but it was the live credential in
  clear text. Reset to `changeme`, not `""`: if the hash is ever lost the
  machine must fall back to the forced-password-change flow, not to a working
  empty credential.
- **`admin_lan_access: "off"`** — a new mode. The customer network may never
  reach `/admin`, whitelist or not. Verified live: customer LAN 403, uplink 302,
  customer portal still 200.
- **`wan_admin`** — admin page only, no SSH, on the uplink port, and it survives
  sealing. Without it, `admin_lan_access=off` plus the `wan_management=off` that
  `seal.sh` forces would leave admin reachable from nowhere at all.

Note the limit honestly: Wi-Fi clients and anything cabled into the AP arrive on
the *same* interface in the *same* subnet. The box cannot tell a cable from a
radio down there. "Admin only over the cable" means the uplink port, which is
the only distinction this hardware supports.

### Latent bug found: placeholders substituted inside their own documentation

The template header named `#@ANTI_TETHER@` in prose while documenting it, and
`detect.sh` substitutes with `str.replace()` — which replaces *every*
occurrence. It hid for as long as `anti_tether` was false, because substituting
`""` into a comment is invisible. The first time the feature was switched on, a
whole nft chain was injected into that comment:

```
# from the hardware actually present. The     chain mangle_post {
        type filter hook postrouting priority mangle; policy accept;
...
    } /  /
```

`test_reachability.py` now asserts every placeholder appears exactly once and
alone on its line, and the check was negative-tested by reintroducing the bug.

### Anti-tethering: implemented, demonstrated, then backed out

Egress `ip ttl set 1` plus an opt-in strict rule dropping client packets whose
TTL shows a hotspot forwarded them.

On hardware it did discriminate correctly. The test phone showed two
populations: **TTL 64** for its own traffic (portal polls *and* its own
internet), and **TTL 63** with retransmits (same IP ID retried) for traffic
that had crossed one forwarding hop.

It was **reverted at the owner's request** before that was fully confirmed. The
strict counter reached 979 packets within minutes, and whether that was a
tethered device retrying or the customer being harmed was not yet established.
Backing out was the right call — this machine takes money, and an unproven rule
that can cut off a paying customer does not belong in front of one.

Board state after revert, verified: `mangle_post` gone, zero TTL rules in the
whole ruleset, forward chain back to its original handles, paid session
restored into the `allowed` set by reconcile (1h32m left, matching the DB), coin
input still armed via gpiod, portal 200.

`anti_tether` and `anti_tether_strict` both ship **false**. To retry: enable the
egress half alone first — it breaks hotspot sharing on its own, because the
tethered device's replies never come back, and it cannot cut anyone off.

---

## 2026-09-01 - second card, and the first fully clean bring-up

New SD card, same board (`02:81:e4:42:af:48`, so the hostname came back as
`pisowifi-42af48` -- the H3 derives its NIC MAC from the SoC serial and it
survives reflashing). Found at 192.168.254.122.

`provision.ps1 -Tailscale` ran end to end, exit 0, no manual intervention after
the SSH key was in place. That is the first time this has happened.

### The three prep fixes all earned their place

- **gpiod, not polling.** `coin input armed on physical pin 3 (PA12) via gpiod`.
  Without `python3-libgpiod` in the package list this card would have taken the
  polling fallback and said so in one line nobody reads.
- **Nothing degraded.** The tethering rules -- never syntax-checked anywhere,
  because this PC has no `nft` and WSL2 cannot start -- loaded first time on
  real nft 1.1.3. All three sets exist, so `flags dynamic` was right.
- **Interface detection handled a swapped dongle.** The LAN adapter is a
  different unit from the last board (`00:e0:4c:68:00:6f` vs `...68:06:a8`) and
  was picked up correctly, which is exactly what the old MAC-pinned udev rule
  could not do.

### Verified with real money and real kernel state

- Coin slot: 1 sale, **P5 -> 120 minutes**, matching the P5 = 2 h tier.
- **Speed limiting proven on hardware for the first time**: `htb 1:2 rate 5Mbit`
  with a CAKE leaf on the LAN, `rate 3Mbit` on ifb0. It had shipped as
  `default_speed: unlimited` since v2.3, so every machine until now ran uncapped
  despite the feature being complete.
- Tethering: detection and per-device enforcement both live. `@tether_block` is
  empty, so nothing is limited -- which is the point of scoping it per confirmed
  device instead of blanket-dropping on TTL.

### Tailscale enrolled without ever handling a credential

`tailscale up` prints a login URL; the owner approved it in their own browser.
No auth key generated, pasted, or stored. Board is `100.66.42.53` on
`tail10036d.ts.net`. `/etc/resolv.conf` confirmed untouched -- `--accept-dns=false`
matters, or MagicDNS would replace the resolver dnsmasq needs for the portal.

### Admin locked to Tailscale, verified from every side

`admin_lan_access: tailscale`. Tested from a real customer device (the laptop's
Ethernet on 10.0.0.2, which is genuinely on the customer LAN, not a simulation):

```
customer 10.0.0.2      /admin 403   /  200   /api/status 200
uplink   192.168.254.x /admin 403
tailscale 100.66.42.53 /admin 302 -> login
loopback 127.0.0.1     /admin 302 -> login
```

Owner confirmed the tunnel path works from their phone before this was trusted.
Note it blocks the **uplink** too, not just customers -- previously anyone on
the shop's router network was one password guess from the login form.

### Note for next time

`default_speed` applies to NEW sessions. An existing session keeps the speed it
was created with, so changing the setting appears to do nothing until someone
buys time again.

---

## 2026-09-01 (later) - pre-seal audit: three bugs, all fleet-wide

Full audit before sealing. Everything below was found on a card that reported
itself perfectly healthy.

### 1. systemd ordering cycle (fixed, 9de9f4f)

`pisowifi-detect` was `After=network-pre.target`; Debian's `nftables.service`
is `Before=network-pre.target`; we are `Before=nftables.service`. systemd
resolved the loop by **deleting a job**. It picked the harmless one here. It
could equally drop `pisowifi-detect`, leaving nftables to load a stale config.

### 2. systemd-networkd stealing the customer LAN (fixed, e946dd2)

The serious one. Rebooted the card and it came back with every service active
and **no customer LAN address**:

```
12:06:10  pisowifi-detect: provisioned          <- ip addr replace ran
12:06:11  enx...: Link DOWN / Lost carrier
12:06:11  Configuring with 10-netplan-all-eth-interfaces.network
```

Armbian's netplan matches `Name=e*` with DHCP=yes, which catches the customer
dongle as well as the uplink. A race, so it would hit some boots and some cards
and not others -- the worst way for a fleet to fail. detect.sh now writes
`/etc/systemd/network/05-pisowifi-lan.network` each boot and lets networkd own
the address.

### 3. Reboot button would have looked broken (fixed, 1c9fa93)

Rebooting inline kills waitress mid-response. Deferred through `systemd-run`.

### Tested as a real customer, not a simulation

The laptop's Ethernet is genuinely on 10.0.0.2, and a host route via 10.0.0.1
made its traffic actually traverse the forward chain.

| | paid | unpaid |
|---|---|---|
| internet 1.1.1.1:443 | CONNECTED, 27 ms, HTTPS 301 | BLOCKED |
| portal / api/status  | 200 | 200 |
| DNS via box          | works | works |
| /admin               | 403 | 403 |

The unpaid "ping replied" was **not** a leak: those were
`Destination port unreachable` from the box's own reject, which Windows counts
as received. A capture on the uplink saw zero ICMP escape. Worth remembering --
it looks exactly like a leak in a test that only counts replies.

### The tethering fix validated in the field

The laptop sits in `ttl_fwd` only, never `ttl_norm` -- one consistent TTL below
Windows' 128, because Norton VPN is installed. Classified `vpn_like`, **not
blocked**, and browsing normally throughout. That is precisely the device v1
cut off the internet.

### Concurrency on real hardware

- 50 simultaneous customers: 50/50 enforced in the kernel, 153 ms each, load
  0.48, 754 MB free, **zero leftover state** after teardown.
- 100 concurrent `/api/status` (the real pattern -- one poll per phone per
  second): 452 ms, all 200.
- `/admin` under 50-way load: 403 x 50. The gate does not soften under load.

### Reboot from the admin page, end to end

```
12:54:12  admin login              (owner, over Tailscale)
12:54:50  reboot requested from admin
12:55:12  boot 0 begins
```

Came back with services up, LAN addressed, coin on gpiod, Tailscale running,
admin still 403 from customers, session intact (2284 s, speed profile kept),
and shaping self-healing to 5/3 Mbit once the client sent its first packet.

### Note for next time

Two test artefacts that looked like product bugs and were not: `ping` counting
ICMP rejects as replies, and `curl` without `--interface` on a dual-homed PC
sourcing from the wrong NIC. Bind the source when testing a customer path.

---

## 2026-09-01 (evening) - card #2, and remote management proven

Reflashed after imaging master #1. `setup.ps1` built the card, then everything
below was done over Tailscale from the laptop.

### setup.ps1 had a real bug, and my first diagnosis of it was wrong

It splatted an **array** into provision.ps1. Array splatting binds
POSITIONALLY, so the literal string "-Target" went to the first positional
parameter and the IP to the second, and every `[switch]` was silently dropped:

```
ARRAY      Target = '-Target'          User = '192.168.254.122'  Tailscale = False
HASHTABLE  Target = '192.168.254.122'  User = 'root'             Tailscale = True
```

It surfaced as *"Could not install the SSH key -- check the IP and the root
password"*, which points at the network and the password, neither of which was
wrong. I first blamed the variable being named `$args`; renaming it changed
nothing and the same failure recurred. `$args` is still a bad name -- it is an
automatic variable -- but it was not the cause. Both are now SKILL.md 1f.

### Option A applied and verified: laptop + Tailscale only

Enrolled with the login-URL flow again, so no auth key was ever generated,
pasted or stored. Board is `100.84.129.18`; `/etc/resolv.conf` confirmed
untouched.

The gate before closing anything was **both** paths proven from the laptop over
the tunnel: SSH (peer IP 100.108.32.39, i.e. scarlite, not the LAN) and the
admin page. Only then were `wan_management`, `wan_ssh` and `wan_admin` all set
false with `admin_lan_access: tailscale` -- applied **through the tunnel**, so
the connection doing the work was the one that had to survive.

```
1. UPLINK CLOSED   :22 closed   :8080 closed   ping no reply
2. TAILSCALE WORKS :22 open     :8080 open     /admin 302 -> login 200
3. CUSTOMERS FINE  portal 200   /admin 403     captive detect 302
```

Check the `iifname "tailscale0" accept` rule is in the **template** and not just
the live ruleset before doing this. The re-render rebuilds from the template; a
rule that exists only in memory disappears at exactly the wrong moment.

### Remote management confirmed

12/12 deployed modules md5-match the repo, compared entirely over the tunnel.
Config edits, firewall re-render, service restarts and file verification all
work from the laptop with no path to the board on any physical network.

### State

Card #2 carries the corrected defaults (5/3 Mbit cap, tethering enforcement on)
which master #1 does not. Admin password set to the owner's choice, stored
PBKDF2, installer seed cleared from config.json.

---

## 2026-09-01 (night) - full audit of card #2, locked to Tailscale

### Information leak closed

`/admin` answered a refused customer with 403 and an explanation naming the
config file and the setting to change -- a recovery procedure handed to whoever
knocked, on top of confirming an admin panel existed. Now answered with the
same 302 to the portal that any unknown URL gets. Verified from a real customer
on 10.0.0.2: `/admin`, `/admin/login`, `/admin/diag` and `/admin/remote-access`
are byte-identical to `/nope`, and nothing leaks in body or headers.

The reason moves to the audit log with the source IP, which also makes probing
visible -- repeated `admin_denied` from one address is somebody trying doors.

`test_security` had asserted *"refusal explains the recovery path"*, an
assertion that is exactly backwards once the point is not to explain. Updated
rather than deleted.

### Audit results

```
suites          11/11
services        pisowifi/nftables/dnsmasq/tailscaled  all active, 0 restarts
resources       132/991 MB, 19% disk, load 0.01, 41 C
coin            armed via gpiod; 1 real sale of P1 -> 20 min, speed applied
kernel conc.    50/50 enforced, 154 ms each, zero leftover
http conc.      50 portal 246 ms | 100 polls 462 ms | 200 polls 3.2 s | 50 admin 302x50
customer        portal 200, /admin 302, DNS ok, captive detect 302
uplink          :22 and :8080 closed, ping dead
tunnel          admin + SSH work; board is 100.84.129.18
```

### Reboot over the tunnel, with the uplink shut

The test that matters after a power cut: with the uplink closed and admin
Tailscale-only, the tunnel is the only way back. **Board returned in 48 s**,
unattended, with LAN addressed, coin on gpiod, session and shaping intact,
0 ordering cycles.

### Three test artefacts that impersonated bugs today

Worth more than the bugs, because each cost time and each looked real:

1. `ping` counts ICMP *rejects* as replies, so an unpaid client "replied" while
   a capture on the uplink showed zero ICMP escaping.
2. `curl` without `--interface` on a dual-homed PC sources from the wrong NIC,
   making a working captive-portal redirect look like HTTP 000.
3. `grep -i failed` over a unit's journal matched `resolvconf: Failed to set DNS
   configuration`, a cosmetic message from a *different* process, and made a
   clean dnsmasq look like it had a boot race. `Result=success, NRestarts=0` is
   the answer, not a log grep.

The dnsmasq port-53 failure is real but install-time only: Debian's postinst
starts it with the stock config before ours is rendered. It does not recur at
boot and a cloned card never sees it.

---

## 2026-09-01 (late) - six features, built against failures this project has had

Each exists because of something that actually went wrong here, not because it
seemed like a good idea.

**Coin-box reconciliation.** The database knew what it credited; nothing
compared that to the box, so a coin lost between the acceptor and the owner's
hand left no trace. `collections` records each emptying with expected, counted
and the difference. One short count is a miscount; a pattern is a problem.

**Storage health** (`app/health.py`). The worst incident this project has had
was a rootfs going read-only, undetected. In a shop that is silent and
expensive: the portal serves, coins count, customers get online, and nothing is
written down. The check writes, fsyncs and deletes a probe file rather than
asking `os.access`, which returns True on a filesystem that went read-only
underneath. When it fails, `/api/insert` returns 503 and the portal disables
the button -- taking cash you cannot record is worse than turning the customer
away.

**Offsite backup over Taildrop.** A backup on the same card as the database
protects against almost nothing, since the card is the most likely thing to
fail and takes both copies. Taildrop rather than scp: no keys to distribute
between machines. Offsite copies exclude secrets, because this copy leaves the
machine.

**Fleet heartbeat enriched.** The collector already existed (server/app.py);
the heartbeat carried only earnings and uptime. Now it carries storage_ok,
coin_backend (so a machine degraded to polling is visible), unclaimed coins,
box total and last variance. Earnings alone are not enough to run a fleet on --
every silent failure here looked plausible in the takings for a while.

**Auto-block admin probers.** Denials were already audited; now N inside a
window blocks that MAC. Keyed on MAC because a customer can take a new DHCP
address in seconds. Whitelisted devices are exempt, and tested to be -- an
owner locking themselves out is exactly the own-goal this kind of feature
invites.

**Happy hour.** `set_rate` existed but was never offered in the UI. The page
now says it needs TWO jobs, one to change the rate and one to change it back; a
single job changes the rate permanently and you find out weeks later from the
takings.

Deployed over Tailscale, 8/8 files md5-verified, 12/12 suites, service clean,
collections table created, storage check reports writable, portal 200.

### A fourth test artefact

Importing `main` in a second process to inspect the heartbeat made the coin
line report `mock`: the second process tried to claim a GPIO the running
service already held, fell back to polling, then to mock. The service was on
gpiod throughout. **Inspect a running service through its own logs or API,
never by importing its module again.**

### Open items

- [ ] Redeploy `47d357f` (portal-after-paying fix) — board was off the network.
- [ ] Set the acceptor to **fast pulse** mode (gaps are 1.4-2.5 s now), or
      raise `pulse_end_gap_s` to ~3 s so one coin is one train.
- [ ] Set `coin_edge` to **rising** -- this harness is active-HIGH.
- [ ] Verify the relay actually gates the acceptor. Note the README trap:
      a WiFi5-Soft harness puts the relay on physical pin **5** (PA11),
      not 11 (PA1); config currently says 11.
- [ ] Train the coin acceptor and verify every denomination end to end.
- [ ] Set admin password, rates, hotspot name; whitelist the owner's phone.
- [ ] Then `.\provision.ps1 -Target <ip> -Seal -ZeroFill` to cut the master.
- [ ] **Unresolved:** the read-only filesystem event. Suspect the buck converter
      or the 2019 no-name SD card. Retest with a branded card and a 5 V/2 A supply.
- [ ] Root password is cloned into every card (`/etc/shadow`); treat as a fleet
      secret, or set key-only SSH before imaging.
- [ ] Branch is not merged to `master`; the prebuilt deployer `.exe` is stale.

## 2026-09-01 (later) - three field bugs, three silent failures

Owner's report from the shop, with two paid phones and a third tethered off
one of them: no sound; the tethered phone browses freely; one paid phone
capped, the other not, until a later retry. All three were features that
reported success while doing nothing. None threw.

### Speed cap: the neighbour table is a cache

`_ip_for_mac()` asked only `ip neigh`. A phone quiet for a minute shows
`10.0.0.109 FAILED` there while holding a perfectly good lease. No IP, no tc
class, no cap -- and the shaper does not complain about a session it was never
told about. Once the phone spoke and became REACHABLE the next reconcile capped
it, which is exactly "no cap, then after 2nd try it's limited". Fixed with a
fallback to `/var/lib/misc/dnsmasq.leases`: a customer cannot reach the portal
without a lease, so the lease file always knows a paying device. Verified two
HTB leaf classes where there had been one.

### Tethering: v2 could never fire, and its enforcement was dead anyway

Installed a throwaway `(MAC . TTL)` set with counters while the owner had a
phone deliberately sharing. Twelve seconds of truth:

    66:11:ee:1a:ec:6d . 63      8 packets   <- the phone
    66:11:ee:1a:ec:6d . 62   1724 packets   <- the phone behind it

The phone's OWN traffic arrives at 63. v2 decided "own traffic" by testing
`ip ttl { 64, 128, 255 }`, which therefore matched nothing for any customer;
`ttl_norm` stayed empty; and since tethering required both sets, it reported
"none detected" all evening. **Never hardcode what a normal TTL is.**

v3 keys one set on the pair -- `typeof ether saddr . ip ttl`, `counter` per
element, both supported on nft 1.1.3 -- and the app learns each device's
baseline as its own highest TTL. Anything below that, in volume (25 packets;
a VPN leak is single digits, a shared device is thousands), is sharing. No
absolute TTL anywhere in the logic. Test suite drives the real parser with
nft-format text and carries the hardware numbers as a regression.

Then detection fired and the tethered phone *still* browsed. The MAC was in
`tether_block`; the audit log said "limiting". Counters on the rule:

    oifname LAN ether daddr @tether_block ip ttl set 1   -> 0 packets
    oifname LAN ip daddr 10.0.0.100                      -> 30 packets

**`ether daddr` matches nothing in postrouting** -- the outgoing Ethernet
header does not exist yet at that hook, and nft does not say so. The rule had
been a no-op since it was written. Enforcement now writes the IP (resolved
from the lease, above) to a parallel `tether_block_ip` set and the rule
matches `ip daddr`. Every confirmed device is re-pushed each reconcile so a
lease renewal is followed. When a rule does nothing, put a counter on it
before theorising.

Owner's observation that fits: a laptop on the tethering phone's hotspot sees
the *phone's* timer on the portal. The phone NATs, so the laptop arrives as
the phone. Inherent, and the reason TTL is the only lever.

### Sound: a coin is not a gesture

Config was fine -- served `on: true`, all three clips. The gate was wrong.
`unlockAudio` was bound to `pointerdown` with `{once:true}`; the customer is
holding a coin, not the phone, so anyone who had not tapped had no context and
no clips, forever, and one failed unlock was permanent. Also: a clip the
browser refused to play `return`ed past the synth fallback with the rejection
swallowed. And on iOS the ringer switch mutes Web Audio while `<audio>` still
plays. Now: clips built at load; unlock retried on every gesture until the
context reports running; each clip primed with a silent play/pause inside the
gesture; a silent loop to move iOS onto the media channel; a "tap anywhere to
turn on sound" chip after 1.5 s if it has not unlocked. Browsers require a
gesture and nothing can bypass that; the honest fix is to ask.

### Two firewall reloads over the tunnel

Each with `nft -c` first, a `flush ruleset` + `nft list ruleset` snapshot, and
a detached 150 s auto-rollback cancelled by touching a file once the next SSH
got through. Cheap insurance for editing the firewall you are logged in
through.

### Open items

- [ ] Owner to confirm the tethered phone has lost internet with the IP rule
      live, and that one tap on the portal makes the next coin audible.
- [ ] Why the phone's own TTL is 63 with MACs preserved is not explained (not
      an L3 hop). Irrelevant to v3, which assumes nothing -- but worth a look.
