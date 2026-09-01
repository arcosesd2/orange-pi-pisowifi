"""Coin-box reconciliation, and refusing money we cannot record.

Two features, one theme: this machine handles cash, so the failures that matter
are the silent ones. A coin that vanishes between the acceptor and the owner's
hand leaves no trace unless something compares the books to the box. A card
that has gone read-only keeps serving customers and records not one sale.

Run directly:  python tests/test_cash_health.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "app"))
os.environ.setdefault("PISOWIFI_MOCK", "1")

import health  # noqa: E402
import main    # noqa: E402

fails = []


def check(desc, got, want):
    ok = got == want
    print("  %-62s %-16s %s" % (desc, got, "ok" if ok else "FAIL -> %r" % (want,)))
    if not ok:
        fails.append(desc)


db = main.db

print("\n=== coin box: the books vs the box ===")
db.record_collection(0, 0)                       # start from a known point
before = db.last_collection_at()
check("a fresh collection resets the tally", db.takings_since(before), (0, 0))

db.record_sale("aa:bb:cc:dd:ee:01", 5, 120)
db.record_sale("aa:bb:cc:dd:ee:02", 10, 260)
db.record_sale("aa:bb:cc:dd:ee:03", 1, 20)
check("takings since last emptied", db.takings_since(before), (16, 3))

# The owner counts the box and finds a peso missing.
db.record_collection(16, 3, counted=15, note="one 1-peso short")
last = db.list_collections(1)[0]
check("collection records what was expected", last["expected_pesos"], 16)
check("...and what was actually counted", last["counted_pesos"], 15)
check("...so the variance is visible",
      last["counted_pesos"] - last["expected_pesos"], -1)
check("the tally restarts after emptying",
      db.takings_since(db.last_collection_at()), (0, 0))

print("\n=== storage health: can we still write a sale? ===")
good = tempfile.mkdtemp()
r = health.check(good)
check("a writable directory is ok", r["ok"], True)
check("...and reports no reason", r["reason"], None)

missing = os.path.join(good, "does", "not", "exist")
r = health.check(missing)
check("an unwritable directory is NOT ok", r["ok"], False)
check("...and says why", bool(r["reason"]), True)
check("...naming money, not disks",
      "coins" in (r["reason"] or "") or "recorded" in (r["reason"] or ""), True)

print("\n=== a machine that cannot record must refuse coins ===")
# This is the money-safety rule: taking cash you cannot write down is worse
# than turning the customer away.
main.storage_health = {"ok": False, "reason": "test fault", "readonly": True,
                       "writable": False, "kernel_errors": [], "checked_at": 0}
c = main.app.test_client()
env = {"REMOTE_ADDR": "10.0.0.50"}
main.client_mac = lambda: "aa:bb:cc:dd:ee:09"
r = c.post("/api/insert", environ_base=env)
check("POST /api/insert is refused", r.status_code, 503)
check("...with a customer-readable reason",
      "out of service" in r.get_data(as_text=True).lower(), True)

body = c.get("/api/status", environ_base=env).get_json()
check("status tells the portal to say so", body.get("out_of_service"), True)

main.storage_health = {"ok": True, "reason": None, "readonly": False,
                       "writable": True, "kernel_errors": [], "checked_at": 0}
body = c.get("/api/status", environ_base=env).get_json()
check("and clears once storage recovers", body.get("out_of_service"), False)

print()
if fails:
    print("%d CASH/HEALTH FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("all cash and storage-health expectations hold")
sys.exit(0)
