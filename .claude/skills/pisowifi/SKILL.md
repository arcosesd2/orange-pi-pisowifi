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

### 1f. Heredocs in the Bash tool eat backslashes

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

## 5. GPIO on Debian 13 — the feared problem did *not* happen

Kernel **6.18.44-current-sunxi still exposes `/sys/class/gpio`**, and
`OPi.GPIO` arms pin 3 with edge detection successfully. Coins count. Do not
assume Trixie breaks the coin slot.

It is still worth guarding: `CoinSlot._setup_gpio()` runs at **import time**
and only `ImportError` was caught. The library can import fine and still throw
`OSError` against the kernel's GPIO layout (this H3 exposes both `gpiochip0`
and `gpiochip352`). An escaping exception there kills the entire app — portal,
admin, vouchers, sessions — over a coin acceptor. Catch it, fall back to mock,
log the reason.

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

## 8. Testing this project

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

## 9. Verification discipline

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
