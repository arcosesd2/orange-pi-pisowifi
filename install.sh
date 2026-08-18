#!/usr/bin/env bash
# PisoWiFi installer — run as root on Armbian/Debian on the target board.
#
#   bash install.sh                          # auto-detect, confirm if interactive
#   bash install.sh --yes                    # non-interactive, accept detected plan
#   bash install.sh --wired                  # eth0 = internet, eth1 = antenna (no hostapd)
#   bash install.sh --lan eth1 --wan eth0 --gw 10.0.0.1 --hostapd 1
#   bash install.sh --wan eth0 --lan eth0 --vlan 5   # customer traffic on VLAN 5
#
# Auto-detection: WAN = interface holding the default route; LAN = the first
# other wired interface (USB-ethernet), else a wlan* (hostapd is then enabled).
# --wired  = the standard vendo layout: onboard eth0 is the internet uplink and a
#            USB-ethernet dongle (pinned to eth1) feeds the antenna; hostapd off.
#            The dongle is renamed to eth1 now AND pinned by MAC so it stays eth1
#            across reboots (Armbian otherwise names it enx<mac>).
# --vlan <id> tags the LAN onto <lan_if>.<id> to pair with a VLAN AP/antenna.
# Safe to re-run — existing /etc/pisowifi/config.json is preserved.
set -euo pipefail

LAN_IF=""; WAN_IF=""; GW_IP="10.0.0.1"; USE_HOSTAPD=""; ASSUME_YES=0; LAN_VLAN=""; WIRED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --lan)     LAN_IF="$2";      shift 2 ;;
        --wan)     WAN_IF="$2";      shift 2 ;;
        --gw)      GW_IP="$2";       shift 2 ;;
        --vlan)    LAN_VLAN="$2";    shift 2 ;;
        --hostapd) USE_HOSTAPD="$2"; shift 2 ;;
        --wired)   WIRED=1;          shift ;;
        --yes|-y)  ASSUME_YES=1;     shift ;;
        *) echo "unknown option: $1"; exit 1 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

# Pin the USB-ethernet LAN NIC to a stable "eth1": rename it live and persist the
# name by MAC via udev, so the antenna port is always eth1 across reboots.
ensure_eth1() {
    local cand mac
    if ip link show eth1 >/dev/null 2>&1; then
        cand=eth1
    else
        cand=""
        for i in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1); do
            case "$i" in lo|"$WAN_IF"|docker*|veth*|br-*|wlan*) continue ;; esac
            cand="$i"; break
        done
    fi
    if [ -z "$cand" ]; then
        echo "  WARNING: no USB-ethernet NIC found to become eth1 — plug the antenna dongle in (or pass --lan)."
        LAN_IF="eth1"; return
    fi
    mac="$(cat "/sys/class/net/$cand/address" 2>/dev/null || true)"
    if [ -n "$mac" ]; then
        printf 'SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="%s", NAME="eth1"\n' "$mac" \
            > /etc/udev/rules.d/70-pisowifi-lan.rules
        echo "  pinned $cand ($mac) -> eth1 (udev)"
    fi
    if [ "$cand" != eth1 ]; then
        ip link set "$cand" down 2>/dev/null || true
        ip link set "$cand" name eth1 2>/dev/null || true
        ip link set eth1 up 2>/dev/null || true
    fi
    LAN_IF="eth1"
}

# ---------- detect interfaces ----------
if [ -z "$WAN_IF" ]; then
    WAN_IF="$(ip -o route show default | awk '{print $5; exit}')"
fi
# --wired: onboard eth0 = internet uplink, USB dongle (eth1) = antenna, no hostapd.
if [ "$WIRED" = 1 ]; then
    [ -n "$WAN_IF" ] || WAN_IF="eth0"
    USE_HOSTAPD=0
    echo "==> Wired vendo layout: WAN=$WAN_IF, LAN=eth1 (antenna)"
    ensure_eth1
fi
[ -n "$WAN_IF" ] || { echo "ERROR: no default route — plug in the uplink or pass --wan"; exit 1; }

if [ -z "$LAN_IF" ]; then
    WLAN_CAND=""
    for i in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1); do
        case "$i" in lo|"$WAN_IF"|docker*|veth*|br-*) continue ;; esac
        case "$i" in
            wlan*) WLAN_CAND="$i" ;;
            *)     LAN_IF="$i"; break ;;
        esac
    done
    [ -z "$LAN_IF" ] && LAN_IF="$WLAN_CAND"
