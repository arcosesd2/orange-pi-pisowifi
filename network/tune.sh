#!/bin/sh
# PisoWiFi runtime tuning — run once at boot by pisowifi-tune.service.
# Args: the network interfaces to steer (WAN LAN [VLAN]).
#  - Packet steering (RPS/XPS): spread NIC softirqs across all CPU cores so the
#    Orange Pi One's 4 cores share network load instead of piling on cpu0.
#  - conntrack hashsize: faster connection lookups under many clients.
#  - CPU governor: ondemand (ramps up under load, saves power when idle).

NPROC="$(nproc 2>/dev/null || echo 1)"
MASK="$(printf '%x' $(( (1 << NPROC) - 1 )))"   # all-cores bitmask, e.g. 4 cores -> f

for IF in "$@"; do
    [ -d "/sys/class/net/$IF" ] || continue
    for q in /sys/class/net/"$IF"/queues/rx-*/rps_cpus; do
        [ -e "$q" ] && echo "$MASK" > "$q" 2>/dev/null || true
    done
    for q in /sys/class/net/"$IF"/queues/tx-*/xps_cpus; do
        [ -e "$q" ] && echo "$MASK" > "$q" 2>/dev/null || true
    done
done

[ -e /sys/module/nf_conntrack/parameters/hashsize ] && \
    echo 32768 > /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || true

for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -e "$g" ] && echo ondemand > "$g" 2>/dev/null || true
done

exit 0
