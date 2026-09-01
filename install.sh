#!/usr/bin/env bash
# PisoWiFi installer — run as root on Armbian/Debian on the target board.
#
#   bash install.sh                          # install; hardware detected at boot
#   bash install.sh --yes                    # non-interactive
#   bash install.sh --gw 10.0.0.1            # different client subnet
#   bash install.sh --lan eth1 --wan eth0    # pin the interfaces by hand
#   bash install.sh --vlan 5                 # customer traffic on VLAN 5
#
# The installer no longer decides which NIC is which. That happens on EVERY
# boot, in pisowifi-detect.service (network/detect.sh): the onboard NIC becomes
# the internet uplink, a USB-ethernet dongle becomes the customer LAN, and a
# WiFi adapter becomes the LAN (with hostapd) when there is no dongle. Nothing
# board-specific is written to disk, so the finished SD card can be cloned onto
# any number of machines and each one comes up correctly — see seal.sh.
#
# Passing --lan/--wan turns auto-detection off and pins those names instead.
# Safe to re-run — an existing /etc/pisowifi/config.json is preserved.
set -euo pipefail

LAN_IF=""; WAN_IF=""; GW_IP="10.0.0.1"; USE_HOSTAPD=""; ASSUME_YES=0; LAN_VLAN=""
WAN_MGMT=""; WITH_TAILSCALE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --lan)     LAN_IF="$2";      shift 2 ;;
        --wan)     WAN_IF="$2";      shift 2 ;;
        --gw)      GW_IP="$2";       shift 2 ;;
        --vlan)    LAN_VLAN="$2";    shift 2 ;;
        --hostapd) USE_HOSTAPD="$2"; shift 2 ;;
        --wired)   USE_HOSTAPD=0;    shift ;;   # kept for compatibility
        --wan-management)   WAN_MGMT=1; shift ;;   # keep SSH open on the uplink
        --no-wan-management) WAN_MGMT=0; shift ;;
        --tailscale) WITH_TAILSCALE=1; shift ;;  # remote admin for a sealed card
        --yes|-y)  ASSUME_YES=1;     shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

AUTO=1
if [ -n "$LAN_IF" ] || [ -n "$WAN_IF" ]; then
    AUTO=0
    [ -n "$LAN_IF" ] && [ -n "$WAN_IF" ] || {
        echo "ERROR: pass both --lan and --wan to pin the interfaces, or neither."
        exit 1
    }
fi
[ -z "$LAN_VLAN" ] && LAN_VLAN=0
[ "$LAN_VLAN" = "" ] && LAN_VLAN=0

echo "PisoWiFi install plan:"
if [ "$AUTO" = 1 ]; then
    echo "  Interfaces      : detected at every boot (portable card image)"
else
    echo "  Interfaces      : pinned — WAN=$WAN_IF LAN=$LAN_IF"
fi
echo "  Gateway IP      : $GW_IP"
echo "  Client subnet   : ${GW_IP%.*}.0/24"
[ "$LAN_VLAN" != 0 ] && echo "  LAN VLAN        : $LAN_VLAN"
if [ "$ASSUME_YES" != 1 ] && [ -t 0 ]; then
    read -r -p "Proceed? [y/N] " a
    case "$a" in y|Y) ;; *) echo "aborted"; exit 1 ;; esac
fi

# ---------- packages ----------
# hostapd, pppoe and ppp go on unconditionally even though most machines never
# use them. They are small, and it means the finished card can be moved to a
# WiFi-radio board, or have PPPoE switched on from the admin UI, without anyone
# needing to SSH in and apt-get anything. cloud-guest-utils supplies growpart,
# which first boot uses to grow a written card's rootfs to the card's real size.
# python3-libgpiod is NOT optional. OPi.GPIO's add_event_detect() arms the
# deprecated sysfs edge interface, which this kernel accepts and then never
# fires, so coins are silently never counted. coinslot.py reads the line through
# libgpiod instead, and falls back to polling only when it is absent -- a
# fallback that works, but that nobody would notice had engaged.
echo "==> Installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    dnsmasq nftables python3-flask python3-pip iproute2 hostapd pppoe ppp \
    cloud-guest-utils python3-libgpiod
