"""Tethering detection — and specifically, the case that broke customers.

v1 dropped any client packet whose TTL was below an OS initial value. That is
also what an on-device VPN produces (ad blockers, Private DNS, WARP, corporate
VPN clients re-inject through a local tun and the phone's own stack decrements
on the way through), so v1 cut those customers off the internet completely.

The distinction this suite exists to protect:

    one TTL population from a MAC  -> that device, however it routes internally
    two at once                    -> it is forwarding for something else

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
OWNER = "aa:bb:cc:dd:ee:04"

fails = []


def check(desc, got, want):
    ok = got == want
    print("  %-62s %-18s %s" % (desc, got, "ok" if ok else "FAIL -> %r" % (want,)))
    if not ok:
        fails.append(desc)


def fake_sets(norm, fwd, blocked=()):
    """Stand in for the kernel's view of the two observation sets."""
    def _members(name):
        return {tether.NORM: set(norm), tether.FWD: set(fwd),
                tether.BLOCK: set(blocked)}[name]
    tether._members = _members


print("\n=== Who counts as tethering ===")

# The phone talks only for itself: one TTL population.
fake_sets(norm={PHONE}, fwd=set())
check("plain phone (TTL 64 only) is not tethering",
      tether.tethering_macs(), set())

# The v1 killer. Every packet one lower, but consistently one value, because
# the phone's own VPN forwards it internally before it ever leaves.
fake_sets(norm=set(), fwd={VPN_PHONE})
check("phone with an on-device VPN (TTL 63 only) is NOT tethering",
      tether.tethering_macs(), set())

# Two populations from one MAC: its own traffic and something behind it.
fake_sets(norm={TETHERING}, fwd={TETHERING})
check("phone showing BOTH TTLs is tethering",
      tether.tethering_macs(), {TETHERING})

print("\n=== A realistic mix, all four at once ===")
fake_sets(norm={PHONE, TETHERING, OWNER}, fwd={VPN_PHONE, TETHERING})
check("only the genuine sharer is flagged",
      tether.tethering_macs(), {TETHERING})

seen = tether.observe()
check("VPN phone is reported as forwarded-only",
      (seen[VPN_PHONE]["forwarded"], seen[VPN_PHONE]["own"]), (True, False))
check("VPN phone is visible to the operator, not hidden",
      VPN_PHONE in tether.summary(False)["vpn_like"], True)
check("VPN phone never appears as tethering",
      VPN_PHONE in tether.summary(False)["tethering"], False)

print("\n=== Enforcement input is validated ===")
pushed = []
tether._nft = lambda *a: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
_real_nft = tether._nft


def spy(*args):
    pushed.append(args)
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


tether._nft = spy
tether.enforce({"not-a-mac", "; nft flush ruleset #", PHONE})
macs_sent = [a[-1] for a in pushed]
check("only well-formed MACs reach the nft command line",
      macs_sent, ["{ %s }" % PHONE])

pushed.clear()
check("release() refuses a malformed MAC", tether.release("bogus"), False)
check("release() sent no command for it", pushed, [])

print()
if fails:
    print("%d TETHER FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("all tethering expectations hold")
sys.exit(0)
