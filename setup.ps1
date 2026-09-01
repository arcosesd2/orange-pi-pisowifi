# PisoWiFi -- one command from "board plugged in" to "machine ready".
#
#   .\setup.ps1                       # find the board, set it up, verify
#   .\setup.ps1 -Tailscale            # ...and install Tailscale for remote admin
#   .\setup.ps1 -Target 192.168.1.50  # skip discovery
#
# This wraps provision.ps1 with the two things it does not do: finding the
# board without being told where it is, and waiting out the one step that
# cannot be automated from Windows.
#
# WHY ONE STEP STAYS MANUAL
#
# An Armbian card for this board has a single ext4 partition -- u-boot lives in
# raw sectors and /boot is inside the rootfs. Windows cannot read or write ext4
# without WSL2 or a third-party driver, so there is no FAT partition to drop an
# armbian_first_run.txt onto and no way to plant an SSH key before first boot.
# Until someone logs in once, root is on Armbian's default 1234, the first-login
# wizard intercepts every session, and no automation can get past it.
#
# So this script does everything either side of that: it finds the board, tells
# you exactly what to paste, waits for the key to appear, and then runs the
# whole install and verification unattended.
#
# ASCII only, deliberately. Windows PowerShell 5.1 reads a script as the ANSI
# codepage, where a UTF-8 em dash injects a stray quote and unbalances every
# string after it.
param(
    [string]$Target,
    [string]$User = "root",
    [switch]$Tailscale,
    [switch]$SkipTests,
    [int]$WaitMinutes = 15
)

# NOT "Stop": ssh and apt write to stderr on success, and PowerShell turns that
# into a terminating error. Correctness comes from checking $LASTEXITCODE.
$ErrorActionPreference = "Continue"

$src = $PSScriptRoot
$key = "$env:USERPROFILE\.ssh\id_ed25519"

function Say($m)  { Write-Host "==> $m"      -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    WARN  $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    FAIL  $m" -ForegroundColor Red }
function Step($m) { Write-Host ""; Write-Host $m -ForegroundColor White }

# ---------------------------------------------------------------------------
# 1. Where are we looking?
# ---------------------------------------------------------------------------
function Get-Subnets {
    # Every IPv4 network this PC is actually on, most-likely-first. A board
    # plugged into the same router as the PC shows up on one of these.
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and
            $_.PrefixLength -ge 22
        } |
        ForEach-Object { ($_.IPAddress -split '\.')[0..2] -join '.' } |
        Select-Object -Unique
}

function Find-SshHosts($prefix) {
    # One async TCP connect per address, all in flight together. No ping sweep
    # first: the connect does its own address resolution, and 254 Start-Jobs
    # would spawn 254 PowerShell processes -- minutes of work to learn nothing
    # the connect does not already tell us. ForEach-Object -Parallel is not an
    # option either; it does not exist in Windows PowerShell 5.1, which is what
    # this has to run under.
    $found = @()
    $tasks = @()
    foreach ($i in 1..254) {
        $ip = "$prefix.$i"
        $c  = New-Object System.Net.Sockets.TcpClient
        $tasks += [pscustomobject]@{
            IP = $ip; Client = $c; Async = $c.BeginConnect($ip, 22, $null, $null)
        }
    }
    Start-Sleep -Milliseconds 1200
    foreach ($t in $tasks) {
        if ($t.Async.IsCompleted -and $t.Client.Connected) { $found += $t.IP }
        try { $t.Client.Close() } catch {}
    }
    return $found
}

function Test-KeyAuth($dest) {
    $out = & ssh -o BatchMode=yes -o ConnectTimeout=8 `
                 -o StrictHostKeyChecking=accept-new $dest "true" 2>&1 | Out-String
    return [pscustomobject]@{ Output = $out; Code = $LASTEXITCODE }
}

function Get-BoardModel($dest) {
    $m = & ssh -o BatchMode=yes -o ConnectTimeout=8 $dest `
              "tr -d '\0' < /proc/device-tree/model 2>/dev/null" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($m -join "").Trim()
}

Step "PisoWiFi setup"

