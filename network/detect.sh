#!/usr/bin/env bash
# PisoWiFi boot-time provisioning — run by pisowifi-detect.service on EVERY boot,
# before nftables/dnsmasq/hostapd/pisowifi come up.
#
# The point: nothing about *this particular board* is baked into the SD card.
# Interfaces are re-detected and every network config is re-rendered at boot, so
# one card image boots correctly on any Orange Pi with any USB-ethernet dongle
# or WiFi adapter. That is what makes a cloned/flashed card plug-and-play — the
# old installer pinned the dongle's MAC into a udev rule and wrote the interface
# names into the unit files, which only ever worked on the machine it ran on.
#
# Detection (the standard vendo layout falls out of it): the onboard NIC is the
# internet uplink (WAN) and a USB-ethernet dongle is the customer LAN, because
# the onboard NIC sits on the SoC bus and the dongle on the USB bus. With no
# dongle, a WiFi adapter becomes the LAN and hostapd is switched on.
#
# Set "auto_detect_interfaces": false in config.json to pin lan_if/wan_if by hand.
#
#   pisowifi-detect.sh --dry-run    report what it would pick and change nothing
set -u

CFG="${PISOWIFI_CONFIG:-/etc/pisowifi/config.json}"
TPL="${PISOWIFI_NET_TPL:-/opt/pisowifi/net}"
RUN="${PISOWIFI_RUN:-/run/pisowifi}"
# Overridable so the detection can be exercised against a fake sysfs tree.
SYSNET="${PISOWIFI_SYSNET:-/sys/class/net}"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { echo "pisowifi-detect: $*"; }

[ -f "$CFG" ] || { log "no $CFG — nothing to provision"; exit 0; }
mkdir -p "$RUN"

# ---------------------------------------------------------------------------
# settings: config.json, overlaid with the DB (the admin UI writes there)
# ---------------------------------------------------------------------------
eval "$(python3 - "$CFG" <<'PY'
import json, shlex, sqlite3, sys

cfg = json.load(open(sys.argv[1]))

# The admin UI stores runtime settings in the DB, and those override
# config.json. Read them the same way the app does, so a hotspot renamed from a
# phone shows the new SSID after a reboot with no SSH and no file editing.
db = cfg.get("db_path") or ""
if db:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        for k, v in con.execute("SELECT key, value FROM settings"):
            try:
                cfg[k] = json.loads(v)
            except Exception:
                pass
        con.close()
    except Exception:
        pass

def out(k, v):
    print("%s=%s" % (k, shlex.quote(str(v))))

out("CFG_AUTO", 1 if cfg.get("auto_detect_interfaces", True) else 0)
out("CFG_LAN",  cfg.get("lan_if") or "")
out("CFG_WAN",  cfg.get("wan_if") or "")
out("CFG_GW",   cfg.get("gateway_ip") or "10.0.0.1")
out("CFG_VLAN", int(cfg.get("lan_vlan") or 0))
PY
)"

# ---------------------------------------------------------------------------
# classify the real NICs on this board
# ---------------------------------------------------------------------------
# USB or on the SoC bus? modalias is the precise answer ("usb:v0BDAp8152..."
# vs "platform:..." / "of:..."); the path check is a fallback for drivers that
# do not publish one.
is_usb() {
    d="$SYSNET/$1/device"
    if [ -r "$d/modalias" ]; then
        case "$(cat "$d/modalias" 2>/dev/null)" in
            usb:*) return 0 ;;
            ?*)    return 1 ;;
        esac
    fi
    readlink -f "$d" 2>/dev/null | grep -q '/usb'
}

ONBOARD=""; USBETH=""; WIFI=""
for path in "$SYSNET"/*; do
    ifn=${path##*/}
    case "$ifn" in lo|docker*|veth*|br-*|ifb*|ppp*|virbr*|tail*|wg*) continue ;; esac
    # No device symlink = virtual (bridges, VLAN sub-interfaces, tunnels).
    [ -e "$path/device" ] || continue
    if [ -d "$path/wireless" ] || [ -e "$path/phy80211" ]; then
        WIFI="${WIFI} ${ifn}"
    elif is_usb "$ifn"; then
        USBETH="${USBETH} ${ifn}"
    else
        ONBOARD="${ONBOARD} ${ifn}"
    fi
done
first() { set -- $1; echo "${1:-}"; }

WAN_IF=""; LAN_IF=""; USE_HOSTAPD=0