fi
[ -n "$LAN_IF" ] || { echo "ERROR: no LAN interface found — plug in USB ethernet/WiFi or pass --lan"; exit 1; }

if [ -z "$USE_HOSTAPD" ]; then
    case "$LAN_IF" in wlan*) USE_HOSTAPD=1 ;; *) USE_HOSTAPD=0 ;; esac
fi

# VLAN: default from config.json if not passed; 0/empty = off.
if [ -z "$LAN_VLAN" ]; then
    LAN_VLAN="$(python3 -c "import json;print(json.load(open('$SRC/app/config.json')).get('lan_vlan') or '')" 2>/dev/null || true)"
fi
[ "$LAN_VLAN" = "0" ] && LAN_VLAN=""
LAN_DEV="$LAN_IF"; [ -n "$LAN_VLAN" ] && LAN_DEV="$LAN_IF.$LAN_VLAN"
CLIENT_NET="${GW_IP%.*}.0/24"

echo "PisoWiFi install plan:"
echo "  WAN (internet uplink) : $WAN_IF"
echo "  LAN (customers)       : $LAN_DEV${LAN_VLAN:+  (VLAN $LAN_VLAN on $LAN_IF)}"
echo "  Client subnet         : $CLIENT_NET"
echo "  Gateway IP            : $GW_IP"
echo "  hostapd (own radio)   : $USE_HOSTAPD"
if [ "$ASSUME_YES" != 1 ] && [ -t 0 ]; then
    read -r -p "Proceed? [y/N] " a
    case "$a" in y|Y) ;; *) echo "aborted"; exit 1 ;; esac
fi

# ---------- packages ----------
echo "==> Installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    dnsmasq nftables python3-flask python3-pip iproute2 \
    $( [ "$USE_HOSTAPD" = 1 ] && echo hostapd )
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

# Keep the app's view of the interfaces in sync with what we wire below, so
# per-client QoS (shaper) and lan_vlan resolve to the same device as nftables.
python3 - "$LAN_IF" "$WAN_IF" "$GW_IP" "${LAN_VLAN:-0}" <<'PY'
import json, sys
p = "/etc/pisowifi/config.json"
c = json.load(open(p))
c["lan_if"], c["wan_if"], c["gateway_ip"] = sys.argv[1], sys.argv[2], sys.argv[3]
c["lan_vlan"] = int(sys.argv[4] or 0)
json.dump(c, open(p, "w"), indent=2)
PY

# ---------- network configs ----------
echo "==> Network configs (LAN=$LAN_DEV WAN=$WAN_IF)"
# nftables is rendered in python: interface/net substitution + fill the
# anti-tether / flow-offload tokens from the config toggles.
python3 - "$SRC/network/nftables.conf" "$LAN_DEV" "$WAN_IF" "$CLIENT_NET" /etc/pisowifi/config.json \
    > /etc/nftables.conf <<'PY'
import json, sys
tmpl, lan, wan, net, cfgp = sys.argv[1:6]
cfg = json.load(open(cfgp))
s = open(tmpl).read()
# WAN before LAN so "eth0" inside a VLAN LAN like "eth0.5" isn't rewritten
s = s.replace("eth0", wan).replace("wlan0", lan).replace("10.0.0.0/24", net)
anti = ""
if cfg.get("anti_tether"):
    anti = ("    chain mangle_post {\n"
            "        type filter hook postrouting priority mangle; policy accept;\n"
            f"        oifname {lan} ip ttl set 1\n"
            f"        oifname {lan} ip6 hoplimit set 1\n"
            "    }")
flowt = offl = ""
if cfg.get("flow_offload"):
    flowt = ("    flowtable ft {\n"
             "        hook ingress priority filter\n"
             f"        devices = {{ {lan}, {wan} }}\n"
             "    }")
    offl = "        ip protocol { tcp, udp } flow add @ft"
s = s.replace("#@ANTI_TETHER@", anti).replace("#@FLOWTABLE@", flowt).replace("#@OFFLOAD@", offl)
sys.stdout.write(s)
PY
sed -e "s#eth0#$WAN_IF#g" -e "s#wlan0#$LAN_DEV#g" -e "s#10\.0\.0\.#${GW_IP%.*}.#g" \
    "$SRC/network/dnsmasq.conf" > /etc/dnsmasq.d/pisowifi.conf
