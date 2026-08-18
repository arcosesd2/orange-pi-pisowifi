"""Whole-app smoke test: every page renders, and the DB survives concurrent use.

The concurrency half guards the per-thread connection cache in db.py — the
reconcile, scheduler, watchdog and web threads all share one Db object, and
sqlite3 connections cannot be shared across threads.

Run on a dev PC (mock mode).

    python tests/test_smoke.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import main  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(label)


if not main.MOCK:
    print("refusing to run against real hardware — dev PC only")
    sys.exit(1)

c = main.app.test_client()

# ---- client-facing ----
check("portal renders", c.get("/").status_code, 200)
check("status api", c.get("/api/status").status_code, 200)
check("capport api", c.get("/api/capport").status_code, 200)
check("captive probe redirects", c.get("/generate_204").status_code, 302)
check("unknown path redirects to portal", c.get("/anything/else").status_code, 302)

# ---- admin (log in without touching the stored password) ----
with c.session_transaction() as s:
    s["admin"] = True

for path in ("/admin", "/admin/vouchers", "/admin/devices", "/admin/audit",
             "/admin/branding", "/admin/schedules", "/admin/pppoe", "/admin/diag"):
    check(f"renders {path}", c.get(path).status_code, 200)

for path in ("/admin/backup", "/admin/backup?sales=1",
             "/admin/sales.csv", "/admin/vouchers.csv"):
    check(f"downloads {path}", c.get(path).status_code, 200)

# the streaming CSV export must actually stream to completion
check("sales csv has a header",
      c.get("/admin/sales.csv").get_data(as_text=True).startswith("id,mac,pesos"), True)

# ---- CSRF still guards state-changing admin posts ----
check("admin post without token is rejected",
      c.post("/admin/settings", data={"hotspot_name": "x"}).status_code, 400)

# ---- concurrent DB use across threads ----
errors = []


def hammer(n):
    mac = f"aa:bb:cc:00:00:{n:02x}"
    try:
        for _ in range(40):
            main.db.get_setting("minutes_per_peso")
            main.db.wallet_add(mac, 1)
            main.db.wallet_balance(mac)
            main.db.active_sessions()
        main.db.wallet_take(mac)
    except Exception as e:                       # noqa: BLE001 - reporting it is the point
        errors.append(f"thread {n}: {e!r}")


threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("8 threads hammering the db raise nothing", errors, [])

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
