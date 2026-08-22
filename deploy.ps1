# PisoWiFi one-command deployer -- provisions a fresh board over the network.
#
#   .\deploy.ps1 -Target 192.168.254.117
#   .\deploy.ps1 -Target 192.168.1.50 -InstallArgs "--yes","--lan","eth1"
#
# Requires: your SSH key installed on the board (one-time):
#   Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" | ssh root@<ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [string]$User = "root",
    [string[]]$InstallArgs = @("--yes")
)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dest = "$User@$Target"

Write-Host "==> Checking SSH access to $dest"
ssh -o BatchMode=yes -o ConnectTimeout=10 $dest "true"
if ($LASTEXITCODE -ne 0) { throw "No key-based SSH access to $dest -- install your key first (see comment at top)." }

Write-Host "==> Copying project"
ssh $dest 'rm -rf /root/pisowifi.new && mkdir -p /root/pisowifi.new'
scp -r "$src\app" "$src\network" "$src\systemd" "$src\install.sh" "$src\seal.sh" "$src\README.md" "${dest}:/root/pisowifi.new/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

# Swap into place. The CRLF-strip that used to live here corrupted every file
# it touched: the backslash in "s/\r$//" was lost before the board's sed saw
# it, leaving s/r$// -- a trailing "r" deleted from every line, turning
# "import shaper" into "import shape". .gitattributes checks this tree out as
# LF, so there is nothing to strip.
ssh $dest 'rm -rf /root/pisowifi && mv /root/pisowifi.new /root/pisowifi'

Write-Host "==> Running installer ($($InstallArgs -join ' '))"
ssh $dest "cd /root/pisowifi && bash install.sh $($InstallArgs -join ' ')"
if ($LASTEXITCODE -ne 0) { throw "installer failed -- see output above" }

Write-Host "==> Done. Portal: http://10.0.0.1:8080  Admin: http://10.0.0.1:8080/admin"
