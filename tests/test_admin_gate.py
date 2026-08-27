"""Who may open /admin, in each access mode.

The property that matters commercially: **the customer portal is never gated.**
Locking admin down must not stop a customer inserting a coin, or the machine
stops earning while looking perfectly healthy.

Run directly:  python tests/test_admin_gate.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "app"))
os.environ.setdefault("PISOWIFI_MOCK", "1")

import main  # noqa: E402

fails = []


def check(desc, got, want):
    ok = got == want
    print("  %-64s %-14s %s" % (desc, got, "ok" if ok else "FAIL -> %r" % (want,)))
    if not ok:
        fails.append(desc)


def gate(mode, remote_addr, tailscale_running=True, whitelisted=False,
         has_whitelist=True):
    """Run _admin_lan_denied() for one hypothetical request."""
    main.db.set_setting("admin_lan_access", mode)
    main.tailscale.status = lambda: {
        "installed": True, "running": tailscale_running,
        "ip4": "100.101.102.103" if tailscale_running else None}
    main.db.has_whitelist = lambda: has_whitelist
    main.db.is_whitelisted = lambda mac: whitelisted
    main.client_mac = lambda: "aa:bb:cc:dd:ee:01"
    with main.app.test_request_context("/admin", environ_base={
            "REMOTE_ADDR": remote_addr}):
        return "ALLOWED" if main._admin_lan_denied() is None else "DENIED"


CUSTOMER, UPLINK, TS, LOCAL = "10.0.0.50", "192.168.254.20", "100.101.102.9", "127.0.0.1"

print("\n=== tailscale mode: only the tunnel, and the box itself ===")
check("customer on the hotspot", gate("tailscale", CUSTOMER), "DENIED")
check("whitelisted customer device", gate("tailscale", CUSTOMER, whitelisted=True),
      "DENIED")
check("someone on the uplink network", gate("tailscale", UPLINK), "DENIED")
check("over Tailscale", gate("tailscale", TS), "ALLOWED")
check("the machine itself", gate("tailscale", LOCAL), "ALLOWED")

print("\n=== a Tailscale source does not count when the daemon is down ===")
# 100.64/10 is also CGNAT space. Without the daemon check, anything arriving
# with such a source would be trusted on a machine that never set Tailscale up.
check("100.x source, tailscaled not running",
      gate("tailscale", TS, tailscale_running=False), "DENIED")

print("\n=== the other modes still behave ===")
check("off:       customer denied", gate("off", CUSTOMER), "DENIED")
check("off:       uplink allowed", gate("off", UPLINK), "ALLOWED")
check("whitelist: unlisted customer denied",
      gate("whitelist", CUSTOMER, whitelisted=False), "DENIED")
check("whitelist: listed customer allowed",
      gate("whitelist", CUSTOMER, whitelisted=True), "ALLOWED")
check("whitelist: open while no device is whitelisted yet",
      gate("whitelist", CUSTOMER, has_whitelist=False), "ALLOWED")
check("any:       customer allowed (recovery)", gate("any", CUSTOMER), "ALLOWED")

print("\n=== THE CUSTOMER PORTAL IS NEVER GATED ===")
# _admin_guard only inspects /admin. If this ever changes, a locked-down
# machine silently stops taking money.
main.db.set_setting("admin_lan_access", "tailscale")
main.tailscale.status = lambda: {"installed": True, "running": True,
                                 "ip4": "100.101.102.103"}
for path in ("/", "/api/status", "/buy", "/static/admin.css"):
    with main.app.test_request_context(path, environ_base={
            "REMOTE_ADDR": CUSTOMER}):
        blocked = main._admin_guard() is not None
    check("customer may reach %-18s in tailscale-only mode" % path,
          "REACHABLE" if not blocked else "BLOCKED", "REACHABLE")

with main.app.test_request_context("/admin", environ_base={
        "REMOTE_ADDR": CUSTOMER}):
    blocked = main._admin_guard() is not None
check("customer may reach /admin                  in tailscale-only mode",
      "REACHABLE" if not blocked else "BLOCKED", "BLOCKED")

print()
if fails:
    print("%d ADMIN GATE FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("all admin-gate expectations hold")
sys.exit(0)