pip3 install --break-system-packages OPi.GPIO 2>/dev/null || pip3 install OPi.GPIO
pip3 install --break-system-packages waitress 2>/dev/null || pip3 install waitress || {
    echo "  !! WARNING: waitress did NOT install."
    echo "  !! The app will fall back to the Flask development server, which is"
    echo "  !! not fit for a live site (it stalls under real client load)."
    echo "  !! Fix this before taking money: pip3 install --break-system-packages waitress"
}

# ---------- app ----------
echo "==> Deploying app to /opt/pisowifi"
mkdir -p /opt/pisowifi /var/lib/pisowifi /etc/pisowifi
cp -r "$SRC/app/." /opt/pisowifi/
# Never ship development artifacts to a machine: dev.db carries a development
# SECRET_KEY, and the .pyc files were built for the PC's Python, not the board's.
rm -rf /opt/pisowifi/__pycache__ /opt/pisowifi/dev.db
[ -f /etc/pisowifi/config.json ] || cp "$SRC/app/config.json" /etc/pisowifi/config.json

# The network templates live with the app so the boot-time renderer can find
# them on a machine nobody ever copied the source tree to.
echo "==> Installing network templates to /opt/pisowifi/net"
mkdir -p /opt/pisowifi/net
install -m 0644 "$SRC/network/nftables.conf" "$SRC/network/dnsmasq.conf" \
    "$SRC/network/hostapd.conf" /opt/pisowifi/net/

# The config holds the admin password (until first login) and the remote
# dashboard key; the database holds PPPoE passwords in the clear, because CHAP
# needs them that way. Keep both off-limits to any other local account.
chmod 700 /etc/pisowifi /var/lib/pisowifi
chmod 600 /etc/pisowifi/config.json

# Never leave a machine in the field reachable on the password this project
# ships with. Mint a random one on first install and print it once at the end.
ADMIN_PW=""
if python3 -c "import json,sys; sys.exit(0 if json.load(open('/etc/pisowifi/config.json')).get('admin_password')=='changeme' else 1)"; then
    ADMIN_PW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')"
    python3 - "$ADMIN_PW" <<'PY'
import json, sys
p = "/etc/pisowifi/config.json"
c = json.load(open(p))
c["admin_password"] = sys.argv[1]
json.dump(c, open(p, "w"), indent=2)
PY
    chmod 600 /etc/pisowifi/config.json
fi

# Record the operator's choices. Everything else (which NIC is which) is left
# for the boot-time detector to fill in.
python3 - "$AUTO" "$GW_IP" "$LAN_VLAN" "$LAN_IF" "$WAN_IF" "${WAN_MGMT:-}" <<'PY'
import json, sys
auto, gw, vlan, lan, wan, wanmgmt = sys.argv[1:7]
p = "/etc/pisowifi/config.json"
c = json.load(open(p))
c["auto_detect_interfaces"] = (auto == "1")
c["gateway_ip"] = gw
c["lan_vlan"] = int(vlan or 0)
if auto != "1":
    c["lan_if"], c["wan_if"] = lan, wan
if wanmgmt != "":
    c["wan_management"] = (wanmgmt == "1")
json.dump(c, open(p, "w"), indent=2)
PY
chmod 600 /etc/pisowifi/config.json

# ---------- boot-time provisioning ----------
echo "==> Boot-time provisioning scripts"
install -m 0755 "$SRC/network/detect.sh"    /usr/local/sbin/pisowifi-detect.sh
install -m 0755 "$SRC/network/firstboot.sh" /usr/local/sbin/pisowifi-firstboot.sh
install -m 0755 "$SRC/network/tune.sh"      /usr/local/sbin/pisowifi-tune.sh

# Superseded by pisowifi-detect.service. Left in place it would re-apply the
# interface names of whichever board the installer last ran on, which is the
# whole problem the detector exists to solve.
if [ -e /etc/systemd/system/pisowifi-net.service ]; then
    echo "==> Removing the old pisowifi-net.service (replaced by pisowifi-detect)"
    systemctl disable --now pisowifi-net.service 2>/dev/null || true
    rm -f /etc/systemd/system/pisowifi-net.service
