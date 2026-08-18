# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\app', 'payload\\app'), ('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\network', 'payload\\network'), ('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\systemd', 'payload\\systemd'), ('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\server', 'payload\\server'), ('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\install.sh', 'payload'), ('C:\\Users\\Adiel\\Desktop\\Orange Pi One\\README.md', 'payload')]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules('nacl')
tmp_ret = collect_all('paramiko')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['pisowifi_deployer.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['app'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PisoWiFi-Deployer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