if [ "$CFG_AUTO" != 1 ] && [ -n "$CFG_LAN" ] && [ -n "$CFG_WAN" ]; then
    WAN_IF="$CFG_WAN"; LAN_IF="$CFG_LAN"
    log "auto-detect off — using configured wan=$WAN_IF lan=$LAN_IF"
else
    # Whoever already carries the internet wins — that is the uplink by
    # definition, and it stays right even if someone feeds the internet in
    # through the dongle and hangs the antenna off the onboard port.
    ROUTE_IF="$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')"
    for cand in $ONBOARD $USBETH; do
        [ "$cand" = "$ROUTE_IF" ] && { WAN_IF="$cand"; break; }
    done
    # Nothing routed yet (DHCP still in flight at boot): fall back to the
    # onboard NIC, preferring the conventional uplink names.
    if [ -z "$WAN_IF" ]; then
        for cand in eth0 end0 enp1s0; do
            case " $ONBOARD " in *" $cand "*) WAN_IF="$cand"; break ;; esac
        done
    fi
    [ -n "$WAN_IF" ] || WAN_IF="$(first "$ONBOARD")"
    [ -n "$WAN_IF" ] || WAN_IF="$(first "$USBETH")"

    for cand in $USBETH $ONBOARD; do
        [ "$cand" = "$WAN_IF" ] && continue
        LAN_IF="$cand"; break
    done
    if [ -z "$LAN_IF" ]; then
        LAN_IF="$(first "$WIFI")"
    fi
fi
case " $WIFI " in *" $LAN_IF "*) USE_HOSTAPD=1 ;; esac

if [ -z "$LAN_IF" ]; then
    # Nothing to serve customers on. Leave the box up (so it stays reachable
    # over the uplink to fix) rather than rendering half a hotspot.
    log "ERROR: no LAN interface found — plug in the antenna's USB-ethernet"
    log "       dongle or a WiFi adapter, then reboot. WAN looks like '${WAN_IF:-none}'."
    exit 0
fi

LAN_DEV="$LAN_IF"
[ "$CFG_VLAN" != 0 ] && LAN_DEV="${LAN_IF}.${CFG_VLAN}"
CLIENT_NET="${CFG_GW%.*}.0/24"

log "wan=$WAN_IF lan=$LAN_DEV gw=$CFG_GW hostapd=$USE_HOSTAPD"

if [ "$DRY" = 1 ]; then
    log "candidates: onboard=[${ONBOARD# }] usb-eth=[${USBETH# }] wifi=[${WIFI# }]"
    log "dry run — nothing written"
    exit 0
fi

# Everything downstream (tune.sh, troubleshooting) reads this instead of having
# the interface names baked into its unit file.
cat > "$RUN/env" <<EOF
WAN_IF=$WAN_IF
LAN_IF=$LAN_IF
LAN_DEV=$LAN_DEV
GW_IP=$CFG_GW
CLIENT_NET=$CLIENT_NET
USE_HOSTAPD=$USE_HOSTAPD
EOF

# ---------------------------------------------------------------------------
# render the network configs for THIS board
# ---------------------------------------------------------------------------
python3 - "$CFG" "$TPL" "$LAN_IF" "$LAN_DEV" "$WAN_IF" "$CFG_GW" "$CLIENT_NET" "$USE_HOSTAPD" <<'PY'
import json, os, re, sqlite3, subprocess, sys

cfgp, tpl, lan_if, lan_dev, wan, gw, net, hostapd = sys.argv[1:9]
cfg = json.load(open(cfgp))
db = cfg.get("db_path") or ""
if db:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        for k, v in con.execute("SELECT key, value FROM settings"):
            try:
                cfg[k] = json.loads(v)
            except Exception:
                pass
        con.close()
    except Exception:
        pass


