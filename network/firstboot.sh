#!/usr/bin/env bash
# PisoWiFi first-boot personalisation — run once by pisowifi-firstboot.service,
# the first time a freshly written card boots on a board.
#
# A card written from a sealed image is byte-identical on every machine, which
# is exactly what makes it plug-and-play but also means every board would come
# up claiming the same identity. This gives each one its own: machine-id, SSH
# host keys, and hostname (which is what the cloud dashboard keys devices on).
# It then marks itself done so it never runs again.
#
# Idempotent and safe to run by hand: bash /usr/local/sbin/pisowifi-firstboot.sh
set -u

MARK=/var/lib/pisowifi/.provisioned
log() { echo "pisowifi-firstboot: $*"; }

mkdir -p /var/lib/pisowifi
chmod 700 /var/lib/pisowifi

if [ -e "$MARK" ]; then
    log "already personalised — nothing to do"
    exit 0
fi

# ---------------------------------------------------------------------------
# machine-id — systemd, journald and D-Bus all key off this
# ---------------------------------------------------------------------------
if [ ! -s /etc/machine-id ]; then
    systemd-machine-id-setup >/dev/null 2>&1 || \
        (command -v uuidgen >/dev/null 2>&1 && uuidgen | tr -d - > /etc/machine-id)
fi
if [ -e /var/lib/dbus/machine-id ] || [ -d /var/lib/dbus ]; then
    cp -f /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# SSH host keys — a shared host key across a fleet means any machine can
# impersonate any other, so mint a fresh set per board
# ---------------------------------------------------------------------------
if [ -d /etc/ssh ] && ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -A >/dev/null 2>&1 || true
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    log "generated SSH host keys"
fi

# ---------------------------------------------------------------------------
# hostname — becomes the default device_id reported to the cloud dashboard,
# so two machines never show up as one row
# ---------------------------------------------------------------------------
CURRENT="$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]')"
case "$CURRENT" in
    ""|pisowifi|localhost|armbian|orangepi*|debian)
        SUFFIX=""
        for path in /sys/class/net/*/address; do
            ifn="$(basename "$(dirname "$path")")"
            case "$ifn" in lo|docker*|veth*|br-*|ifb*) continue ;; esac
            SUFFIX="$(tr -d ':' < "$path" | tail -c 7 | tr '[:upper:]' '[:lower:]')"
            [ -n "$SUFFIX" ] && break
        done
        [ -n "$SUFFIX" ] || SUFFIX="$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        NEW="pisowifi-${SUFFIX}"
        echo "$NEW" > /etc/hostname
        hostnamectl set-hostname "$NEW" >/dev/null 2>&1 || hostname "$NEW" 2>/dev/null || true
        # Keep sudo/loopback resolution happy on the new name.
        if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW/" /etc/hosts
        else
            printf '127.0.1.1\t%s\n' "$NEW" >> /etc/hosts
        fi
        log "hostname set to $NEW"
        ;;
    *)
        log "keeping existing hostname '$CURRENT'"
        ;;
esac

touch "$MARK"
log "done"
exit 0