if (-not (Test-Path "$key.pub")) {
    Say "No SSH key on this PC -- generating one"
    ssh-keygen -t ed25519 -N '""' -f $key | Out-Null
}
$pub = (Get-Content "$key.pub" -Raw).Trim()

if (-not $Target) {
    Say "Looking for the board"
    $gateways = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
                 ForEach-Object { $_.NextHop }) -join " "
    $candidates = @()
    foreach ($prefix in Get-Subnets) {
        Write-Host "    scanning $prefix.0/24"
        foreach ($ip in Find-SshHosts $prefix) {
            if ($gateways -match [regex]::Escape($ip)) { continue }  # the router
            $candidates += $ip
        }
    }
    if ($candidates.Count -eq 0) {
        Bad "No machine on this network is answering SSH."
        Write-Host ""
        Write-Host "  Give it another minute to boot, check the cable, or pass"
        Write-Host "  -Target <ip> if you already know the address."
        exit 1
    }
    # Prefer one that identifies itself as an Orange Pi. That only works once
    # the key is installed, so an unidentified single candidate is still fine.
    $named = @()
    foreach ($ip in $candidates) {
        $model = Get-BoardModel "$User@$ip"
        if ($model -match "Orange Pi") {
            Ok "$ip is $model"
            $named += $ip
        }
    }
    if ($named.Count -eq 1)        { $Target = $named[0] }
    elseif ($candidates.Count -eq 1) { $Target = $candidates[0] }
    else {
        Warn "More than one SSH host answered and none identified itself yet:"
        $candidates | ForEach-Object { Write-Host "      $_" }
        Write-Host ""
        Write-Host "  Re-run with -Target <ip>."
        exit 1
    }
    Ok "using $Target"
}

$dest = "$User@$Target"

# ---------------------------------------------------------------------------
# 2. The one manual step, made as small as possible
# ---------------------------------------------------------------------------
$probe = Test-KeyAuth $dest
if ($probe.Output -match "REMOTE HOST IDENTIFICATION HAS CHANGED") {
    # Normal after a reflash: the card generates fresh host keys, and DHCP
    # reuses addresses. Drop the stale record for this address only.
    Warn "known_hosts has an old key for $Target (reflashed card, or the"
    Warn "address was used by another machine). Removing just that entry."
    & ssh-keygen -R $Target 2>&1 | Out-Null
    $probe = Test-KeyAuth $dest
}

if ($probe.Code -ne 0) {
    Step "One step needs you -- it cannot be automated from Windows"
    Write-Host ""
    Write-Host "  This card has no FAT partition (Allwinner boards keep /boot inside"
    Write-Host "  the ext4 rootfs), so Windows cannot plant an SSH key before first"
    Write-Host "  boot. Until someone logs in once, root is on Armbian's default"
    Write-Host "  password 1234 and the first-login wizard blocks every automated"
    Write-Host "  session."
    Write-Host ""
    Write-Host "  1. Open a terminal and run:" -ForegroundColor Yellow
    Write-Host "         ssh $dest"
    Write-Host "     Password: 1234  (or your own, if you have logged in before)"
    Write-Host ""
    Write-Host "  2. Set a root password when asked. Press Ctrl-C at the"
    Write-Host "     'provide a username' prompt -- creating a user is optional and"
    Write-Host "     the wizard jams on some names."
    Write-Host ""
    Write-Host "  3. Paste this single line into that session:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "rm -f /root/.not_logged_in_yet; mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; echo KEY_INSTALLED"
    Write-Host ""
    Write-Host "     (that also deletes .not_logged_in_yet, which stores both"
    Write-Host "      passwords in PLAIN TEXT and is readable from any card reader)"
    Write-Host ""
    Say "Waiting for the key (up to $WaitMinutes minutes) -- Ctrl-C to give up"

    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $dots = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $probe = Test-KeyAuth $dest
        if ($probe.Code -eq 0) { break }
        $dots++
        if ($dots % 12 -eq 0) { Write-Host "    still waiting..." }
    }
    if ($probe.Code -ne 0) {
        Bad "Key never appeared. Nothing has been changed on the board."
        exit 1
    }
    Write-Host ""
}
$model = Get-BoardModel $dest
Ok "key auth works -- $(if ($model) { $model } else { 'model unknown' })"

