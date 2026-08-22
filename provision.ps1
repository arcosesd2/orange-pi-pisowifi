# PisoWiFi — build machine #1 in one command.
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
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$User = "root",
    [switch]$Seal,
    [switch]$ZeroFill
)
$ErrorActionPreference = "Stop"
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
    Say "No SSH key found — generating one"
    ssh-keygen -t ed25519 -N '""' -f $key | Out-Null
}

ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $dest "true" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "Installing your SSH key on the board"
    Write-Host "    Type the ROOT password from your Armbian first-boot profile." -ForegroundColor Yellow
    Get-Content "$key.pub" | ssh -o StrictHostKeyChecking=accept-new $dest `
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the SSH key — check the IP and the root password." }
    ssh -o BatchMode=yes -o ConnectTimeout=10 $dest "true"
    if ($LASTEXITCODE -ne 0) { throw "Key installed but key-auth still fails; is PermitRootLogin enabled?" }
}
$model = (ssh $dest "cat /proc/device-tree/model 2>/dev/null | tr -d '\0'") -join ""
Ok "connected — $(if ($model) { $model } else { 'board model unknown' })"

# ---------------------------------------------------------------------------
# 2. Install
# ---------------------------------------------------------------------------
Say "Copying the project"
ssh $dest 'rm -rf /root/pisowifi.new && mkdir -p /root/pisowifi.new'
scp -q -r "$src\app" "$src\network" "$src\systemd" "$src\install.sh" "$src\seal.sh" "$src\README.md" "${dest}:/root/pisowifi.new/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
ssh $dest 'rm -rf /root/pisowifi && mv /root/pisowifi.new /root/pisowifi && find /root/pisowifi -type f -exec sed -i "s/\r$//" {} +'

Say "Running the installer (this pulls packages — a few minutes)"
ssh $dest "cd /root/pisowifi && bash install.sh --yes"
if ($LASTEXITCODE -ne 0) { throw "installer failed — see the output above" }

# ---------------------------------------------------------------------------
# 3. The checks that actually matter
# ---------------------------------------------------------------------------
Say "Verifying"

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
    $problems += "waitress is missing — the app is on the Flask dev server, which is not fit for live use. Fix: ssh $dest 'pip3 install --break-system-packages waitress' then reboot."
    Bad "waitress not installed"
} else { Ok "waitress (production server) installed" }

# Coin GPIO. On newer kernels (Debian 13 / Trixie) OPi.GPIO's sysfs interface
# may be gone, and the app falls back to mock — it looks healthy but will
# never count a coin.
$gpio = (ssh $dest "python3 -c 'import orangepi.one, OPi.GPIO; print(\"real\")' 2>/dev/null") -join ""
if ($gpio -ne "real") {
    $problems += "GPIO library unavailable — coins will NOT be counted (the app runs mocked). This is the known Debian 13 risk; see the libgpiod note in app/coinslot.py."
    Bad "OPi.GPIO not usable — coin counting will not work"
} else { Ok "GPIO library available" }

$svc = (ssh $dest "systemctl is-active pisowifi pisowifi-detect nftables dnsmasq | tr '\n' ' '") -join ""
if ($svc -match "inactive|failed") {
    $problems += "A service is not running: $svc  (check: ssh $dest 'journalctl -u pisowifi -n 50')"
    Bad "services: $svc"
} else { Ok "services: $svc" }

$http = (ssh $dest "for i in \$(seq 1 10); do c=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/); [ \$c = 200 ] && break; sleep 1; done; echo \$c") -join ""
if ($http -ne "200") {
    $problems += "The portal did not answer (HTTP $http)."
    Bad "portal HTTP $http"
} else { Ok "portal answering" }

# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------
Write-Host ""
if ($problems.Count -gt 0) {
    Write-Host "NOT READY — fix these first:" -ForegroundColor Red
    foreach ($p in $problems) { Write-Host "  * $p" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Do NOT seal this card until they are resolved." -ForegroundColor Red
    exit 1
}

Write-Host "MACHINE #1 IS UP." -ForegroundColor Green
Write-Host ""
Write-Host "  Admin:  http://$Target`:8080/admin   (password was printed above)"
Write-Host ""
Write-Host "Still to do by hand — neither can be automated:"
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
Say "Sealing (the board cuts the connection when it powers off — that is expected)"
ssh $dest "cd /root/pisowifi && bash seal.sh $sealArgs" 2>$null

Write-Host ""
Write-Host "SEALED. Pull the SD card and read it to an .img:" -ForegroundColor Green
Write-Host "  deployer\dist\PisoWiFi-Deployer.exe -> SD Card tab -> READ / CLONE"
Write-Host "  (or USBImager -> Read)"
Write-Host ""
Write-Host "That .img is your master. Write it to any card: insert, power on,"
Write-Host "connect a phone to the hotspot, open Admin, log in with 'changeme'."
