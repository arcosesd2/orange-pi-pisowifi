# PisoWiFi -- build machine #1 in one command.
#
#   .\provision.ps1 -Target 192.168.1.50
#
# Machine #1 is the only one that needs this. It installs the software, then
# checks the things that actually go wrong (wrong NIC picked, no production
# server, coin GPIO unavailable) and tells you plainly whether the board is
# ready. Once it is, seal it with -Seal and every later card is insert-and-go.
#
#   .\provision.ps1 -Target 192.168.1.50 -Seal    # ...then turn it into a master
#
# Handles the SSH key itself: generates one if you have none, installs it on
# the board if it isn't there yet (that is the one time you type the root
# password you set in the Armbian first-boot profile).
param(
    [string]$Target,
    [string]$User = "root",
    [switch]$Seal,
    [switch]$ZeroFill
)
# NOT "Stop". Windows PowerShell turns anything a native command writes to
# stderr into an ErrorRecord, so with Stop the script dies on ssh's own warning
# text and on every apt-get notice during the install -- neither of which is a
# failure. Correctness comes from checking $LASTEXITCODE after each step and
# throwing explicitly, which is what this script does throughout.
$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Find the board if no IP was given
# ---------------------------------------------------------------------------
# The board takes a DHCP lease from your router and there is no way to know the
# address in advance, so without this you have to go digging in the router's
# admin page. Sweep the local /24 for anything answering on SSH instead.
function Find-Board {
    $me = Get-NetIPAddress -AddressFamily IPv4 |
          Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
          Select-Object -First 1
    if (-not $me) { throw "No usable network interface found." }
    $prefix = ($me.IPAddress -split '\.')[0..2] -join '.'
    Write-Host "==> Scanning $prefix.0/24 for boards (SSH)..." -ForegroundColor Cyan

    $gw = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
           ForEach-Object { $_.NextHop }) -join " "
    $tasks = @()
    foreach ($i in 1..254) {
        $ip = "$prefix.$i"
        if ($ip -eq $me.IPAddress) { continue }
        $c = [System.Net.Sockets.TcpClient]::new()
        $tasks += [pscustomobject]@{ IP = $ip; Client = $c; Async = $c.BeginConnect($ip, 22, $null, $null) }
    }
    Start-Sleep -Milliseconds 1200
    $found = @()
    foreach ($t in $tasks) {
        if ($t.Async.IsCompleted -and $t.Client.Connected) { $found += $t.IP }
        try { $t.Client.Close() } catch {}
    }
    # The router answers SSH on plenty of home setups; it is never the board.
    $found = $found | Where-Object { $gw -notmatch [regex]::Escape($_) }

    if ($found.Count -eq 0) {
        throw "No SSH host found on $prefix.0/24. Give it another minute to boot, check the ethernet cable, or pass -Target <ip>."
    }
    if ($found.Count -eq 1) {
        Write-Host "    found $($found[0])" -ForegroundColor Green
        return $found[0]
    }
    Write-Host "    more than one SSH host answered:" -ForegroundColor Yellow
    $found | ForEach-Object { Write-Host "      $_" }
    throw "Pick the board and re-run with -Target <ip>."
}

if (-not $Target) { $Target = Find-Board }
$src  = $PSScriptRoot
$dest = "$User@$Target"
$key  = "$env:USERPROFILE\.ssh\id_ed25519"

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN  $msg" -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "    FAIL  $msg" -ForegroundColor Red }

$problems = @()

# ---------------------------------------------------------------------------
# 1. SSH access
# ---------------------------------------------------------------------------
Say "Checking SSH access to $dest"
if (-not (Test-Path "$key.pub")) {
    Say "No SSH key found -- generating one"
    ssh-keygen -t ed25519 -N '""' -f $key | Out-Null
}

function Test-KeyAuth {
    # Returns the combined output and the real exit code, without letting ssh's
    # stderr chatter become a PowerShell error.
    $out = & ssh -o BatchMode=yes -o ConnectTimeout=10 `
                 -o StrictHostKeyChecking=accept-new $dest "true" 2>&1 | Out-String
    return [pscustomobject]@{ Output = $out; Code = $LASTEXITCODE }
}

# A freshly flashed board generates its own host keys, and DHCP hands out
# addresses that other machines used before it -- so a "HOST IDENTIFICATION HAS
# CHANGED" warning here is the normal case, not an attack, and it blocks the
# connection outright (accept-new only covers unknown hosts, not changed ones).
# Drop the stale record for this address only, and say so.
$probe = Test-KeyAuth
if ($probe.Output -match "REMOTE HOST IDENTIFICATION HAS CHANGED") {
    Warn "known_hosts has an old key for $Target (that address was used by"
    Warn "another machine before this board). Removing just that entry."
    & ssh-keygen -R $Target 2>&1 | Out-Null
    $probe = Test-KeyAuth
}
if ($probe.Code -ne 0) {
    Say "Installing your SSH key on the board"
    Write-Host "    Type the ROOT password from your Armbian first-boot profile." -ForegroundColor Yellow
    Get-Content "$key.pub" | ssh -o StrictHostKeyChecking=accept-new $dest `
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the SSH key -- check the IP and the root password." }
    $probe = Test-KeyAuth
    if ($probe.Code -ne 0) { throw "Key installed but key-auth still fails; is PermitRootLogin enabled?" }
}
$model = (ssh $dest "cat /proc/device-tree/model 2>/dev/null | tr -d '\0'") -join ""
Ok "connected -- $(if ($model) { $model } else { 'board model unknown' })"