# ---------------------------------------------------------------------------
# 3. Hand over to the installer
# ---------------------------------------------------------------------------
Step "Installing (this pulls packages -- several minutes)"
# NOT $args. That is a PowerShell automatic variable holding the script's own
# unbound arguments, and assigning to it then splatting @args scrambles the
# binding: provision.ps1 received -User "192.168.254.122" and -Target "-Target",
# and tried to ssh to "192.168.254.122@-Target". It fails in a way that reads
# like a network or password problem, which is what makes it worth naming.
$provisionArgs = @("-Target", $Target, "-User", $User)
if ($Tailscale) { $provisionArgs += "-Tailscale" }
if ($SkipTests) { $provisionArgs += "-SkipTests" }
& (Join-Path $src "provision.ps1") @provisionArgs
if ($LASTEXITCODE -ne 0) {
    Bad "provision.ps1 failed -- see the output above. Nothing was sealed."
    exit 1
}

# ---------------------------------------------------------------------------
# 4. Verify the things that have gone wrong before
# ---------------------------------------------------------------------------
# Every check here exists because that exact failure shipped once, and every
# one of them was silent: the service was active and the portal answered while
# the machine quietly did the wrong thing.
Step "Post-install checks"
$problems = @()

$coin = & ssh -o BatchMode=yes $dest `
    "journalctl -u pisowifi -b --no-pager -o cat | grep -i 'coin input armed' | tail -1" 2>$null
if ($coin -match "via gpiod") {
    Ok "coin input on the libgpiod backend"
} elseif ($coin -match "via poll") {
    Warn "coin input fell back to POLLING -- python3-libgpiod missing?"
    Warn "  It still counts coins, but this is not the intended path."
} else {
    Bad "coin input did not arm at all"
    $problems += "coin input not armed"
}

$degraded = & ssh -o BatchMode=yes $dest `
    "journalctl -u pisowifi-detect -b --no-pager -o cat | grep -ci 'WITHOUT tethering\|is INVALID'" 2>$null
if ("$degraded".Trim() -eq "0") {
    Ok "firewall loaded in full (nothing degraded)"
} else {
    Warn "the ruleset would not load and was installed WITHOUT tethering"
    Warn "  detection. The machine works; that feature does not."
}

$listen = & ssh -o BatchMode=yes $dest `
    "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://10.0.0.1:8080/" 2>$null
if ("$listen".Trim() -eq "200") { Ok "portal answering on the customer LAN" }
else { Bad "portal did not answer on 10.0.0.1:8080 (got '$listen')"; $problems += "portal" }

# Single-quoted: PowerShell expands $s inside double quotes and treats the
# backslash as literal, so "\$s" would reach the board as a bare backslash.
$svcCmd = 'for s in pisowifi nftables dnsmasq; do systemctl is-active $s; done'
$svc = & ssh -o BatchMode=yes $dest $svcCmd 2>$null
if (($svc -join " ") -notmatch "inactive|failed") { Ok "services active: $($svc -join ' ')" }
else { Bad "a service is not running: $($svc -join ' ')"; $problems += "services" }

Step "Summary"
if ($problems.Count -gt 0) {
    Bad "$($problems.Count) problem(s): $($problems -join ', ')"
    Write-Host "  Do NOT take money with this machine until they are resolved."
    exit 1
}
Write-Host "MACHINE IS UP at http://$Target`:8080/admin" -ForegroundColor Green
Write-Host ""
Write-Host "Still yours to do -- neither can be automated:"
Write-Host "  1. Train the coin acceptor, then verify with real coins in"
Write-Host "     Admin -> Diagnostics -> coin pulse monitor."
Write-Host "  2. Set the admin password, rates and hotspot name, and whitelist"
Write-Host "     your own phone before anything else."
if ($Tailscale) {
    Write-Host "  3. Enrol Tailscale in Admin -> Remote access, confirm you can reach"
    Write-Host "     admin at its 100.x address, and only THEN lock admin to it."
}
Write-Host ""
Write-Host "When it all works, cut the master card:"
Write-Host "  .\provision.ps1 -Target $Target -Seal -ZeroFill"
exit 0
