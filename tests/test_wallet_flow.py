"""End-to-end: buy 'fast' time -> save to wallet -> redeem -> speed preserved.

Regression test for the bug where redeeming wallet credit silently downgraded
the client to default_speed.

Run on a dev PC (mock mode). It uses the dev database, and restores the
settings and rows it touches on the way out.

    python tests/test_wallet_flow.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import main  # noqa: E402

MAC = "aa:bb:cc:dd:ee:01"   # what client_mac() returns in MOCK mode
fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(label)


if not main.MOCK:
    print("refusing to run against real hardware — dev PC only")
    sys.exit(1)

# remember what we're about to change
_prev = {k: main.db.get_setting(k) for k in ("wallet_enabled", "default_speed")}
main.db.set_setting("wallet_enabled", True)
main.db.set_setting("default_speed", "unlimited")
main.db.delete_session(MAC)
main.db.wallet_take(MAC)          # zero the balance

try:
    c = main.app.test_client()

    # 1. buy 10 minutes on the "fast" profile
    main._grant(MAC, 10, source="coin", pesos=5, speed="fast")
    check("session speed after purchase", main.db.get_session(MAC)["speed"], "fast")

    # 2. save the remaining time to the wallet and disconnect
    r = c.post("/api/wallet/save")
    check("save ok", r.status_code, 200)
    check("saved ~10 min", r.get_json()["saved_minutes"], 9)   # 9m59s floors to 9
    check("wallet remembers the profile", main.db.wallet_speed(MAC), "fast")
    check("session cleared", main.db.get_session(MAC), None)

    # 3. redeem it — this used to silently drop the client to default_speed
    r = c.post("/api/wallet/use")
    check("use ok", r.status_code, 200)
    check("granted the banked minutes", r.get_json()["minutes"], 9)
    check("SPEED PRESERVED on redeem", main.db.get_session(MAC)["speed"], "fast")
finally:
    main.db.delete_session(MAC)
    main.db.wallet_take(MAC)
    for k, v in _prev.items():
        if v is not None:
            main.db.set_setting(k, v)

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
