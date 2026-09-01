---
name: pisowifi
description: Hard-won rules for working on this PisoWiFi project - deploying to an Orange Pi One from Windows, editing Flask/Jinja admin templates, and changing the nftables ruleset. Load before writing any .ps1 deploy script, editing app/templates/*.html, editing network/nftables.conf, or provisioning a board. Every rule here comes from a bug that actually shipped and cost real debugging time.
---

# PisoWiFi — rules that were learned the hard way

Each rule below is a defect that reached hardware. The failure mode is given
because most of these are **silent** — no error, just something that does not
work.

---

## 1. Windows PowerShell → ssh → remote sh

This pipeline mangles text in three separate ways. It caused more lost time
than every other category combined.

### 1a. Keep `.ps1` files pure ASCII

**Fails:** an em dash (`—`, U+2014) anywhere in a `.ps1`.

Windows PowerShell 5.1 reads a script as the ANSI codepage unless the file has
a UTF-8 BOM. `—` is `E2 80 94` in UTF-8, and CP1252 maps `0x94` to a curly
closing quote `”`. Every em dash therefore **injects a stray quote** and
unbalances every string after it. The whole script fails to parse.

    FAILS   Write-Host "Installing — a few minutes"
    WORKS   Write-Host "Installing -- a few minutes"

Same trap: `…` (U+2026), curly quotes, `→`. Use ASCII in `.ps1`. Markdown,
Python and shell files are fine — they are read as UTF-8.

### 1b. Validate with `powershell.exe`, not `pwsh`

PowerShell 7 defaults to UTF-8 and parses files 5.1 rejects. Validating with
the wrong engine reports success on a script the user cannot run.

```bash
powershell.exe -NoProfile -Command "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path 'x.ps1').Path,[ref]$null,[ref]$e) | Out-Null; if($e){$e|%{$_.Message}}else{'OK'}"
```

### 1c. `$ErrorActionPreference = "Stop"` turns stderr into a fatal error

**Fails:** any native command that writes to stderr — and `ssh` and `apt-get`
both do so routinely, on success.

`ssh`'s host-key warning killed a script *at the line written to handle that
warning*. `apt-get`'s notices would have killed the install a minute later.

    FAILS   $ErrorActionPreference = "Stop"
    WORKS   $ErrorActionPreference = "Continue"   + explicit $LASTEXITCODE checks

To capture output and exit code without a throw:

```powershell
$out  = & ssh -o BatchMode=yes $dest "true" 2>&1 | Out-String
$code = $LASTEXITCODE
```

### 1d. Single-quote every remote command

PowerShell expands `$( )` and `$var` inside double quotes, and `\"` is **not**
an escape — the escape character is a backtick.

    FAILS   ssh $d "for i in \$(seq 1 10); do ...; done"     # seq runs on Windows
    FAILS   ssh $d "python3 -c 'print(\"x\")'"               # quotes stripped
    WORKS   $cmd = 'for i in $(seq 1 10); do ...; done'
            ssh $d $cmd

If the remote command needs both quote types, put it in a script file and run
that instead of fighting three layers of quoting.

### 1e. Never pipe source through `sed` on the way to a board

**Fails, silently, catastrophically:**

    find /root/pisowifi -type f -exec sed -i "s/\r$//" {} +

The backslash did not survive PowerShell → ssh → sh. The board's sed received
`s/r$//` — *delete a trailing `r` from every line*. `import shaper` became
`import shape`. **7 of 10 modules were corrupted with no error anywhere**; the
only symptom was a service restarting forever on `ModuleNotFoundError`.

`.gitattributes` already checks this tree out as LF, so there was nothing to
strip. **Verify the upload instead of transforming it:**

```powershell
$remote = ssh $dest 'md5sum /opt/pisowifi/*.py'
# compare against Get-FileHash -Algorithm MD5 locally; fail loudly on mismatch
```

### 1f. Splat a HASHTABLE, never an array

**Fails:** passing arguments between `.ps1` scripts with an array.

    FAILS   $a = @("-Target", $ip, "-User", "root", "-Tailscale")
            & .\provision.ps1 @a
    WORKS   $a = @{ Target = $ip; User = "root" }
            $a["Tailscale"] = $true
            & .\provision.ps1 @a

Array splatting passes elements **positionally**. Proven side by side:

```
ARRAY      Target = '-Target'          User = '192.168.254.122'   Tailscale = False
HASHTABLE  Target = '192.168.254.122'  User = 'root'              Tailscale = True
```

So the literal string `-Target` binds to the first positional parameter, the
value lands in the second, and every `[switch]` is **silently dropped** -- the
`-Tailscale` above never arrived. The visible symptom was an ssh to
`192.168.254.122@-Target`, reported as *"Could not install the SSH key -- check
the IP and the root password"*, which sends you to look at the network and the
password, neither of which is wrong.

Also do not name the variable `$args`: that is an automatic variable holding
the script's own unbound arguments. Renaming it does **not** fix the array
problem though -- that was a wrong first diagnosis here, and the rename alone
changed nothing.

### 1g. PowerShell 5.1 has no `&&`

    FAILS   xz -t "file" && echo OK        # 5.1: "not a valid statement separator"
    WORKS   xz -t "file"; if ($LASTEXITCODE -eq 0) { "OK" }

`pwsh` (7+) supports `&&`, Windows PowerShell 5.1 does not, so a command that
works in one terminal fails in the other. Bash paths (`/c/Users/...`) do not
work in PowerShell either, and Git's tools are not on its PATH -- `xz` lives in
`C:\Program Files\Git\mingw64\bin`. **Say which shell a command is for.**

### 1h. Heredocs in the Bash tool eat backslashes

Writing a Python script via `python - <<'PY'` loses `\\n` / `\\r`. Use the
Write or Edit tool for any content containing backslashes.

---

## 2. Flask + Jinja templates

### 2a. JSON into `<script>` needs `| tojson`

**Fails:**

    const TIERS = {{ tiers }};          <!-- with tiers = json.dumps(...) -->

Autoescaping turns every `"` into `&#34;`. Entities are **not** decoded inside
`<script>` (unlike HTML attributes, where they are), so this is a JavaScript
syntax error that kills the entire block.

    WORKS   const TIERS = {{ tiers | tojson }};   <!-- pass the raw dict -->

Pass the **object**, not `json.dumps(...)` — `tojson` does the encoding, and
double-encoding yields a JS string instead of an object.

**Why this was severe:** the dead block also held the loop that stamps the CSRF
token into every form, so *every* POST on the page silently failed the CSRF
check. Rates, hotspot name, remote settings, kick, restore — all inert, with no
error shown. The tell was an empty `settings` table in the DB.

**Therefore:** keep CSRF stamping in its own `<script>` in the base layout, so
a page script can never take it down.

### 2b. Do not write `{{ }}` inside a template comment

Jinja parses it and raises `TemplateSyntaxError`. Same for a literal `&#34;` in
a comment — it defeats any grep-based check for escaped entities.

### 2c. Test by rendering, then check the emitted JS

Rendering all templates against representative context catches syntax errors;
piping each emitted `<script>` through `node --check` catches the escaping bug
that rendering alone will not.

---

## 3. nftables (Debian 13 ships 1.1.3)

### 3a. Do not quote addresses

    FAILS   define CLIENT_NET = "10.0.0.0/24"   # Error: Could not resolve hostname
    WORKS   define CLIENT_NET = 10.0.0.0/24

nft treats a quoted value as a symbolic hostname. Debian 12's nft 1.0.6
tolerated the quotes. **Interface names stay quoted** — those really are
strings.

### 3b. An early `accept` skips every later rule in that chain

The `prerouting` chain accepts paid clients before the port-80 redirect, so a
customer who had paid could no longer reach the portal: nothing listens on :80,
and the input chain answered with `reject with tcp reset` — "refused to
connect". Anything that must always work has to sit **above** the
short-circuits:

```
iifname $LAN_IF ether saddr @blocked drop
iifname $LAN_IF ip daddr $GW_IP tcp dport 80 redirect to :$PORTAL_PORT   # always
iifname $LAN_IF ether saddr @allowed accept                              # exits here
```

### 3c. The installer can seal its own management path

`iifname $WAN_IF drop` ends the input chain, so the moment nftables loads, the
board is unreachable from the uplink — including by the script that just
installed it. `wan_management` (config toggle, `install.sh --wan-management`,
rendered via the `#@WAN_MGMT@` token) keeps SSH/portal/ICMP open for bench
work; `seal.sh` forces it off so no master image ships that way.

### 3d. Never block a client on a low TTL alone

**Fails, by cutting paying customers off the internet entirely:**

    iifname $LAN_IF ip ttl != { 64, 128, 255 } drop

The reasoning is that a hotspot decrements TTL when it forwards, so a low TTL
means a shared device. The arithmetic is right and the conclusion is wrong: **a
phone decrements its own traffic too whenever an on-device VPN is running** —
ad blockers, Private DNS, WARP and corporate VPN clients re-inject through a
local tun and the phone's own stack forwards on the way through. Every packet
that customer sends is one lower, forever.

The evidence that misleads you: the test phone answered **pings at TTL 64**
while its **TCP left at 63**. That looks like a phone plus something behind it.
Echo replies are kernel-generated and never cross the tun, so a local VPN
produces exactly that split.

**Tethering is two TTL populations from one MAC at once**, not a low one:

    64 only          the device, however it routes internally
    63 only          on-device VPN — NOT tethering, leave it alone
    64 and 63        it is forwarding for something else

Record into two sets and intersect in userspace; enforce per confirmed MAC,
never with a blanket rule.

### 3e. A set that rules add to needs `flags dynamic`

    FAILS   set ttl_norm { type ether_addr; flags timeout; timeout 10m; }
    WORKS   set ttl_norm { type ether_addr; flags dynamic, timeout; timeout 10m; }

Without it nft rejects the **whole ruleset**, not just the offending line. Sets
populated only from userspace (`nft add element`) do not need it.

**What losing the ruleset actually costs** — worth being precise, because the
obvious guess is wrong. `nftables.conf` is the only source of NAT
(`oifname $WAN_IF masquerade`, no iptables fallback), so a ruleset that will not
load does **not** mean "no firewall, everyone online free". It means no
masquerade and no portal redirect: nobody reaches the internet and nobody
reaches the portal. The machine is visibly dead rather than silently generous —
better, but still dead.

`detect.sh` therefore degrades feature by feature rather than all at once: it
`nft -c`s the full render, retries with the optional tethering rules stripped,
and only then falls back to the previous file. A fresh card has no previous
file, so "refuse and keep the old one" alone would have left it with nothing.
Verified: the stripped render is byte-identical to an `anti_tether: false`
render, which is the ruleset already proven on hardware.

### 3f. Never write a `#@TOKEN@` in prose in the template

`detect.sh` fills placeholders with `str.replace()`, which replaces **every**
occurrence. The header comment documented `#@ANTI_TETHER@` by naming it, so the
mention was substituted too.

It hid indefinitely, because while the feature was off the replacement was `""`
and blanking part of a comment changes nothing. The first time anyone enabled
it, a whole nft chain was injected into the comment and the ruleset stopped
parsing -- i.e. the bug only existed once the feature was switched on.

    FAILS   # The #@ANTI_TETHER@ / #@FLOWTABLE@ tokens are filled in ...
    WORKS   # The ANTI_TETHER / FLOWTABLE placeholders are filled in ...

`tests/test_reachability.py` asserts every placeholder appears exactly once and
alone on its line. That check was negative-tested; a guard nobody has watched
fail is not a guard.

### 3g. Reloading nftables flushes the runtime sets

`systemctl restart nftables` reloads `/etc/nftables.conf`, which recreates
`allowed` / `whitelist` / `blocked` **empty** -- every paying customer is
disconnected at that instant. The app's reconcile pass puts them back (verified:
a session with 1h32m left returned intact, matching the DB), but anything that
reloads the ruleset must be followed by a reconcile, not left to the next timer
tick.


---

## 4. Armbian / Orange Pi One bring-up

- **The autoconfig profile applies at first *login*, not first boot.**
  `/root/.not_logged_in_yet` is consumed by the first-login wizard. Until
  someone logs in, root is on Armbian's default **`1234`**, not the profile
  password.
- **The wizard blocks automation.** It re-runs on every login until it
  completes, so every SSH command lands in a password prompt. If it fails
  (it rejects an illegal username and gives up), delete the trigger:
  `rm -f /root/.not_logged_in_yet`. That file also holds both passwords in
  plain text, so `seal.sh` removes it too.
- **The onboard NIC is `end0`, not `eth0`,** on this Armbian/kernel. Detect
  interfaces by bus type (`device/modalias` starts `usb:` vs `of:`/`platform:`)
  and prefer whichever already holds the default route.
- **A recycled DHCP address breaks SSH automation.** A fresh flash mints new
  host keys; if the router reuses the address, ssh aborts with
  `REMOTE HOST IDENTIFICATION HAS CHANGED`. `StrictHostKeyChecking=accept-new`
  does **not** cover a *changed* key — only an unknown one. Detect that string
  and run `ssh-keygen -R <ip>`.
- **Windows cannot provision the card offline.** Armbian sunxi images are a
  single ext4 partition (no FAT boot partition), and WSL1 cannot `wsl --mount`.
  There is no offline path; the board must boot and be reached over the network.

---

## 5. GPIO on Debian 13

### 5a. `add_event_detect()` arms cleanly and never fires — NEVER use it

**This is the worst bug this project has had.** The machine took coins and gave
nothing back, and every diagnostic said it was healthy.

`OPi.GPIO.add_event_detect()` uses the **deprecated sysfs edge interface**.
Kernel 6.18.44-current-sunxi accepts the arming, writes `edge=falling`, keeps
the fd open — and delivers no interrupt, ever. There is no exception, no log
line, no restart, no degraded flag. `/sys/class/gpio` still *exists* on Trixie,
which is what made this so slow to find: the earlier note here said "coins
count, Trixie is fine", and that was wrong.

Everything below was true **while not one coin was being counted**:

```
systemctl is-active pisowifi   -> active, 0 restarts
/sys/class/gpio/gpio12         -> direction=in, edge=falling
/proc/<pid>/fd                 -> open fd on gpio12/value
slot.mock                      -> False, gpio_error None
```

    FAILS   GPIO.add_event_detect(pin, edge, callback=..., bouncetime=...)
    WORKS   gpiod.request_lines(..., LineSettings(edge_detection=Edge.BOTH))
            or level-polling /sys/class/gpio/gpioN/value at 1 ms

**How to catch this class of bug:** poll the pin's `value` file from a separate
process while the app runs. If the line transitions and the app's counter does
not, the delivery mechanism is dead, not the hardware. That one test separated
"broken acceptor" from "broken kernel API" in a single coin drop.

### 5b. A sysfs export holds the line against libgpiod

`/sys/class/gpio/gpioN` **outlives the process that created it**. While it
exists, `gpiod.request_lines()` on that line returns `OSError: [Errno 16]
Device or resource busy`.

This bites exactly once and then hides: the first restart after switching to
libgpiod falls back to polling, logs one line, and works — so the machine looks
fine and never uses the good backend again. Unexport before requesting.

### 5c. Physical pin != sysfs number != libgpiod offset

`HEADER` in `diagnostics.py` maps physical pin -> `PA12`-style name. For
gpiochip0 (H3, 224 lines, PA..PG at 32 per bank) the sysfs number and the
libgpiod offset are both `bank*32 + n`, so `PA12` is `12` for both — but only
because the coin pin is in bank A. Do not generalise that to PC/PD pins.

### 5d. Guard the arming path — it runs at import time

`CoinSlot._setup_gpio()` runs at **import time** and only `ImportError` was
caught. The library can import fine and still throw `OSError` against the
kernel's GPIO layout (this H3 exposes both `gpiochip0` and `gpiochip352`). An
escaping exception there kills the entire app — portal, admin, vouchers,
sessions — over a coin acceptor. Catch it, fall back, log the reason.

### 5e. Read the signal before trusting the config

Measured on this board: the coin line **idles LOW and pulses HIGH** for ~50 ms,
the opposite of the README's documented PC817 + pull-up wiring. `falling` still
works (it catches the trailing edge) so it hid, but `rising` is correct.

Count **both** edge directions and show them separately. A line pulsing the
opposite way to `coin_edge` is otherwise indistinguishable from a dead
acceptor, and both look like "the coin slot is broken".

Also measured: 1.4–2.5 s between pulses against `pulse_end_gap_s = 0.7` — the
acceptor is in slow-pulse mode, so one ₱5 coin arrives as five ₱1 trains. The
totals survive only because the denomination table is linear. That is luck.

---

## 6. Money safety — invariants that must never regress

This machine takes cash. Anything that can silently destroy or misroute a coin
outranks every other bug.

- **A coin must never be destroyed.** A pulse train completing with no insert
  window open used to set `credited = False` and discard the value: no session,
  no sale, no record but a diagnostics field. Coins now go to a pending pot
  (`CoinSlot.pending_pesos`), are claimed by the next window within
  `uncredited_hold_s`, and are shown on the portal. **The relay is a mitigation,
  not the mechanism** — it is optional, it can fail, and software must not
  assume it is fitted.
- **A coin inserted during another customer's window goes to that customer.**
  Inherent: the acceptor reports pulses, not who dropped them. Only the relay
  closes this. Do not pretend a code change fixes it.
- **`apply_credit()` is not the coin path.** It is the software entry point;
  the real path is `_pulse_cb -> _watcher -> window`. A test that calls
  `apply_credit` directly proves nothing about coins — that gap is exactly how
  the destroyed-coin bug survived an earlier "money safety" test.
- Credit is **asynchronous**: the train only completes after
  `pulse_end_gap_s`. Tests must wait for it.

## 7. Security invariants

The threat model is a customer standing in the shop with a phone.

- **Customers get the internet, not the owner's network.** The uplink plugs
  into the owner's home router, so without an explicit block every paying
  client is inside that LAN -- router admin page, NAS, CCTV, PCs. The forward
  chain rejects `$PRIVATE_NETS` (RFC1918 + 169.254 + 127/8 + CGNAT). Two
  escape hatches sit ABOVE the block and must stay there: `@whitelist` (the
  owner's own devices) and `@walled` (deliberately allowed local hosts).
- **IPv6 forwarding is off on purpose.** The isolation rules are IPv4-first;
  enabling `net.ipv6.conf.all.forwarding` routes straight around them.
- **Customer-to-customer is not this machine's to filter.** Same subnet means
  switched, not routed -- nftables never sees it. AP Isolation on the antenna
  is the only control. Documented in README section 1b.
- **Downloadable backups must not carry credentials.** `_SECRET_SETTINGS`
  strips `SECRET_KEY`, `remote_key`, `admin_pw_hash`, `admin_pw_default` and
  `admin_password`. The on-box scheduled backup keeps them
  (`include_secrets=True`) so a local restore stays complete.
- Accepted and not fixable here: MAC spoofing inherits paid time, and DNS to
  the box is a low-bandwidth tunnel that captive-portal detection requires.

`tests/test_security_audit.py` asserts all of this from the customer's side.

### 7a. A model that widens on an unfamiliar rule is worse than no model

`tests/test_reachability.py` knew `iifname $LAN_IF`, `$WAN_IF` and `"lo"`. A
rule naming anything else matched none of its clauses and fell through to
"matches everything", so `iifname "tailscale0" accept` read as a blanket accept
and the suite cheerfully reported that unpaid customers could reach SSH.

It failed loudly, which is the only reason it was caught. Match literal
interface names generically, and be suspicious of any model whose default for
an unrecognised construct is permissive.

### 7b. Policy that cannot survive cloning must be reset by `seal.sh`

`admin_lan_access: "tailscale"` locks admin to the tunnel. A cloned card has no
Tailscale identity — sealing wipes `/var/lib/tailscale`, and it must, or every
card claims one node and they knock each other offline — and the only way to
enrol one is the admin page. So the card boots unreachable, unbootstrappable,
and with no SSH.

The test for anything in the settings table: *if a fresh card inherited this,
could its new owner still get in?* If not, it belongs in `seal.sh`'s reset list
alongside the identity keys.

### 7c. Never gate the customer portal

Whatever admin is locked behind, `/`, `/api/status` and `/buy` must stay
reachable from the hotspot. Gate those and customers cannot insert coins: the
machine keeps running, looks healthy, and silently stops earning.
`tests/test_admin_gate.py` asserts this in the most restrictive mode.

---

## 8. Rates, and what survives a restart

### 8a. `bonus_tiers` stores totals but MEANS rates

    stored   {"5": 120}      = five pesos buys two hours
    means    24 min per peso

An amount between tiers earns the **better tier's rate**, not the base rate.
`minutes_for()` takes the max of the base rate and every tier at or below the
amount, so the multiplier can only grow with the amount — **more money can
never buy less time**, whatever is typed into the table.

    FAILS   exact-match lookup + linear fallback
            base 12, tier {5:120}  ->  P5 = 2h, P6 = 1h12  (customer loses 48 min)
    WORKS   best rate at or below the amount
            base 12, tier {5:120}  ->  P5 = 2h, P6 = 2h24

This reproduces WiFi5-Soft exactly across P1–P30, including its P25 = 24 h day
pass. Its `minute` field is a per-peso multiplier applied to the highest tier
≤ the amount — proven in `PISOWIFI_RATE_FORMULA.md` against 10,860 of that
machine's session logs. Every RE report, **and my own first reading**, took it
as a total; that would mean P20 buys less than P5. `57.6 × 25 = 1440` is the
tell.

### 8b. Restart is a state transition, on both sides

Anything held only in RAM, or computed from the wall clock, breaks quietly when
the board or a client reboots. This machine has a scheduled nightly reboot, so
"rare" is the wrong model.

- **Held coins must be persisted.** The pending pot is real money; RAM-only
  meant a power cut destroyed it — the exact bug the pot was built to fix.
- **There is no RTC.** The clock resumes from a saved value and jumps when NTP
  lands. Session expiry is absolute, so `expires_at - now` against a pre-sync
  clock is wrong for every customer. `pisowifi.service` orders after
  `time-sync.target`, and a checkpoint written each reconcile pass detects time
  going backwards — surfaced on the dashboard, never silent.
- **tc state belongs to the interface.** Recreating the LAN device (a dongle
  unplug will do it) destroys the whole shaping tree. `limit()` then adds
  classes under a parent that no longer exists and every unchecked tc call
  fails quietly. `shaper.ensure_setup()` rebuilds the root only when it is
  genuinely missing, since `setup()` tears down every client class.
- **`_ip_for_mac()` returns None right after any reboot** — the neighbour table
  is empty until the client sends a packet. A normal transient, but it must be
  reported as pending rather than assumed applied.
- **Verify, do not assume.** `shaper.limit()` reads the kernel back and returns
  whether the class exists; the dashboard counts what the kernel is doing, not
  what was asked for.

Verified on hardware: reboot with a live session, a held coin and a cap in
force — session time correct to the second, ₱7 restored, shaping rebuilt, zero
failures logged.

---

## 9. Testing this project

`pytest tests` **runs nothing**. The files are standalone scripts that call
`sys.exit()` at module level, so pytest aborts collection with an
INTERNALERROR and reports success having tested nothing. Use:

```bash
python tests/run_all.py
```

Two suites are worth knowing about:

- `test_reachability.py` walks the real `nftables.conf` and evaluates packets
  against it, asserting who can reach what in each customer state
  (unpaid / paid / whitelisted / blocked). Rule-ordering bugs are invisible in
  a diff and this is the only thing that catches them. It models **listeners**
  too: being permitted through to a port nothing is bound to is still a
  failure, and that distinction is what revealed the portal bug also broke
  whitelisted devices.
- `test_functional.py` drives every customer and admin feature through the
  Flask test client, including 10 and 50 concurrent customers.

Concurrency is fine as it stands: 50 simultaneous customers complete in ~1.6 s
with no thread errors and no lost sales. The per-thread sqlite connection cache
in `db.py` is sound; do not "fix" it.

---

## 10. Verification discipline

The recurring theme: **every one of these failed silently.** Rules that follow
from that:

1. **Check what landed, not what you sent.** Checksum deployed files against
   local source.
2. **Print the log at the moment of failure.** A firewall or a crash can make
   the machine unreachable seconds later; telling the operator to "go and look"
   assumes they still can.
3. **A working service is not a working feature.** The portal answered HTTP 200
   while every form on it was inert, and the coin slot can run mocked while
   looking perfectly healthy. Assert the specific behaviour.
4. **Capture kernel logs to tmpfs during risky work** —
   `systemd-run --unit=dmesgcap --property=StandardOutput=file:/run/dmesg-live.log dmesg --follow`
   survives a read-only remount, which is exactly when the evidence is lost.
5. **Match the user's environment when validating** — PS 5.1 vs 7, the board's
   nft version vs the docs'.

---

## Keeping this current

This file is maintained, not archaeology. When something in this project fails
in a way that was not obvious beforehand:

1. Add the rule here, in the **failing form / working form** shape used above,
   and say what the *symptom* was — almost everything in this project fails
   silently, so the symptom is the part that saves the next hour.
2. Append the incident to [`docs/PROJECT-LOG.md`](../../../docs/PROJECT-LOG.md)
   with enough evidence to justify the rule.
3. Only promote something here once it has actually bitten. Speculative advice
   dilutes the signal.

---

## Board facts (this deployment)

- Xunlong Orange Pi One, Armbian 26.8.1 trixie, kernel 6.18.44-current-sunxi, armv7l, 991 MB RAM
- `end0` = uplink (onboard), `enx…` = customer LAN (USB dongle), hostapd off
- Portal `10.0.0.1:8080`; SSH blocked from both interfaces unless whitelisted or `wan_management` is on
- Deploy: `.\provision.ps1 -Target <ip>` from the worktree; `-Seal -ZeroFill` to build a master image
