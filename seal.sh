#!/usr/bin/env bash
# Seal this machine for imaging — turn a working PisoWiFi box into a "golden"
# card you can clone onto any number of new machines.
#
#   bash seal.sh --yes            # wipe identity + takings, then power off
#   bash seal.sh --yes --keep-db  # keep sales history (single-machine backup)
#   bash seal.sh --yes --zero     # also zero free space (smaller .img.xz, slow)
#
# THIS IS DESTRUCTIVE on the machine it runs on. It removes everything that
# makes this box *this* box: the sales database, the admin password, the SSH
# host keys, the machine-id, the logs, and the rendered network configs. What
# is left is a generic image. Each card written from it personalises itself on
# first boot (pisowifi-firstboot) and re-detects its own hardware on every boot
# (pisowifi-detect), so it comes up as a working hotspot with no SSH at all —
# the owner just opens the portal and sets an admin password.
#
# Run this on the golden machine as the LAST step, then power it off, pull the
# card, and read it to an .img with the deployer's SD Card tab (or USBImager).
set -u

YES=0; KEEP_DB=0; ZERO=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)   YES=1 ;;
        --keep-db)  KEEP_DB=1 ;;
        --zero)     ZERO=1 ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

DB_PATH="$(python3 -c "import json;print(json.load(open('/etc/pisowifi/config.json')).get('db_path') or '')" 2>/dev/null || true)"
[ -n "$DB_PATH" ] || DB_PATH=/var/lib/pisowifi/pisowifi.db

cat <<EOF

This will WIPE this machine and power it off:
  * sales, sessions, wallets, vouchers, audit log, PPPoE accounts   $( [ "$KEEP_DB" = 1 ] && echo "(KEPT: --keep-db)" )
  * the device whitelist (or card N would 403 its own owner)
  * admin password (the new card forces a fresh one at first login)
  * SSH host keys, machine-id, hostname, logs, DHCP leases
  * the rendered nftables/dnsmasq/hostapd configs (regenerated at every boot)

KEPT — this is what a golden image is for:
  * coin/relay GPIO setup: pin, edge, debounce, denominations, pulse timing
  * rates, bonus tiers, hotspot name, speed profiles
  * branding (logo/banner/colour), schedules, watchdog, feature toggles

EOF

if [ "$YES" != 1 ]; then
    if [ -t 0 ]; then
        read -r -p "Type SEAL to continue: " a
        [ "$a" = "SEAL" ] || { echo "aborted"; exit 1; }
    else
        echo "refusing to seal without --yes (not a terminal)"; exit 1
    fi
fi

echo "==> Stopping services"
systemctl stop pisowifi dnsmasq hostapd pppoe-server 2>/dev/null || true

# ---------------------------------------------------------------------------
# takings and per-machine state
# ---------------------------------------------------------------------------
if [ "$KEEP_DB" = 1 ]; then
    echo "==> Keeping the database untouched (--keep-db)"
else
    echo "==> Clearing takings and identity, keeping your configuration"
    python3 - "$DB_PATH" <<'PY'
import os, sqlite3, sys

db = sys.argv[1]
if not os.path.exists(db):
    print("    (no database yet — nothing to clear)")
    raise SystemExit(0)

# Everything the operator tuned on this machine lives in the settings table,
# because that is where the admin UI writes: rates, bonus tiers, hotspot name,
# branding, schedules, feature toggles — and the coin/relay GPIO setup. That
# configuration IS the value of a golden image, so it stays. Deleting the whole
# database here would hand every cloned card the config.json defaults instead,
# including the default coin pin, which is exactly the wrong outcome.
IDENTITY = ("admin_pw_hash", "admin_pw_default", "SECRET_KEY", "device_id")
CUSTOMER = ("sales", "sessions", "vouchers", "devices", "audit",
            "trials", "wallets", "pppoe_accounts")

con = sqlite3.connect(db)
con.isolation_level = None          # autocommit — VACUUM cannot run in a transaction
for t in CUSTOMER:
    try:
        con.execute("DELETE FROM %s" % t)
    except sqlite3.OperationalError:
        pass                        # table absent in this schema version
for k in IDENTITY:
    con.execute("DELETE FROM settings WHERE key=?", (k,))
