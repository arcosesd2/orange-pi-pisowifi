"""Tethering detection.

## Why the first attempt cut customers off the internet

v1 dropped any client packet whose TTL was not an OS initial value (64, 128,
255), on the reasoning that a hotspot decrements TTL when it forwards, so a
lower TTL means a shared device. The arithmetic is right and the conclusion is
wrong, because a phone decrements its own traffic too whenever an on-device VPN
is running -- ad blockers, Private DNS, WARP, corporate VPN clients all take
packets into a local tun and re-inject them, and the phone's own stack forwards
them on the way through. Every packet that customer sends is then one lower,
forever, and v1 dropped all of it. They could not browse at all.

## Why the second attempt detected nothing at all

v2 stopped dropping and started observing, sorting each packet into one of two
MAC sets by testing its TTL against { 64, 128, 255 }: in that set meant "the
device sent this itself", out of it meant "the device forwarded this". A MAC in
both sets at once was the tethering signal.

The idea is right. The test was not, because it quietly assumed nothing sits
between the customer and this machine. Measured on the live machine, through a
CF-EW73 access point, with one phone deliberately sharing to another:

    66:11:ee:1a:ec:6d . 63      8 packets      <- the phone itself
    66:11:ee:1a:ec:6d . 62      1724 packets   <- the phone it was sharing to

The AP routes rather than bridges, so it decrements once and the phone's OWN
traffic arrives at 63. Nothing any customer ever sent matched { 64, 128, 255 }.
The "own traffic" set stayed empty for everybody, permanently, and because
tethering required membership of both sets it could never be reported. The
admin page said "none detected" while a phone shared its connection all evening.

## What actually identifies tethering

Not a low TTL, and not a particular TTL -- *two different TTLs from one MAC*.

    phone alone                 -> one value                    not sharing
    phone with a local VPN      -> one value, lower             NOT sharing
    phone sharing to a laptop   -> two values, one hop apart    sharing

A device only ever originates at one initial TTL. So the baseline is simply the
highest TTL seen from that MAC -- learned per device, never assumed -- and any
traffic arriving below its own baseline is traffic it forwarded for something
else. How many routers sit in between stops mattering entirely.

## The volume floor

An on-device VPN can leak a handful of packets outside its tunnel and show a
second TTL, which would look like sharing. So a second population has to carry
real weight before it counts: `MIN_FWD_PACKETS` below. In the measurement above
the shared device sent 1724 packets and the phone 8 -- the two cases are orders
of magnitude apart, not marginal.

Enforcement stays opt-in and per device, applies only to MACs confirmed here,
and an owner can always overrule it from the admin page.
"""
import re
import subprocess

TABLE = "inet pisowifi"
SEEN, BLOCK, BLOCK_IP = "ttl_seen", "tether_block", "tether_block_ip"

# Packets a below-baseline population must carry before it counts as sharing
# rather than a VPN's stray leak. Deliberately low: a device being shared to
# passes this within seconds of loading one page, while leaked packets are
# counted in single digits.
MIN_FWD_PACKETS = 25

