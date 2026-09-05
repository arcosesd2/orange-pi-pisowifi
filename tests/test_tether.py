"""Tethering detection — and specifically, the two ways it has been wrong.

v1 dropped any client packet whose TTL was below an OS initial value. That is
also what an on-device VPN produces, so v1 cut those customers off the internet
completely.

v2 stopped dropping and started observing, but decided what a device "sent
itself" by testing the TTL against { 64, 128, 255 }. Through an access point
that routes rather than bridges, every customer arrives one lower than that and
the test matched nothing for anybody -- so it reported no tethering, ever, while
a phone shared its connection all evening.

The distinction this suite exists to protect:

    one TTL population from a MAC   -> that device, however it routes internally
    two at once, with real volume   -> it is forwarding for something else

and, just as importantly, that NO absolute TTL value appears in the logic. The
baseline is whatever that device's own highest TTL turns out to be.

Run directly:  python tests/test_tether.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "app"))

import tether  # noqa: E402

PHONE = "aa:bb:cc:dd:ee:01"
VPN_PHONE = "aa:bb:cc:dd:ee:02"
TETHERING = "aa:bb:cc:dd:ee:03"
LEAKY_VPN = "aa:bb:cc:dd:ee:04"

fails = []


def check(desc, got, want):
    ok = got == want
    print("  %-60s %-20s %s" % (desc, got, "ok" if ok else "FAIL -> %r" % (want,)))
    if not ok:
        fails.append(desc)


def fake_seen(*elements, **kw):
    """Stand in for the kernel's ttl_seen set.

    Takes (mac, ttl, packets) triples and renders them the way nft actually
    prints them, so the parser is under test too and not just the arithmetic.
    """
    blocked = kw.get("blocked", ())
    text = ", ".join(
        "%s . %d counter packets %d bytes %d" % (m, t, p, p * 207)
        for m, t, p in elements)

    def _elements(name):
        if name == tether.SEEN:
            return " " + text + " }"
        return " " + ", ".join(blocked) + " }" if blocked else " }"
    tether._elements = _elements


print("\n=== Who counts as tethering ===")

fake_seen((PHONE, 64, 900))
check("plain phone, one TTL, is not tethering", tether.tethering_macs(), set())

# The v1 killer: every packet one lower, but consistently ONE value, because
# the phone's own VPN forwards it internally before it ever leaves.
fake_seen((VPN_PHONE, 63, 900))
check("phone with an on-device VPN is NOT tethering",
      tether.tethering_macs(), set())

fake_seen((TETHERING, 64, 40), (TETHERING, 63, 1200))
check("phone showing two TTLs with volume IS tethering",
      tether.tethering_macs(), {TETHERING})

# A VPN that leaks a few packets outside its tunnel shows a second TTL too.
# Volume is what separates it from a device actually being shared to.
fake_seen((LEAKY_VPN, 63, 900), (LEAKY_VPN, 62, 6))
check("VPN leaking a handful of packets is not tethering",
      tether.tethering_macs(), set())
check("...but it is surfaced to the operator, not hidden",
      LEAKY_VPN in tether.summary(False)["vpn_like"], True)

print("\n=== The v2 regression: no absolute TTL may appear in the logic ===")

# Measured on the live machine through a CF-EW73 AP with one phone sharing to
# another. The phone's OWN traffic arrives at 63, not 64, because the AP
# decrements once. v2 tested against { 64, 128, 255 } and so saw nothing here.
HW = "66:11:ee:1a:ec:6d"
fake_seen((HW, 63, 8), (HW, 62, 1724))
check("sharing is detected when the phone's own TTL is 63, not 64",
      tether.tethering_macs(), {HW})
check("baseline is learned from the device, not assumed",
      tether.observe()[HW]["baseline"], 63)
check("forwarded volume is counted below that learned baseline",
      tether.observe()[HW]["forwarded"], 1724)

# Same shape again, moved down the range. Nothing about the logic may care.
fake_seen((HW, 58, 8), (HW, 57, 1724))
check("and still detected four hops further away (58/57)",
      tether.tethering_macs(), {HW})

fake_seen((HW, 128, 40), (HW, 127, 900))
check("and on a Windows baseline of 128", tether.tethering_macs(), {HW})

print("\n=== Link-local traffic is not forwarding (the 13-device false positive) ===")

# Measured on the live machine with 13 devices connected. mDNS/SSDP go out at
# TTL 255, so an ordinary phone shows a handful of packets at 254 alongside all
# its real traffic at 63. Taking the plain max as the baseline made 254 the
# reference and counted every normal packet as "arriving below it": two of the
# 13 were flagged and one was actually blocked. None was tethering.
REAL = "52:4c:fd:42:1f:e5"
fake_seen((REAL, 254, 369), (REAL, 63, 419532))
check("phone with mDNS at 254 is NOT tethering", tether.tethering_macs(), set())
check("baseline comes from the dominant population, not the max",
      tether.observe()[REAL]["baseline"], 63)
check("its normal traffic is not counted as forwarded",
      tether.observe()[REAL]["forwarded"], 0)

fake_seen(("32:1e:2e:5e:b6:d7", 254, 5), ("32:1e:2e:5e:b6:d7", 63, 277691))
check("and again with only five discovery packets",
      tether.tethering_macs(), set())

# But a device that BOTH does discovery and genuinely shares must still be
# caught: the dominant population is 63/62, and 62 is below its baseline.
BOTH = "aa:bb:cc:dd:ee:09"
fake_seen((BOTH, 255, 6), (BOTH, 63, 100), (BOTH, 62, 5000))
check("discovery traffic does not mask genuine sharing",
      tether.tethering_macs(), {BOTH})
check("...and the baseline is still the real one", tether.observe()[BOTH]["baseline"], 63)
check("...counting only the traffic below it", tether.observe()[BOTH]["forwarded"], 5000)

# The rest of that live sample: every one of these is an ordinary device.
for mac, ttl, n in [("20:64:cb:8c:7b:29", 63, 12382), ("50:28:4a:0a:20:62", 127, 4466),
                    ("aa:ce:63:c8:8c:0b", 63, 691469), ("9a:59:25:d8:d4:4c", 63, 115)]:
    fake_seen((mac, ttl, n))
    check("single-population device %s is left alone" % mac,
          tether.tethering_macs(), set())

print("\n=== Parser handles what nft actually prints ===")


def _with_expiry(name):
    if name != tether.SEEN:
        return " }"
    return (" 66:11:ee:1a:ec:6d . 62 expires 9m51s404ms counter packets 1724"
            " bytes 356821, 66:11:ee:1a:ec:6d . 63 expires 30s788ms counter"
            " packets 8 bytes 429 }")


tether._elements = _with_expiry
check("an 'expires' field does not break the counter",
      tether.observe()[HW]["forwarded"], 1724)
check("both TTLs survive the parse",
      sorted(tether.pairs()[HW]), [62, 63])

tether._elements = lambda name: ""
check("an empty set yields no devices and does not raise",
      tether.observe(), {})

print("\n=== A realistic mix, all at once ===")
fake_seen((PHONE, 64, 900), (VPN_PHONE, 63, 700),
          (LEAKY_VPN, 63, 400), (LEAKY_VPN, 62, 5),
          (TETHERING, 63, 30), (TETHERING, 62, 2100))
check("only the genuine sharer is flagged",
      tether.tethering_macs(), {TETHERING})
s = tether.summary(False)
check("all four devices are being watched", s["watched"], 4)
check("the sharer never appears as merely borderline",
      TETHERING in s["vpn_like"], False)
check("evidence is published for every device", sorted(s["detail"]),
      sorted([PHONE, VPN_PHONE, LEAKY_VPN, TETHERING]))

print("\n=== Enforcement input is validated ===")
pushed = []


def spy(*args):
    pushed.append(args)
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


tether._nft = spy
ips = {PHONE: "10.0.0.7"}
got = tether.enforce({"not-a-mac", "; nft flush ruleset #", PHONE}, ips.get)
check("only well-formed MACs reach the nft command line",
      sorted(a[-1] for a in pushed), sorted(["{ %s }" % PHONE, "{ 10.0.0.7 }"]))
check("the MAC is reported as pushed", got, {PHONE})

# The TTL rule hooks postrouting, where no Ethernet header exists yet, so it
# has to match on IP. A device with no resolvable IP therefore cannot be
# enforced, and must not be reported as if it were.
pushed.clear()
got = tether.enforce({TETHERING}, lambda m: None)
check("no IP -> nothing written to either set", pushed, [])
check("...and it is not reported as blocked", got, set())

pushed.clear()
tether.enforce({PHONE}, lambda m: "10.0.0.7; nft flush ruleset")
check("a malformed IP from the resolver is refused", pushed, [])

pushed.clear()
check("release() refuses a malformed MAC", tether.release("bogus"), False)
check("release() sent no command for it", pushed, [])

pushed.clear()
tether.release(PHONE, "10.0.0.7")
check("release() clears both the MAC and the IP entry",
      sorted(a[-1] for a in pushed), sorted(["{ %s }" % PHONE, "{ 10.0.0.7 }"]))

print()
if fails:
    print("%d TETHER FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("all tethering expectations hold")
sys.exit(0)