kept = con.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.execute("VACUUM")
con.close()
print("    kept %d configuration settings (rates, pins, branding, toggles)" % kept)
print("    cleared sales, sessions, vouchers, devices, wallets, audit, PPPoE")
PY
    rm -f "$DB_PATH-wal" "$DB_PATH-shm"
    rm -rf "$(dirname "$DB_PATH")/backups"
fi
rm -f /var/lib/pisowifi/.provisioned

# The admin password lives in the DB as a hash once it has been set. Dropping
# the DB is what re-arms the forced first-run setup wizard; the config.json
# plaintext below is only the one-time seed that gets you as far as that wizard.
echo "==> Resetting the admin password to the shipped default"
python3 - <<'PY'
import json
p = "/etc/pisowifi/config.json"
c = json.load(open(p))
c["admin_password"] = "changeme"
# Bench-only. A master image must never ship with the uplink open.
c["wan_management"] = False
# device_id falls back to the hostname, which first boot makes unique per board.
c["device_id"] = ""
json.dump(c, open(p, "w"), indent=2)
PY
chmod 600 /etc/pisowifi/config.json

# ---------------------------------------------------------------------------
# machine identity — regenerated on first boot by pisowifi-firstboot
# ---------------------------------------------------------------------------
echo "==> Clearing machine identity (machine-id, SSH host keys, hostname)"
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
echo pisowifi > /etc/hostname
sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tpisowifi/' /etc/hosts 2>/dev/null || true

# ---------------------------------------------------------------------------
# anything that pins this image to one board's hardware
# ---------------------------------------------------------------------------
echo "==> Clearing hardware-specific network state"
# The pre-plug-and-play installer pinned the LAN dongle's MAC here, which is
# exactly what stopped a cloned card working in another machine.
rm -f /etc/udev/rules.d/70-pisowifi-lan.rules
rm -f /etc/udev/rules.d/70-persistent-net.rules
rm -f /etc/nftables.conf /etc/dnsmasq.d/pisowifi.conf /etc/hostapd/hostapd.conf
rm -f /etc/NetworkManager/conf.d/99-pisowifi.conf
rm -f /var/lib/misc/dnsmasq.leases /var/lib/dhcp/*.leases 2>/dev/null || true
rm -f /etc/NetworkManager/system-connections/* 2>/dev/null || true
rm -f /run/pisowifi/env /run/pisowifi/hostapd.needed 2>/dev/null || true
# Superseded by pisowifi-detect.service; leaving it behind would re-apply an
# old board's interface names on top of the freshly detected ones.
systemctl disable pisowifi-net.service 2>/dev/null || true
rm -f /etc/systemd/system/pisowifi-net.service

# ---------------------------------------------------------------------------
# logs and shell history
# ---------------------------------------------------------------------------
echo "==> Clearing logs"
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -rf /var/log/journal/* 2>/dev/null || true
find /var/log -type f \( -name '*.gz' -o -name '*.1' -o -name '*.old' \) -delete 2>/dev/null || true
: > /var/log/wtmp 2>/dev/null || true
: > /var/log/btmp 2>/dev/null || true
rm -f /root/.bash_history /home/*/.bash_history 2>/dev/null || true

# Armbian's first-boot autoconfig file stores the root and user passwords in
# PLAIN TEXT. It has already done its job on this machine, and every card
# written from this image would otherwise carry those passwords readable to
# anyone who puts the card in a PC.
rm -f /root/.not_logged_in_yet

if [ "$ZERO" = 1 ]; then
    echo "==> Zeroing free space (makes the .img compress far smaller; slow)"
    dd if=/dev/zero of=/ZEROFILL bs=4M status=none 2>/dev/null || true
    sync; rm -f /ZEROFILL; sync
fi

cat <<'EOF'

==> SEALED.

Next:
  1. This machine powers off in 5 seconds. Pull the SD card.
  2. Read the card to an image (deployer -> SD Card -> READ / CLONE, or
     USBImager -> Read). That .img is your master.
  3. Write it to as many cards as you like. Each one boots ready to use:
     insert card -> power on -> the SSID appears -> open the portal on a
     phone -> Admin -> set a password. No SSH, no config editing.

Do the first-login password step before the machine faces customers: until
it is done the box is on the shipped password, and the setup page is
deliberately reachable from the customer LAN so you can do it from a phone.
EOF

sync
sleep 5
systemctl poweroff