if [ "$USE_HOSTAPD" = 1 ]; then
    sed -e "s/wlan0/$LAN_IF/g" "$SRC/network/hostapd.conf" > /etc/hostapd/hostapd.conf
    sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd || true
fi

echo "==> IP forwarding + network tuning (sysctl)"
cat > /etc/sysctl.d/99-pisowifi.conf <<'EOF'
net.ipv4.ip_forward=1
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

echo "==> Runtime tuning unit (packet steering / conntrack / CPU governor)"
install -m 0755 "$SRC/network/tune.sh" /usr/local/sbin/pisowifi-tune.sh
cat > /etc/systemd/system/pisowifi-tune.service <<EOF
[Unit]
Description=PisoWiFi runtime tuning (RPS/conntrack/governor)
After=pisowifi-net.service network.target
Wants=pisowifi-net.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/pisowifi-tune.sh $WAN_IF $LAN_IF $LAN_DEV

[Install]
WantedBy=multi-user.target
EOF

echo "==> Keep NetworkManager off the LAN interface"
if command -v nmcli >/dev/null 2>&1; then
    mkdir -p /etc/NetworkManager/conf.d
    printf '[keyfile]\nunmanaged-devices=interface-name:%s;interface-name:%s\n' "$LAN_IF" "$LAN_DEV" \
        > /etc/NetworkManager/conf.d/99-pisowifi.conf
    systemctl reload NetworkManager || true
fi

echo "==> LAN address unit (pisowifi-net.service)"
VLAN_LINE=""
[ -n "$LAN_VLAN" ] && VLAN_LINE="ExecStart=-/sbin/ip link add link $LAN_IF name $LAN_DEV type vlan id $LAN_VLAN"
cat > /etc/systemd/system/pisowifi-net.service <<EOF
[Unit]
Description=PisoWiFi LAN interface address
Before=dnsmasq.service hostapd.service pisowifi.service
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=-/sbin/ip link set $LAN_IF up
$VLAN_LINE
ExecStart=-/sbin/ip link set $LAN_DEV up
ExecStart=-/sbin/ip addr replace $GW_IP/24 dev $LAN_DEV

[Install]
WantedBy=multi-user.target
EOF

# ---------- optional: PPPoE access concentrator (monthly subscribers) ----------
PPPOE_EN="$(python3 -c "import json;print(1 if json.load(open('/etc/pisowifi/config.json')).get('pppoe_enabled') else 0)" 2>/dev/null || echo 0)"
if [ "$PPPOE_EN" = "1" ]; then
    echo "==> PPPoE access concentrator (pppoe-server on $LAN_DEV)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y pppoe ppp || true
    cat > /etc/ppp/pppoe-server-options <<'EOF'
require-chap
ms-dns 10.0.0.1
lcp-echo-interval 10
lcp-echo-failure 2
mtu 1492
noaccomp
default-asyncmap
EOF
    touch /etc/ppp/chap-secrets && chmod 600 /etc/ppp/chap-secrets
    cat > /etc/systemd/system/pppoe-server.service <<EOF
[Unit]
Description=PisoWiFi PPPoE access concentrator
After=pisowifi-net.service network.target
Wants=pisowifi-net.service

[Service]
ExecStart=/usr/sbin/pppoe-server -F -I $LAN_DEV -L 12.0.0.1 -R 12.0.0.2 -N 254 -O /etc/ppp/pppoe-server-options
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable pppoe-server || true
fi

cp "$SRC/systemd/pisowifi.service" /etc/systemd/system/pisowifi.service
systemctl daemon-reload

echo "==> Enabling services"
systemctl enable --now nftables pisowifi-net pisowifi-tune
if [ "$USE_HOSTAPD" = 1 ]; then
    systemctl unmask hostapd
    systemctl enable --now hostapd
fi
systemctl enable --now dnsmasq pisowifi
systemctl restart pisowifi-net nftables dnsmasq pisowifi
[ "$PPPOE_EN" = "1" ] && systemctl restart pppoe-server 2>/dev/null || true

echo
echo "DONE. Portal: http://$GW_IP:8080   Admin: http://$GW_IP:8080/admin (password: changeme)"
echo "Edit /etc/pisowifi/config.json (rates, name, admin password), then: systemctl restart pisowifi"
