"""Security regression tests for the admin exposure controls.

Covers the three things that made the panel unsafe to deploy:
  * /admin sits on the customer-reachable portal port -> it must refuse
    customers at the door, while never locking the owner out of a fresh box
  * a machine on the shipped password must not be usable until it changes
  * login guessing must not reset by picking a new IP

Run on a dev PC (mock mode). Restores the state it touches.

    python tests/test_security.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import main  # noqa: E402

MAC = "aa:bb:cc:dd:ee:01"          # what client_mac() returns in MOCK mode
OTHER = "aa:bb:cc:dd:ee:99"
LAN = {"REMOTE_ADDR": "10.0.0.50"}  # a customer on the hotspot
BOX = {"REMOTE_ADDR": "127.0.0.1"}  # the machine itself / Tailscale / other iface
REAL_PW = "a-real-password"
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

# The door policy keys off "does ANY whitelisted device exist", so the dev
# database's own entries have to be out of the way for these tests to mean
# anything. Remember them and put them back at the end.
_saved_devices = main.db.list_devices()


def reset(whitelist=(), lan_access="whitelist", default_pw=False):
    for d in main.db.list_devices():
        main.db.delete_device(d["mac"])
    for m in whitelist:
        main.db.set_device(m, "white", "test")
    main.db.set_setting("admin_lan_access", lan_access)
    main.db.set_admin_password(REAL_PW, is_default=default_pw)
    main._LOGIN_FAILS.clear()


try:
    # ---- door policy -----------------------------------------------------
    reset(whitelist=())
    check("fresh box: LAN can still reach admin (no whitelist yet)",
          c.get("/admin/login", environ_base=LAN).status_code, 200)

    reset(whitelist=(OTHER,))
    check("locked down: customer on LAN is refused",
          c.get("/admin/login", environ_base=LAN).status_code, 403)
    check("refusal explains the recovery path",
          "admin_lan_access" in c.get("/admin", environ_base=LAN).get_data(as_text=True),
          True)
    check("locked down: the box itself still gets in",
          c.get("/admin/login", environ_base=BOX).status_code, 200)

    reset(whitelist=(OTHER, MAC))
    check("whitelisted device gets in from the LAN",
          c.get("/admin/login", environ_base=LAN).status_code, 200)

    reset(whitelist=(OTHER,), lan_access="any")
    check('opt-out ("any") restores old behavior',
          c.get("/admin/login", environ_base=LAN).status_code, 200)

    # the guard must not touch the customer portal
    reset(whitelist=(OTHER,))
    check("portal is unaffected by the admin guard",
          c.get("/", environ_base=LAN).status_code, 200)
    check("status api is unaffected",
          c.get("/api/status", environ_base=LAN).status_code, 200)

    # ---- forced first-run password change --------------------------------
    reset(whitelist=(), default_pw=True)
    with c.session_transaction() as s:
        s["admin"] = True
    r = c.get("/admin", environ_base=BOX)
    check("default password: admin bounces to setup", r.status_code, 302)
    check("bounces to the right place", "/admin/setup" in r.headers["Location"], True)
    check("setup page itself is reachable",
          c.get("/admin/setup", environ_base=BOX).status_code, 200)

    with c.session_transaction() as s:
        s["csrf"] = "tok"

    def setup(pw, confirm=None, wl="off"):
        return c.post("/admin/setup", environ_base=BOX, data={
            "password": pw, "confirm": confirm if confirm is not None else pw,
            "whitelist_me": wl, "csrf": "tok"})

    check("rejects a short password", "at least 8" in setup("abc").get_data(as_text=True), True)
    # (the rendered page HTML-escapes the apostrophe, so match on the plain part)
    check("rejects a mismatch",
          "match" in setup("longenough1", "different1").get_data(as_text=True), True)
    check("rejects the shipped default",
          "shipped default" in setup("changeme").get_data(as_text=True), True)
    check("still flagged as default after failed attempts",
          main.db.admin_password_is_default(), True)

    r = setup("a-good-password", wl="on")
    check("accepts a good password", r.status_code, 302)
    check("no longer flagged as default", main.db.admin_password_is_default(), False)
    check("plaintext seed dropped", main.db.get_setting("admin_password"), "")
    check("setup whitelisted this device", main.db.is_whitelisted(MAC), True)
    check("admin now opens normally", c.get("/admin", environ_base=BOX).status_code, 200)

    # ---- login lockout ---------------------------------------------------
    reset(whitelist=())
    with c.session_transaction() as s:
        s.clear()
    for i in range(5):
        c.post("/admin/login", environ_base=BOX, data={"password": "wrong"})
    r = c.post("/admin/login", environ_base=BOX, data={"password": "wrong"})
    check("locks out after repeated failures", r.status_code, 429)
    check("lockout says so", "locked" in r.get_data(as_text=True).lower(), True)

    # a new IP must NOT reset the lockout — that was the old bug
    r = c.post("/admin/login", environ_base={"REMOTE_ADDR": "10.0.0.77"},
               data={"password": "wrong"})
    check("changing IP does not reset the lockout", r.status_code, 429)

    # the correct password is refused while locked out
    r = c.post("/admin/login", environ_base=BOX, data={"password": REAL_PW})
    check("correct password refused during lockout", r.status_code, 429)

    # clearing the counter lets the real password through again
    main._LOGIN_FAILS.clear()
    r = c.post("/admin/login", environ_base=BOX, data={"password": REAL_PW})
    check("correct password works once unlocked", r.status_code, 302)
finally:
    for d in main.db.list_devices():
        main.db.delete_device(d["mac"])
    for d in _saved_devices:                       # put the dev data back
        main.db.set_device(d["mac"], d["kind"], d.get("label"), d.get("speed"))
    main._LOGIN_FAILS.clear()
    main.db.set_setting("admin_lan_access", "whitelist")

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
