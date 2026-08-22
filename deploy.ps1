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

# swap into place + strip Windows line endings
ssh $dest 'rm -rf /root/pisowifi && mv /root/pisowifi.new /root/pisowifi && find /root/pisowifi -type f -exec sed -i "s/\r$//" {} +'

Write-Host "==> Running installer ($($InstallArgs -join ' '))"
ssh $dest "cd /root/pisowifi && bash install.sh $($InstallArgs -join ' ')"
if ($LASTEXITCODE -ne 0) { throw "installer failed -- see output above" }

Write-Host "==> Done. Portal: http://10.0.0.1:8080  Admin: http://10.0.0.1:8080/admin"