# ---------------------------------------------------------------------------
# 2. Install
# ---------------------------------------------------------------------------
Say "Copying the project"
ssh $dest 'rm -rf /root/pisowifi.new && mkdir -p /root/pisowifi.new'
scp -q -r "$src\app" "$src\network" "$src\systemd" "$src\install.sh" "$src\seal.sh" "$src\README.md" "${dest}:/root/pisowifi.new/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
# There is deliberately NO CRLF-strip here any more. It used to read:
#     find /root/pisowifi -type f -exec sed -i "s/\r$//" {} +
# By the time that reached the board's sed the backslash was gone, leaving
# s/r$// -- "delete a trailing r from every line". It rewrote "import shaper"
# as "import shape" and damaged 7 of the 10 modules, silently: no error
# anywhere, the app just died at import on the board.
# .gitattributes checks this tree out as LF, so there is nothing to strip, and
# the checksum comparison below catches it if that ever stops being true.
ssh $dest 'rm -rf /root/pisowifi && mv /root/pisowifi.new /root/pisowifi'

Say "Running the installer (this pulls packages -- a few minutes)"
# --wan-management keeps SSH reachable from the uplink. Without it the
# firewall seals this interface the moment nftables loads, and every check
# below times out against a board that is actually fine. seal.sh turns it
# back off, so it never reaches a master image.
ssh $dest "cd /root/pisowifi && bash install.sh --yes --wan-management"
if ($LASTEXITCODE -ne 0) { throw "installer failed -- see the output above" }

# ---------------------------------------------------------------------------
# 3. The checks that actually matter
# ---------------------------------------------------------------------------
Say "Verifying"

# Did the code that landed on the board match what left this PC? A transport
# that quietly rewrites source is not hypothetical -- the CRLF-strip removed
# above corrupted 7 of 10 modules, and the only symptom was a service that
# would not start. Compare checksums so that failure can never be silent again.
$localHashes = @{}
Get-ChildItem "$src\app" -Filter *.py | ForEach-Object {
    $localHashes[$_.Name] = (Get-FileHash $_.FullName -Algorithm MD5).Hash.ToLower()
}
$remoteRaw = ssh $dest 'md5sum /opt/pisowifi/*.py 2>/dev/null'
$remoteHashes = @{}
foreach ($line in $remoteRaw) {
    if ($line -match '^([0-9a-f]{32})\s+\S*/([^/]+\.py)$') {
        $remoteHashes[$Matches[2]] = $Matches[1]
    }
}
$corrupt = @()
foreach ($name in $localHashes.Keys) {
    if ($remoteHashes[$name] -ne $localHashes[$name]) { $corrupt += $name }
}
if ($corrupt.Count -gt 0) {
    Bad "upload does NOT match source: $($corrupt -join ', ')"
    $problems += "These files differ from the local source after upload: $($corrupt -join ', '). The deployed code is not the code you wrote."
} else {
    Ok "all $($localHashes.Count) modules match the local source"
}

# Which NIC became what. Getting this backwards is the single most common way
# a board comes up looking fine and serving nobody.
$detect = (ssh $dest "pisowifi-detect.sh --dry-run 2>&1 | head -2") -join "`n"
Write-Host $detect
if ($detect -match "no LAN interface found") {
    $problems += "No customer LAN adapter. Plug in the antenna's USB-ethernet dongle (or a WiFi adapter) and re-run."
    Bad "no LAN interface"
} else { Ok "interfaces detected" }

# waitress: without it the app silently falls back to the Flask dev server,
# which stalls under real client load.
ssh $dest "python3 -c 'import waitress' 2>/dev/null"
if ($LASTEXITCODE -ne 0) {
    $problems += "waitress is missing -- the app is on the Flask dev server, which is not fit for live use. Fix: ssh $dest 'pip3 install --break-system-packages waitress' then reboot."
    Bad "waitress not installed"
} else { Ok "waitress (production server) installed" }

