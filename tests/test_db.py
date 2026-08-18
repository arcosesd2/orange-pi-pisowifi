"""DB-layer regression tests: wallet speed carry-over + audit pruning.

Runs against a throwaway database — safe to run anywhere, touches nothing real.

    python tests/test_db.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from db import Db  # noqa: E402

MAC = "aa:bb:cc:dd:ee:01"
fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(label)


d = Db(os.path.join(tempfile.mkdtemp(), "t.db"))

# the wallets.speed migration ran on open
with d._conn() as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(wallets)")]
check("wallets has speed column", "speed" in cols, True)

d.wallet_add(MAC, 300, "fast")
check("balance after add", d.wallet_balance(MAC), 300)
check("speed stored", d.wallet_speed(MAC), "fast")

# a later add without a speed must not wipe the stored profile (COALESCE)
d.wallet_add(MAC, 60)
check("balance accumulates", d.wallet_balance(MAC), 360)
check("speed survives speedless add", d.wallet_speed(MAC), "fast")

d.wallet_add(MAC, 60, "default")
check("speed replaced when given", d.wallet_speed(MAC), "default")

check("take returns requested", d.wallet_take(MAC, 120), 120)
check("balance after take", d.wallet_balance(MAC), 300)
check("speed survives take", d.wallet_speed(MAC), "default")
check("speed for unknown mac", d.wallet_speed("00:00:00:00:00:00"), None)

# audit pruning bounds SD-card writes; the sales ledger is never touched
for i in range(120):
    d.log_audit(f"action {i}")
d.record_sale(MAC, 5, 120)
check("pruned the excess", d.prune_audit(keep=50), 70)
check("kept the cap", len(d.list_audit(1000)), 50)
check("newest survived", d.list_audit(1)[0]["action"], "action 119")
check("sales untouched", d.sales_summary()["all_time"]["count"], 1)
check("prune is idempotent", d.prune_audit(keep=50), 0)

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