fi
rm -f /etc/udev/rules.d/70-pisowifi-lan.rules   # old MAC pin — see detect.sh

install -m 0644 "$SRC/systemd/pisowifi.service"           /etc/systemd/system/pisowifi.service
install -m 0644 "$SRC/systemd/pisowifi-detect.service"    /etc/systemd/system/pisowifi-detect.service
install -m 0644 "$SRC/systemd/pisowifi-firstboot.service" /etc/systemd/system/pisowifi-firstboot.service

cat > /etc/systemd/system/pisowifi-tune.service <<'EOF'
[Unit]
Description=PisoWiFi runtime tuning (RPS/conntrack/governor)
After=pisowifi-detect.service network.target
Wants=pisowifi-detect.service
ConditionPathExists=/run/pisowifi/env

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '. /run/pisowifi/env; exec /usr/local/sbin/pisowifi-tune.sh "$WAN_IF" "$LAN_IF" "$LAN_DEV"'

[Install]
WantedBy=multi-user.target
EOF

# hostapd is installed on every machine but must only actually start on the
# ones serving their own WiFi. The detector drops this flag file when the LAN
# it found is a wireless adapter.
mkdir -p /etc/systemd/system/hostapd.service.d
cat > /etc/systemd/system/hostapd.service.d/99-pisowifi.conf <<'EOF'
[Unit]
After=pisowifi-detect.service
Wants=pisowifi-detect.service
ConditionPathExists=/run/pisowifi/hostapd.needed
EOF

# ---------- optional: PPPoE access concentrator (monthly subscribers) -------
# Enabled/disabled purely by the `pppoe_enabled` toggle in the admin UI; the
# detector drops the flag file this unit waits on.
touch /etc/ppp/chap-secrets && chmod 600 /etc/ppp/chap-secrets
cat > /etc/systemd/system/pppoe-server.service <<'EOF'
[Unit]
Description=PisoWiFi PPPoE access concentrator
After=pisowifi-detect.service network.target
Wants=pisowifi-detect.service
ConditionPathExists=/run/pisowifi/pppoe.needed

[Service]
ExecStart=/bin/sh -c '. /run/pisowifi/env; exec /usr/sbin/pppoe-server -F -I "$LAN_DEV" -L 12.0.0.1 -R 12.0.0.2 -N 254 -O /etc/ppp/pppoe-server-options'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> IP forwarding + network tuning (sysctl)"
cat > /etc/sysctl.d/99-pisowifi.conf <<'EOF'
net.ipv4.ip_forward=1
# IPv6 is deliberately NOT routed. The customer-isolation rules are
# written for IPv4, so enabling this without extending them would give
# customers an unfiltered path to the owner's network.
net.ipv6.conf.all.forwarding=0
# handle many simultaneous clients/connections without exhausting conntrack
net.netfilter.nf_conntrack_max=131072
net.core.netdev_max_backlog=2000
net.core.somaxconn=1024
net.ipv4.tcp_fastopen=3
net.ipv4.tcp_mtu_probing=1
EOF
sysctl --system >/dev/null 2>&1 || true

echo "==> Protecting the SD card (journal size caps)"
# The microSD is the first thing to die on a box that runs 24/7 for years, and
# systemd's journal is normally the heaviest writer on it. Cap it hard. Logs
# still survive reboots (needed for `journalctl -u pisowifi` troubleshooting);
# for maximum card life set Storage=volatile instead and accept RAM-only logs.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-pisowifi.conf <<'EOF'
[Journal]
SystemMaxUse=32M
SystemMaxFileSize=8M
RuntimeMaxUse=16M
EOF
systemctl restart systemd-journald 2>/dev/null || true

systemctl daemon-reload

# ---------- provision this board now, so the install works without a reboot --
echo "==> Detecting interfaces and rendering network configs"
/usr/local/sbin/pisowifi-firstboot.sh || true
/usr/local/sbin/pisowifi-detect.sh
# shellcheck disable=SC1091
[ -f /run/pisowifi/env ] && . /run/pisowifi/env