_MAC = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)
_IP = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
# "aa:bb:cc:dd:ee:ff . 62 [expires 9m51s] counter packets 1724 bytes 356821"
_ELEM = re.compile(
    r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s*\.\s*(\d{1,3})", re.I)
_PKTS = re.compile(r"packets\s+(\d+)")


def _nft(*args):
    try:
        return subprocess.run(("nft",) + args, capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 1, "", "nft unavailable")


def _elements(set_name):
    """Raw element text of an nft set, or "" on any failure.

    This is diagnostics. It must never be the reason the portal stops working,
    so every failure path returns empty rather than raising.
    """
    r = _nft("list", "set", *TABLE.split(), set_name)
    if r.returncode != 0:
        return ""
    body = r.stdout.split("elements = {", 1)
    return body[1] if len(body) > 1 else ""


def _members(set_name):
    """MACs in a plain MAC-keyed set."""
    return {m.group(1).lower() for m in _MAC.finditer(_elements(set_name))}


def pairs():
    """{mac: {ttl: packets}} as currently recorded by the packet path.

    Elements are comma separated and no single element contains a comma, so
    splitting on it is safe and keeps each counter attached to its own pair.
    """
    out = {}
    for chunk in _elements(SEEN).split(","):
        m = _ELEM.search(chunk)
        if not m:
            continue
        mac, ttl = m.group(1).lower(), int(m.group(2))
        p = _PKTS.search(chunk)
        packets = int(p.group(1)) if p else 0
        d = out.setdefault(mac, {})
        d[ttl] = d.get(ttl, 0) + packets
    return out


def observe():
    """Current picture, per MAC.

    Returns {mac: {"baseline": int, "ttls": {ttl: packets}, "forwarded": int,
                   "tethering": bool}} where `baseline` is that device's own
    highest observed TTL and `forwarded` is the packet count arriving below it.
    """
    out = {}
    for mac, ttls in pairs().items():
        if not ttls:
            continue
        baseline = max(ttls)
        forwarded = sum(n for t, n in ttls.items() if t < baseline)
        out[mac] = {
            "baseline": baseline,
            "ttls": dict(sorted(ttls.items(), reverse=True)),
            "forwarded": forwarded,
            "tethering": forwarded >= MIN_FWD_PACKETS,
        }
    return out


def tethering_macs():
    return {mac for mac, s in observe().items() if s["tethering"]}


def blocked_macs():
    return _members(BLOCK)


def enforce(macs, resolve):
    """Add confirmed tethering devices to the enforcement sets.

    `resolve` maps a MAC to its current IP (or None). Both are written: the
    MAC is the device's identity everywhere else, but the TTL rule matches on
    IP, because it hooks postrouting and no Ethernet header exists there yet.
    A device whose IP cannot be resolved is NOT recorded as blocked -- that
    would report an enforcement that is not happening -- and is simply retried
    next cycle, by which time it will have a lease if it is really a customer.

    Adding is idempotent and refreshes the timeout, so a device that stops
    tethering ages out on its own rather than needing to be removed, and a
    device that renews onto a new address is followed. Returns the MACs
    actually pushed.
    """
    done = set()
    for mac in macs:
        if not _MAC.fullmatch(mac):
            continue                       # never interpolate unvalidated text
        mac = mac.lower()
        ip = resolve(mac) if resolve else None
        if not ip or not _IP.fullmatch(ip):
            continue
        r_ip = _nft("add", "element", *TABLE.split(), BLOCK_IP, "{ %s }" % ip)
        r_mac = _nft("add", "element", *TABLE.split(), BLOCK, "{ %s }" % mac)
        if r_ip.returncode == 0 and r_mac.returncode == 0:
            done.add(mac)
    return done


def release(mac, ip=None):
    """Stop enforcing against one device, for an owner overruling a detection.

    The IP entry is removed too when the caller can supply it; without it the
    MAC entry goes and the IP ages out on its own timeout, which is bounded.
    """
    if not _MAC.fullmatch(mac or ""):
        return False
    if ip and _IP.fullmatch(ip):
        _nft("delete", "element", *TABLE.split(), BLOCK_IP, "{ %s }" % ip)
    return _nft("delete", "element", *TABLE.split(), BLOCK,
                "{ %s }" % mac).returncode == 0


def summary(enforcing):
    """Snapshot for the dashboard."""
    seen = observe()
    tethering = sorted(m for m, s in seen.items() if s["tethering"])
    # A second TTL exists but has not carried enough traffic to be called
    # sharing. Usually a VPN leaking a few packets outside its tunnel. Shown
    # rather than hidden: these are the devices v1 cut off the internet, and
    # naming them is what makes "we are not blocking these" auditable.
    borderline = sorted(m for m, s in seen.items()
                        if s["forwarded"] and not s["tethering"])
    return {
        "enabled": True,
        "enforcing": bool(enforcing),
        "watched": len(seen),
        "tethering": tethering,
        "blocked": sorted(blocked_macs()),
        "vpn_like": borderline,
        # Per-device TTL evidence, so a detection can be checked rather than
        # trusted. This is the table that would have exposed the v2 bug in
        # minutes instead of an evening.
        "detail": {m: {"baseline": s["baseline"], "ttls": s["ttls"],
                       "forwarded": s["forwarded"]}
                   for m, s in sorted(seen.items())},
        "threshold": MIN_FWD_PACKETS,
    }