# Coin GPIO. On newer kernels (Debian 13 / Trixie) OPi.GPIO's sysfs interface
# may be gone, and the app falls back to mock -- it looks healthy but will
# never count a coin.
# Single-quoted for the same reason as the portal check: PowerShell mangles
# escaped quotes inside a double-quoted string, and the command that reached
# the board was malformed -- reporting GPIO broken on a board where it works.
$gpioCmd = 'python3 -c "import orangepi.one, OPi.GPIO; print(chr(114)+chr(101)+chr(97)+chr(108))" 2>/dev/null'
$gpio = (ssh $dest $gpioCmd) -join ""
if ($gpio -ne "real") {
    $problems += "GPIO library unavailable -- coins will NOT be counted (the app runs mocked). This is the known Debian 13 risk; see the libgpiod note in app/coinslot.py."
    Bad "OPi.GPIO not usable -- coin counting will not work"
} else { Ok "GPIO library available" }

$svc = (ssh $dest "systemctl is-active pisowifi pisowifi-detect nftables dnsmasq | tr '\n' ' '") -join ""
if ($svc -match "inactive|failed") {
    Bad "services: $svc"
    # Print the failure here rather than telling the operator to go and look.
    # The firewall can seal this board off seconds later, and then nobody can
    # go and look -- this is the one moment the logs are reachable.
    Warn "last 25 log lines from pisowifi:"
    ssh $dest 'journalctl -u pisowifi -n 25 --no-pager 2>&1 | tail -25' |
        ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    $problems += "A service is not running: $svc  (log printed above)"
} else { Ok "services: $svc" }

# A mocked coin slot is not a crash, so nothing above catches it -- the portal
# serves happily and silently never counts a coin.
$mockWarn = (ssh $dest "journalctl -u pisowifi -b --no-pager 2>/dev/null | grep -m1 'coinslot: GPIO unavailable'") -join ""
if ($mockWarn) {
    $problems += "The coin slot armed as MOCK, so coins will not be counted: $mockWarn"
    Bad "coin slot is mocked -- $mockWarn"
}

# Single-quoted: PowerShell expands $( ) inside double quotes, so a double-quoted
# version of this ran seq and curl on Windows instead of sending them to the
# board. Backslash is not a PowerShell escape either -- only the backtick is.
$portalCmd = 'for i in $(seq 1 10); do c=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/); [ "$c" = 200 ] && break; sleep 1; done; echo "$c"'
$http = (ssh $dest $portalCmd) -join ""
if ($http -ne "200") {
    $problems += "The portal did not answer (HTTP $http)."
    Bad "portal HTTP $http"
} else { Ok "portal answering" }

# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------
Write-Host ""
if ($problems.Count -gt 0) {
    Write-Host "NOT READY -- fix these first:" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "  * $p" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Do NOT seal this card until they are resolved." -ForegroundColor Red
    exit 1
}

Write-Host "MACHINE #1 IS UP." -ForegroundColor Green
Write-Host ""
Write-Host "  Admin:  http://$Target`:8080/admin   (password was printed above)"
Write-Host ""
Warn "Bench mode: SSH and the portal are open on the uplink interface."
Warn "That is how this script can still reach the board. -Seal clears it."
Write-Host ""
Write-Host "Still to do by hand -- neither can be automated:"
Write-Host "  1. Train the coin acceptor, then verify with real coins in"
Write-Host "     Admin -> Diagnostics -> coin pulse monitor."
Write-Host "  2. Set the admin password, rates and hotspot name in Admin,"
Write-Host "     and whitelist your phone."
Write-Host ""

if (-not $Seal) {
    Write-Host "When it all works, make the master card:"
    Write-Host "  .\provision.ps1 -Target $Target -Seal -ZeroFill"
    exit 0
}

# ---------------------------------------------------------------------------
# 5. Seal into a master image
# ---------------------------------------------------------------------------
Write-Host "SEAL: this WIPES the sales database, admin password, SSH host keys" -ForegroundColor Yellow
Write-Host "      and logs on $Target, then powers it off." -ForegroundColor Yellow
$a = Read-Host "Type SEAL to continue"
if ($a -ne "SEAL") { Write-Host "aborted"; exit 1 }

$sealArgs = "--yes"
if ($ZeroFill) { $sealArgs = "--yes --zero" }
Say "Sealing (the board cuts the connection when it powers off -- that is expected)"
ssh $dest "cd /root/pisowifi && bash seal.sh $sealArgs" 2>$null

Write-Host ""
Write-Host "SEALED. Pull the SD card and read it to an .img:" -ForegroundColor Green
Write-Host "  deployer\dist\PisoWiFi-Deployer.exe -> SD Card tab -> READ / CLONE"
Write-Host "  (or USBImager -> Read)"
Write-Host ""
Write-Host "That .img is your master. Write it to any card: insert, power on,"
Write-Host "connect a phone to the hotspot, open Admin, log in with 'changeme'."