echo "==> Enabling services"
systemctl unmask hostapd 2>/dev/null || true
systemctl enable pisowifi-firstboot pisowifi-detect pisowifi-tune hostapd pppoe-server \
    >/dev/null 2>&1 || true
systemctl enable nftables dnsmasq pisowifi >/dev/null 2>&1 || true
systemctl restart pisowifi-detect pisowifi-tune nftables dnsmasq pisowifi
# Condition-gated: these no-op on machines that do not need them.
systemctl restart hostapd 2>/dev/null || true
systemctl restart pppoe-server 2>/dev/null || true

echo
if [ -f /run/pisowifi/env ]; then
    echo "DONE. WAN=${WAN_IF:-?}  LAN=${LAN_DEV:-?}  hostapd=${USE_HOSTAPD:-0}"
else
    echo "DONE — but no LAN adapter was found. Plug in the antenna's USB-ethernet"
    echo "dongle (or a WiFi adapter) and reboot; it is picked up automatically."
fi
echo "Portal: http://$GW_IP:8080   Admin: http://$GW_IP:8080/admin"
if [ -n "$ADMIN_PW" ]; then
    echo
    echo "  =============================================================="
    echo "  ADMIN PASSWORD (generated, shown once):"
    echo "      $ADMIN_PW"
    echo "  =============================================================="
    echo "  Write this down now. It is also in /etc/pisowifi/config.json"
    echo "  (root-only) if you lose it. The first login makes you replace it."
else
    echo "  (kept the existing /etc/pisowifi/config.json — password unchanged)"
fi

# ---------------------------------------------------------------------------
# optional: Tailscale, for administering a sealed card
# ---------------------------------------------------------------------------
# Installed here rather than from the admin page on purpose: this adds a
# third-party package source, which is a decision for whoever builds the
# machine, not something a web request should be able to do. Enrolment is the
# web page's job -- this only puts the binary on the card.
#
# Nothing is authenticated at this point. seal.sh wipes /var/lib/tailscale so
# a cloned card carries the package but not an identity, and each machine
# enrols itself with its own auth key from Admin -> Remote access.
if [ "$WITH_TAILSCALE" = 1 ]; then
    if command -v tailscale >/dev/null 2>&1; then
        echo "==> Tailscale already installed"
    else
        echo "==> Installing Tailscale"
        CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-trixie}")"
        install -d -m 0755 /usr/share/keyrings
        KEY_URL="https://pkgs.tailscale.com/stable/debian/${CODENAME}.noarmor.gpg"
        LIST_URL="https://pkgs.tailscale.com/stable/debian/${CODENAME}.tailscale-keyring.list"
        KEYRING=/usr/share/keyrings/tailscale-archive-keyring.gpg
        if curl -fsSL "$KEY_URL" -o "$KEYRING" &&
           curl -fsSL "$LIST_URL" -o /etc/apt/sources.list.d/tailscale.list; then
            apt-get update -qq || true
            if apt-get install -y tailscale; then
                systemctl enable --now tailscaled || true
                echo "    installed. Enrol it from Admin -> Remote access."
            else
                echo "  !! tailscale package failed to install; continuing without it"
            fi
        else
            # A missing uplink must not take the whole install down -- the
            # hotspot works fine without remote admin.
            echo "  !! could not reach pkgs.tailscale.com; continuing without it"
        fi
    fi
fi

echo
echo "IMPORTANT — admin exposure:"
echo "  The portal port must be open to customers, and /admin sits on it. Admin"
echo "  stays reachable from the customer network only until you whitelist your"
echo "  first device (Admin -> Devices). After that only whitelisted devices, the"
echo "  machine itself, or a private path such as Tailscale can open it."
echo "  Whitelist your own phone first, or you will lock yourself out."
echo
echo "Rates, hotspot name and password are all editable from Admin in a browser."
echo "To turn this machine into a master card image for others: bash seal.sh --yes"
