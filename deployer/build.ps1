# Build PisoWiFi-Deployer.exe (run from the deployer/ folder).
#
#   pip install -r requirements.txt      # once
#   .\build.ps1                          # produces dist\PisoWiFi-Deployer.exe
#
# The whole PisoWiFi project (app/, network/, systemd/, install.sh, server/)
# is bundled inside the exe as "payload/", so the exe is fully self-contained.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$proj = Split-Path -Parent $PSScriptRoot
$sep = ";"   # PyInstaller add-data separator on Windows

$addData = @(
  "$proj\app${sep}payload\app",
  "$proj\network${sep}payload\network",
  "$proj\systemd${sep}payload\systemd",
  "$proj\server${sep}payload\server",
  "$proj\install.sh${sep}payload",
  "$proj\README.md${sep}payload"
)

$args = @(
  "--noconfirm", "--clean", "--onefile",
  "--name", "PisoWiFi-Deployer",
  "--collect-all", "paramiko",
  "--collect-all", "cryptography",
  "--collect-submodules", "nacl",
  "--exclude-module", "app"           # don't sweep the payload app/ into imports
)
foreach ($d in $addData) { $args += @("--add-data", $d) }
$args += "pisowifi_deployer.py"

Write-Host "==> Cleaning old build"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "==> Running PyInstaller"
python -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "==> Self-test of the built exe"
& ".\dist\PisoWiFi-Deployer.exe" --selftest
if ($LASTEXITCODE -ne 0) { throw "exe self-test FAILED" }

Write-Host "`n==> DONE: dist\PisoWiFi-Deployer.exe"