def write(path, text, mode=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


gw_prefix = gw.rsplit(".", 1)[0] + "."

# -- nftables ---------------------------------------------------------------
src = os.path.join(tpl, "nftables.conf")
if os.path.exists(src):
    s = open(src).read()
    # WAN before LAN so "eth0" inside a VLAN LAN like "eth0.5" isn't rewritten
    s = s.replace("eth0", wan).replace("wlan0", lan_dev).replace("10.0.0.0/24", net)
    # Anchored to the define line rather than a blanket substitution: a bare
    # replace of "10.0.0.1" would also rewrite the "10.0.0.1" inside a longer
    # address such as 10.0.0.10 if one ever appears in this template.
    s = re.sub(r"^(define GW_IP\s*=\s*).*$", r"\g<1>" + gw, s, count=1, flags=re.M)
    # Anti-tethering has two halves and they are not equally safe.
    #
    # Egress TTL=1 is the effective one and it cannot lock anybody out: the
    # paying device is the last hop so it receives normally, but if it forwards
    # the packet to a tethered device the TTL hits 0 and dies. On its own this
    # already breaks hotspot sharing in both directions, because the tethered
    # device's replies never come back.
    #
    # The strict half drops client packets whose TTL shows they were forwarded.
    # It only adds anything against a rooted phone that rewrites TTL on
    # forward, and it fails CLOSED -- a client with an unusual initial TTL is
    # cut off entirely, and on a deployment where the AP routes rather than
    # bridges it would cut off everyone. Hence the separate opt-in, and the
    # counter, so a blocked device shows up as a number instead of a mystery.
    anti = antifwd = ""
    if cfg.get("anti_tether"):
        # Observation only: record (MAC, arriving TTL) and count the packets.
        # Nothing is dropped here, and nothing is classified here either -- the
        # rule states no opinion about which TTLs are "normal", which is the
        # whole point. Two different TTLs from one MAC is the tethering signal,
        # and that comparison is made in the app against whatever baseline that
        # particular device turns out to have.
        #
        # The previous version tested `ip ttl { 64, 128, 255 }` here to decide
        # what a device originated itself. Through an AP that routes rather
        # than bridges, every customer arrives one lower and that test matched
        # nothing for anybody -- so the feature silently reported no tethering
        # regardless of what was happening. Measured on hardware: a phone
        # sharing its connection showed 63 for itself and 62 for the device
        # behind it, and neither is in that set.
        antifwd = ("        iifname %s update @ttl_seen "
                   "{ ether saddr . ip ttl counter }" % lan_dev)
        if cfg.get("anti_tether_enforce"):
            # Enforcement is per device and applies ONLY to devices the app has
            # confirmed. TTL 1 still reaches the phone -- it is the last hop --
            # but dies the moment the phone forwards it to anything behind it.
            #
            # Matched on IP, not MAC. This chain hooks postrouting, and at that
            # point the outgoing Ethernet header does not exist yet, so an
            # `ether daddr` test matches nothing and says so to nobody. That is
            # how the first version of this rule shipped: the block set filled
            # correctly, the audit log said "limiting", and the shared phone
            # kept browsing. Counters settled it -- 0 packets on the MAC rule,
            # 30 in twelve seconds on `ip daddr` for the same device. The app
            # keeps the MAC set as the identity and resolves it to an IP from
            # the DHCP lease when it enforces.
            #
            # Scoped to the block set rather than the whole LAN because a
            # blanket `ip ttl set 1` also breaks any customer running an
            # on-device VPN, whose stack decrements once more internally: 1
            # becomes 0 and the packet is discarded before an app ever sees it.
            #
            # IPv4 only. The customer LAN carries no IPv6, and the old hoplimit
            # rule was keyed the same dead way.
            anti = ("    chain mangle_post {\n"
                    "        type filter hook postrouting priority mangle; "
                    "policy accept;\n"
                    "        oifname %s ip daddr @tether_block_ip ip ttl set 1\n"
                    "    }" % lan_dev)
    flowt = offl = ""
    if cfg.get("flow_offload"):
        flowt = ("    flowtable ft {\n"
                 "        hook ingress priority filter\n"
                 "        devices = { %s, %s }\n"
                 "    }" % (lan_dev, wan))
        offl = "        ip protocol { tcp, udp } flow add @ft"
    # Three doors on the uplink, mutually exclusive, most-open first. They exist
    # separately because they have different lifetimes and different blast
    # radii, and collapsing them means opening more than the job needs.
    #
    # wan_management -- SSH + portal + ping. The bench bring-up escape hatch:
    #   without it the uplink is sealed the instant the installer finishes and
    #   nothing can verify the machine it just built. seal.sh forces it off, so
    #   it never reaches a deployed card.
    #
    # wan_ssh -- SSH + ping, and NOT the portal. For an owner who administers
    #   over Tailscale but wants one trusted machine able to shell in over the
    #   wire as well: a laptop with the key gets in, while the admin page stays
    #   off every physical network. wan_management would also have exposed the
    #   portal to whatever the uplink is plugged into, which on a deployed
    #   machine is the shop's router and everything on it.
    #
    # wan_admin -- the admin page only, no SSH. For an owner who administers
    #   from the uplink network rather than over a tunnel. Pairing
    #   admin_lan_access=off with wan_management=off (which sealing does) would
    #   otherwise leave admin reachable from nowhere at all.
    #
    # Note what none of these can do: restrict by source address. The owner's
    # laptop is on DHCP and its address moves, so pinning a rule to it would
    # break silently the next time the router hands out a different one. The
    # control that actually holds is key-only SSH -- reaching the port is not
    # the same as getting in.
    port = int(cfg.get("portal_port") or 8080)
    wanmgmt = ""
    if cfg.get("wan_management"):
        wanmgmt = ("        iifname %s tcp dport { 22, %d } accept\n"
                   "        iifname %s icmp type echo-request accept"
                   % (wan, port, wan))
    elif cfg.get("wan_ssh"):
        wanmgmt = ("        iifname %s tcp dport 22 accept\n"
                   "        iifname %s icmp type echo-request accept"
                   % (wan, wan))
    elif cfg.get("wan_admin"):
        wanmgmt = "        iifname %s tcp dport %d accept" % (wan, port)
    s = s.replace("#@ANTI_TETHER@", anti).replace("#@FLOWTABLE@", flowt) \
         .replace("#@OFFLOAD@", offl).replace("#@WAN_MGMT@", wanmgmt) \
         .replace("#@ANTI_TETHER_FWD@", antifwd)

    # Syntax-check before installing, and fall back feature by feature.
    #
    # This runs at every boot, and nftables.conf is the ONLY source of NAT --
    # `oifname $WAN_IF masquerade` lives there and there is no iptables
    # fallback. So a ruleset that will not load does not mean "no firewall,
    # everyone free"; it means no masquerade, no portal redirect, and a machine
    # that serves nobody. Visible rather than silent, but still dead.
    #
    # The optional features are the risky part, so they are dropped before the
    # base ruleset is. A fresh card in particular has no previous
    # /etc/nftables.conf to keep, so "refuse and keep the old one" would leave
    # it with nothing at all.
    tmp = "/run/pisowifi/nftables.candidate"

    def loads(candidate):
        write(tmp, candidate)
        return subprocess.run(["nft", "-c", "-f", tmp],
                              capture_output=True, text=True)

    chk = loads(s)
    if chk.returncode == 0:
        write("/etc/nftables.conf", s)
    else:
        sys.stderr.write(
            "pisowifi-detect: rendered ruleset is INVALID:\n%s\n"
            % (chk.stderr or "").strip())
        # Retry without the optional extras, so an untested toggle can never
        # take a machine off the air.
        bare = (s.replace(antifwd, "").replace(anti, "")
                if (antifwd or anti) else s)
        chk2 = loads(bare) if bare != s else chk
        if bare != s and chk2.returncode == 0:
            sys.stderr.write(
                "pisowifi-detect: installed WITHOUT tethering detection so the "
                "machine still works. Fix the template, then re-enable it.\n")
            write("/etc/nftables.conf", bare)
        elif os.path.exists("/etc/nftables.conf"):
            sys.stderr.write("pisowifi-detect: keeping the previous ruleset.\n")
        else:
            sys.stderr.write(
                "pisowifi-detect: and there is no previous ruleset to fall "
                "back to -- this machine will not pass traffic.\n")
    try:
        os.unlink(tmp)
    except OSError:
        pass

# -- dnsmasq ----------------------------------------------------------------
src = os.path.join(tpl, "dnsmasq.conf")
if os.path.exists(src):
    s = open(src).read()
    s = s.replace("eth0", wan).replace("wlan0", lan_dev).replace("10.0.0.", gw_prefix)
    write("/etc/dnsmasq.d/pisowifi.conf", s)

# -- hostapd (only when the Pi is its own radio) ----------------------------
if hostapd == "1":
    src = os.path.join(tpl, "hostapd.conf")
    if os.path.exists(src):
        s = open(src).read().replace("wlan0", lan_if)
        # Carry the hotspot name onto the air, so renaming it in the admin UI
        # renames the SSID on the next reboot instead of needing a file edit.
        name = str(cfg.get("hotspot_name") or "").strip()
        if name:
            s = "\n".join(
                ("ssid=" + name[:32]) if ln.startswith("ssid=") else ln
                for ln in s.splitlines()
            ) + "\n"
        write("/etc/hostapd/hostapd.conf", s)

# -- keep the app's view of the wiring in sync ------------------------------
disk = json.load(open(cfgp))
if disk.get("auto_detect_interfaces", True):
    if disk.get("lan_if") != lan_if or disk.get("wan_if") != wan:
        disk["lan_if"], disk["wan_if"] = lan_if, wan
        write(cfgp, json.dumps(disk, indent=2) + "\n", mode=0o600)
PY

if [ "$USE_HOSTAPD" = 1 ]; then
    sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' \
        /etc/default/hostapd 2>/dev/null || true
    # hostapd.service carries a ConditionPathExists on this file, so the radio
    # only starts on boards that actually serve the WiFi themselves.
    : > "$RUN/hostapd.needed"
else
    rm -f "$RUN/hostapd.needed"
fi

# ---------------------------------------------------------------------------
# PPPoE concentrator — a runtime toggle, so its unit is condition-gated the
# same way. Rendering the options here is what keeps ms-dns pointing at this
# machine's real gateway address rather than a value frozen at install time.
# ---------------------------------------------------------------------------
if python3 -c "import json,sys; sys.exit(0 if json.load(open('$CFG')).get('pppoe_enabled') else 1)" 2>/dev/null; then
    cat > /etc/ppp/pppoe-server-options <<EOF
require-chap
ms-dns $CFG_GW
lcp-echo-interval 10
lcp-echo-failure 2
mtu 1492
noaccomp
default-asyncmap
EOF
    : > "$RUN/pppoe.needed"
else
    rm -f "$RUN/pppoe.needed"
fi

# ---------------------------------------------------------------------------
# keep NetworkManager's hands off the customer LAN
# ---------------------------------------------------------------------------
if command -v nmcli >/dev/null 2>&1; then
    mkdir -p /etc/NetworkManager/conf.d
    printf '[keyfile]\nunmanaged-devices=interface-name:%s;interface-name:%s\n' \
        "$LAN_IF" "$LAN_DEV" > /etc/NetworkManager/conf.d/99-pisowifi.conf
    nmcli device set "$LAN_IF" managed no >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# bring the LAN up and address it (what pisowifi-net.service used to do from
# hardcoded interface names)
# ---------------------------------------------------------------------------
ip link set "$LAN_IF" up 2>/dev/null || true
if [ "$CFG_VLAN" != 0 ] && ! ip link show "$LAN_DEV" >/dev/null 2>&1; then
    ip link add link "$LAN_IF" name "$LAN_DEV" type vlan id "$CFG_VLAN" 2>/dev/null || true
fi
ip link set "$LAN_DEV" up 2>/dev/null || true

# Give the customer LAN to systemd-networkd explicitly, instead of only setting
# the address by hand.
#
# Armbian ships a netplan drop-in that matches "Name=e*" with DHCP=yes. That
# glob catches the customer dongle as well as the uplink, so networkd would
# take the interface over about a second after this script addressed it:
#
#   12:06:10  pisowifi-detect: provisioned            <- ip addr replace ran
#   12:06:11  enx...: Link DOWN / Lost carrier
#   12:06:11  Configuring with 10-netplan-all-eth-interfaces.network
#
# and the gateway address was gone. No DHCP for customers, no portal, nothing
# listening on 10.0.0.1 -- on a board whose services all report healthy. It is
# a race, so it would strike some boots and some cards and not others, which is
# the worst way for a fleet to fail.
#
# The filename sorts before the netplan drop-in, and names this specific
# device, so the uplink keeps its DHCP. A fixed filename means a swapped dongle
# overwrites the old one rather than leaving a stale config behind.
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/05-pisowifi-lan.network <<EOF
# Generated by pisowifi-detect at every boot. Do not edit.
[Match]
Name=$LAN_DEV

[Network]
Address=$CFG_GW/24
DHCP=no
LinkLocalAddressing=ipv6
IPv6AcceptRA=no
# The AP may be unplugged when the board boots; the gateway address still has
# to exist, or the portal has nothing to listen on.
ConfigureWithoutCarrier=yes
EOF
networkctl reload 2>/dev/null || systemctl try-reload-or-restart systemd-networkd 2>/dev/null || true

# Belt and braces: apply it immediately too, so the address is up before
# networkd gets round to it rather than a second later.
ip addr replace "${CFG_GW}/24" dev "$LAN_DEV" 2>/dev/null || true

log "provisioned"
exit 0
