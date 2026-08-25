"""Whole-product functional audit — every customer and admin feature.

Runs against the app in mock mode (no GPIO, no nft), driving it through the
Flask test client exactly as a browser would. The emphasis is on **state
transitions**, because that is where this project's bugs live: things work in
the first state and silently stop working in the second. Paying is a state
change. Expiring is a state change. Pausing is a state change.

    python tests/test_functional.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "app")
sys.path.insert(0, APP)

# Start from a clean development database so results are reproducible.
DEV_DB = os.path.join(APP, "dev.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DEV_DB + suffix)
    except OSError:
        pass

import main  # noqa: E402

if not main.MOCK:
    print("refusing to run against real hardware -- dev PC only")
    sys.exit(1)

fails, warns, section = [], [], ""


def head(t):
    global section
    section = t
    print("\n=== %s ===" % t)


def ok(label, cond, detail=""):
    print("  %-56s %s%s" % (label, "ok" if cond else "FAIL",
                            ("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append("[%s] %s %s" % (section, label, detail))


def warn(label, detail):
    print("  %-56s WARN  %s" % (label, detail))
    warns.append("[%s] %s -- %s" % (section, label, detail))


c = main.app.test_client()
MAC = "aa:bb:cc:dd:ee:01"          # what client_mac() returns in mock mode
db = main.db


def status():
    return c.get("/api/status").get_json()


def insert_coins(*pesos):
    """Open the window, drop coins in, close it -- the real customer path."""
    c.post("/api/insert")
    for p in pesos:
        main.slot.inject(p) if hasattr(main.slot, "inject") else None
    return None


# ===========================================================================
head("Customer -- unpaid arrival")
# ===========================================================================
r = c.get("/")
ok("portal renders", r.status_code == 200)
ok("portal shows the hotspot name", b"PisoWiFi" in r.data or b"piso" in r.data.lower())
ok("captive probe redirects to portal", c.get("/generate_204").status_code == 302)
ok("unknown path redirects to portal", c.get("/some/random/path").status_code == 302)

cap = c.get("/api/capport").get_json()
ok("capport reports captive while unpaid", cap.get("captive") is True, str(cap))
ok("capport advertises a venue/user URL",
   any(k in cap for k in ("user-portal-url", "venue-info-url")), str(cap))

s = status()
ok("status: no time yet", s["remaining"] == 0, str(s))
ok("status: not paused", s["paused"] is False)


# ===========================================================================
head("Customer -- inserting coins")
# ===========================================================================
r = c.post("/api/insert")
ok("insert opens the coin window", r.status_code == 200, r.get_data(as_text=True))
s = status()
ok("window shows as open", s["window"]["open"] is True, str(s["window"]))

# Simulate the acceptor: pulses -> pesos -> credited when the window closes.
main.slot._window_pesos = 5
r = c.post("/api/done")
j = r.get_json()
ok("done credits the inserted pesos", j.get("pesos") == 5, str(j))

s = status()
expected = main.minutes_for(5) * 60
ok("time granted matches the rate table (P5)",
   expected - 5 <= s["remaining"] <= expected, "got %ss, want ~%ss" % (s["remaining"], expected))
ok("capport no longer captive once paid", c.get("/api/capport").get_json()["captive"] is False)

sales = db.sales_summary()
ok("sale recorded", sales["all_time"]["count"] == 1, str(sales["all_time"]))
ok("sale amount recorded", sales["all_time"]["pesos"] == 5, str(sales["all_time"]))


# ===========================================================================
head("Customer -- topping up while still active (time must STACK)")
# ===========================================================================
before = status()["remaining"]
c.post("/api/insert")
main.slot._window_pesos = 1
c.post("/api/done")
after = status()["remaining"]
gain = after - before
want = main.minutes_for(1) * 60
ok("second coin stacks on the remaining time",
   want - 5 <= gain <= want + 1, "gained %ss, want ~%ss" % (gain, want))


# ===========================================================================
head("Customer -- EXPIRY (the transition that broke before)")
# ===========================================================================
# Time-travel: push the session's expiry into the past, as the clock would.
sess = db.get_session(MAC)
db.set_session(MAC, time.time() - 1, None)

s = status()
ok("status reports zero after expiry", s["remaining"] == 0, str(s))
ok("capport reports captive again after expiry",
   c.get("/api/capport").get_json()["captive"] is True)
ok("portal still renders after expiry", c.get("/").status_code == 200)
ok("expired device is no longer an active session",
   all(x["mac"] != MAC for x in db.active_sessions()),
   str([dict(x) for x in db.active_sessions()]))

# The reported-bug shape: can the customer buy again after running out?
r = c.post("/api/insert")
ok("can re-open the coin window after expiry", r.status_code == 200,
   r.get_data(as_text=True))
main.slot._window_pesos = 5
c.post("/api/done")
s = status()
ok("re-purchase after expiry grants time again",
   s["remaining"] > 0, str(s))
ok("re-purchase does not inherit the stale expiry",
   s["remaining"] >= main.minutes_for(5) * 60 - 5, str(s))


# ===========================================================================
head("Customer -- pause and resume")
# ===========================================================================
before = status()["remaining"]
r = c.post("/api/pause")
ok("pause accepted while time is active", r.status_code == 200, r.get_data(as_text=True))
s = status()
ok("paused flag set", s["paused"] is True)
ok("paused time is preserved", abs(s["remaining"] - before) <= 2,
   "was %s now %s" % (before, s["remaining"]))

time.sleep(1.2)
ok("paused time does not tick down", abs(status()["remaining"] - before) <= 2)

r = c.post("/api/pause")
ok("double-pause is rejected", r.status_code == 400, str(r.status_code))

r = c.post("/api/resume")
ok("resume accepted", r.status_code == 200)
ok("resume clears the paused flag", status()["paused"] is False)
r = c.post("/api/resume")
ok("double-resume is rejected", r.status_code == 400, str(r.status_code))

# Buying while paused should resume and stack, not discard the banked time.
c.post("/api/pause")
banked = status()["remaining"]
c.post("/api/insert")
main.slot._window_pesos = 1
c.post("/api/done")
s = status()
ok("buying while paused resumes and stacks",
   s["paused"] is False and s["remaining"] >= banked, "banked %s now %s" % (banked, s["remaining"]))


# ===========================================================================
head("Customer -- money safety")
# ===========================================================================
# apply_credit() is the software path and behaves correctly...
before = status()["remaining"]
main.apply_credit(MAC, 5)
after = status()["remaining"]
ok("apply_credit() with no window still grants time", after > before,
   "before %s after %s" % (before, after))

# ...but the REAL path is a physical pulse train, and that is a different
# story. These two are the audit's most serious findings: in both cases the
# customer's coin is gone and only an optional relay stands in the way.
db.set_setting("pulse_end_gap_s", 0.2)
main.slot.reconfigure(main.hwcfg())
gap = float(main.setting("pulse_end_gap_s")) + 0.4

if main.slot._window_mac:
    main.slot.close_window(main.slot._window_mac)
main.slot.pending_pesos = 0

main.slot.simulate(5)                       # a real coin, no window open
time.sleep(gap)
ok("a coin with no window open is HELD, not destroyed",
   main.slot.pending_pesos == 5,
   "pending=P%d, last_train=%s" % (main.slot.pending_pesos, main.slot.last_train))
ok("held coins are advertised to the portal",
   main.slot.status("someone")["pending"] == 5,
   str(main.slot.status("someone")))

main.slot.open_window(MAC)                  # next customer taps INSERT COIN
ok("the next customer to open a window claims the held coins",
   main.slot._window_pesos == 5, "window holds P%d" % main.slot._window_pesos)
ok("the pot is emptied once claimed", main.slot.pending_pesos == 0)
main.slot.close_window(MAC)

# Held coins must not be handed out forever -- a coin from hours ago belongs
# to the owner to settle, not to whoever happens to walk up next.
db.set_setting("uncredited_hold_s", 1)
main.slot.reconfigure(main.hwcfg())
main.slot.simulate(5)
time.sleep(gap + 1.2)                       # let the hold window lapse
main.slot.open_window(MAC)
ok("coins older than uncredited_hold_s are NOT auto-given away",
   main.slot._window_pesos == 0, "window picked up P%d" % main.slot._window_pesos)
ok("but they are still on the books, not destroyed",
   main.slot.pending_pesos == 5, "pending=P%d" % main.slot.pending_pesos)
main.slot.close_window(MAC)
main.slot.pending_pesos = 0
db.set_setting("uncredited_hold_s", 300)
db.set_setting("pulse_end_gap_s", 0.7)
main.slot.reconfigure(main.hwcfg())

# This one cannot be fixed in software: the acceptor reports pulses, not who
# dropped them. The relay is the only real defence, so record it as a known
# limitation rather than pretending a test can close it.
main.slot.open_window("aa:aa:aa:aa:aa:aa")
main.slot.simulate(10)
time.sleep(gap)
stolen = main.slot.close_window("aa:aa:aa:aa:aa:aa")
if stolen:
    warn("coin inserted during another customer's window",
         "credited P%d to the customer holding the slot -- inherent to the "
         "hardware; fit the relay so the acceptor is dead between windows" % stolen)
ok("relay is configured to gate the acceptor",
   bool(main.setting("relay_gpio_pin")),
   "relay_gpio_pin=%r -- with no relay the acceptor takes coins at any time"
   % main.setting("relay_gpio_pin"))

# The owner has to be able to SEE money going astray, or the holding pot just
# hides the problem instead of the coins.
ok("unclaimed coins are tallied for the owner",
   main.slot.unclaimed_total >= 10 and main.slot.unclaimed_events >= 2,
   "total=P%d over %d events" % (main.slot.unclaimed_total, main.slot.unclaimed_events))
d = main.slot.diag()
ok("the tally is exposed to the diagnostics API",
   d.get("unclaimed_total", 0) > 0 and "pending_pesos" in d,
   str({k: d.get(k) for k in ("unclaimed_total", "pending_pesos", "relay_pin")}))

before = status()["remaining"]
main.apply_credit(MAC, 0)
ok("a zero-peso credit changes nothing", status()["remaining"] == before)

n_before = db.sales_summary()["all_time"]["count"]
main.apply_credit(MAC, -5)
ok("a negative credit is refused", db.sales_summary()["all_time"]["count"] == n_before)


# ===========================================================================
head("Customer -- vouchers")
# ===========================================================================
codes = ["AUDIT1", "AUDIT2"]
for cd in codes:
    db.create_voucher(cd, minutes=30, price=5, batch="audit")
ok("voucher batch generated", db.get_voucher("AUDIT1") is not None)
code = codes[0]

r = c.post("/api/redeem", json={"code": code.lower()})   # case-insensitive
ok("voucher redeems (case-insensitive)", r.status_code == 200, r.get_data(as_text=True))
r = c.post("/api/redeem", json={"code": code})
ok("the same voucher cannot be reused", r.status_code == 400, r.get_data(as_text=True))
r = c.post("/api/redeem", json={"code": "NOPE99"})
ok("an unknown code is refused", r.status_code == 400)
r = c.post("/api/redeem", json={"code": ""})
ok("an empty code is refused", r.status_code == 400)


# ===========================================================================
head("Customer -- free trial")
# ===========================================================================
r = c.post("/api/trial")
ok("trial refused while the feature is off", r.status_code == 400, r.get_data(as_text=True))

db.set_setting("trial_enabled", True)
db.set_setting("trial_minutes", 15)
db.set_setting("trial_period_hours", 24)
db.delete_session(MAC)

r = c.post("/api/trial")
ok("trial granted when enabled", r.status_code == 200, r.get_data(as_text=True))
r = c.post("/api/trial")
ok("second trial within the period is refused", r.status_code == 429, r.get_data(as_text=True))
db.set_setting("trial_enabled", False)


# ===========================================================================
head("Customer -- wallet (saved credit)")
# ===========================================================================
r = c.post("/api/wallet/save")
ok("wallet refused while the feature is off", r.status_code == 400)

db.set_setting("wallet_enabled", True)
r = c.post("/api/wallet/use")
ok("using an empty wallet is refused", r.status_code == 400, r.get_data(as_text=True))

# Bank the remaining time, then spend it back.
have = status()["remaining"]
r = c.post("/api/wallet/save")
if r.status_code == 200:
    s = status()
    ok("saving banks the remaining time", s["remaining"] == 0 and s["wallet"] > 0, str(s))
    r = c.post("/api/wallet/use")
    ok("saved credit can be used again", r.status_code == 200, r.get_data(as_text=True))
    ok("using restores the time", status()["remaining"] > 0, str(status()))
else:
    warn("wallet save", "returned %s: %s" % (r.status_code, r.get_data(as_text=True)))
db.set_setting("wallet_enabled", False)


# ===========================================================================
head("Admin -- authentication")
# ===========================================================================
a = main.app.test_client()
ok("admin bounces to login when signed out", a.get("/admin").headers.get("Location", "").endswith("/admin/login"))

r = a.post("/admin/login", data={"password": "wrong-password"})
ok("wrong password rejected", b"Wrong password" in r.data, r.get_data(as_text=True)[:120])

r = a.post("/admin/login", data={"password": main.setting("admin_password")})
ok("correct password accepted", r.status_code == 302, str(r.status_code))
ok("first login is forced to the setup wizard",
   r.headers.get("Location", "").endswith("/admin/setup"),
   r.headers.get("Location", ""))

r = a.get("/admin")
ok("every admin page bounces to setup until the password is changed",
   r.headers.get("Location", "").endswith("/admin/setup"), r.headers.get("Location", ""))

# Complete the wizard.
csrf = None
page = a.get("/admin/setup").get_data(as_text=True)
import re as _re
m = _re.search(r'name="csrf"\s+value="([^"]+)"', page)
csrf = m.group(1) if m else None
ok("setup page carries a CSRF token", bool(csrf))

r = a.post("/admin/setup", data={"password": "short", "confirm": "short", "csrf": csrf})
ok("short password rejected", b"at least 8" in r.data, r.get_data(as_text=True)[:150])
r = a.post("/admin/setup", data={"password": "abcd1234", "confirm": "different", "csrf": csrf})
ok("mismatched confirmation rejected", b"don't match" in r.data or b"match" in r.data)
r = a.post("/admin/setup", data={"password": "changeme", "confirm": "changeme", "csrf": csrf})
ok("the shipped default is refused as the new password",
   b"shipped default" in r.data, r.get_data(as_text=True)[:150])

r = a.post("/admin/setup", data={"password": "auditpass1", "confirm": "auditpass1",
                                 "whitelist_me": "on", "csrf": csrf})
ok("a real password is accepted", r.status_code == 302, str(r.status_code))
ok("setup redirects into the dashboard", r.headers.get("Location", "").endswith("/admin"))
ok("the setup device was whitelisted", db.is_whitelisted(MAC))
ok("dashboard now opens directly", a.get("/admin").status_code == 200)


# ===========================================================================
head("Admin -- CSRF is actually enforced")
# ===========================================================================
r = a.post("/admin/settings", data={"minutes_per_peso": "99"})
ok("POST without a CSRF token is rejected", r.status_code in (400, 403), str(r.status_code))
ok("the rejected POST did not change anything", main.setting("minutes_per_peso") != 99,
   str(main.setting("minutes_per_peso")))


def admin_csrf(path="/admin"):
    page = a.get(path).get_data(as_text=True)
    m = _re.search(r"i\.value = (?:'|\")([0-9a-f]{16,})(?:'|\")", page) or \
        _re.search(r'name="csrf"\s+value="([^"]+)"', page) or \
        _re.search(r'i\.value = "([^"]+)"', page)
    return m.group(1) if m else None


tok = admin_csrf()
ok("dashboard exposes a CSRF token for its forms", bool(tok), "none found")


# ===========================================================================
head("Admin -- every page renders")
# ===========================================================================
for path in ("/admin", "/admin/vouchers", "/admin/devices", "/admin/branding",
             "/admin/schedules", "/admin/pppoe", "/admin/audit", "/admin/diag"):
    r = a.get(path)
    ok("GET %s" % path, r.status_code == 200, "status %s" % r.status_code)

for path in ("/admin/sales.csv", "/admin/vouchers.csv", "/admin/backup"):
    r = a.get(path)
    ok("GET %s" % path, r.status_code == 200, "status %s" % r.status_code)

r = a.get("/admin/api/diag")
ok("diagnostics API responds", r.status_code == 200 and "pins" in r.get_json())


# ===========================================================================
head("Admin -- settings round-trip")
# ===========================================================================
r = a.post("/admin/settings", data={
    "csrf": tok, "hotspot_name": "Audited WiFi", "minutes_per_peso": "25",
    "bonus_tiers": json.dumps({"5": 150, "10": 320}), "admin_password": ""})
ok("settings POST accepted", r.status_code == 302, str(r.status_code))
ok("hotspot name saved", main.setting("hotspot_name") == "Audited WiFi",
   main.setting("hotspot_name"))
ok("fallback rate saved", main.setting("minutes_per_peso") == 25,
   str(main.setting("minutes_per_peso")))
ok("bonus tiers saved", main.setting("bonus_tiers") == {"5": 150, "10": 320},
   str(main.setting("bonus_tiers")))
ok("new rate is what a P5 coin now buys", main.minutes_for(5) == 150,
   str(main.minutes_for(5)))
# Between tiers, the better per-peso rate carries: P5=150 is 30/peso, so P7
# earns 210, not the base 25/peso = 175.
ok("an amount between tiers earns the better tier rate",
   main.minutes_for(7) == 210, "P7 = %s (want 210)" % main.minutes_for(7))
ok("an amount below every tier uses the base rate",
   main.minutes_for(3) == 75, str(main.minutes_for(3)))

# The invariant that matters commercially: more money must never buy less time.
# Proven against a deliberately hostile table, not just a sane one.
db.set_setting("minutes_per_peso", 12)
db.set_setting("bonus_tiers", {"5": 120, "9": 100, "13": 500})   # tier 9 is a mistake
ladder = [main.minutes_for(p) for p in range(1, 41)]
ok("more money NEVER buys less time, even with a mis-typed tier",
   all(b >= a for a, b in zip(ladder, ladder[1:])),
   "regressions at P%s" % [i + 1 for i, (a, b) in enumerate(zip(ladder, ladder[1:])) if b < a])

# Match the commercial machine exactly on its own published table.
db.set_setting("minutes_per_peso", 20)
db.set_setting("bonus_tiers", {"1": 20, "5": 120, "10": 240,
                               "15": 360, "20": 480, "25": 1440})
VEND = {1: 20, 5: 24, 10: 24, 15: 24, 20: 24, 25: 57.6}
diffs = [(p, main.minutes_for(p), int(p * VEND[max(k for k in VEND if k <= p)]))
         for p in range(1, 31)]
ok("reproduces the WiFi5-Soft rate table exactly, P1-P30",
   all(mine == vend for _, mine, vend in diffs),
   str([d for d in diffs if d[1] != d[2]][:4]))
ok("the P25 day pass is exactly 24 hours", main.minutes_for(25) == 1440,
   str(main.minutes_for(25)))
ok("blank password field leaves the password alone",
   db.verify_admin("auditpass1"))


# ===========================================================================
head("Admin -- devices, vouchers, schedules, PPPoE, branding")
# ===========================================================================
r = a.post("/admin/devices/add", data={"csrf": tok, "mac": "11:22:33:44:55:66",
                                       "label": "CCTV", "kind": "white", "speed": ""})
ok("whitelist add accepted", r.status_code == 302)
ok("device is whitelisted", db.is_whitelisted("11:22:33:44:55:66"))
r = a.post("/admin/devices/add", data={"csrf": tok, "mac": "not-a-mac",
                                       "label": "", "kind": "white", "speed": ""})
ok("an invalid MAC is rejected", r.status_code in (200, 302, 400))
ok("the invalid MAC was not stored",
   not any(d["mac"] == "not-a-mac" for d in db.list_devices()))
r = a.post("/admin/devices/remove", data={"csrf": tok, "mac": "11:22:33:44:55:66"})
ok("device removal accepted", r.status_code == 302)
ok("device actually removed", not db.is_whitelisted("11:22:33:44:55:66"))

r = a.post("/admin/vouchers/generate", data={"csrf": tok, "count": "3", "minutes": "45",
                                             "price": "5", "speed": "", "batch": "b2",
                                             "expires_days": "0"})
ok("voucher generation accepted", r.status_code == 302)
ok("vouchers exist", db.voucher_stats()["total"] >= 5, str(db.voucher_stats()))

r = a.post("/admin/schedules/add", data={"csrf": tok, "type": "reboot",
                                         "time": "03:00", "value": "", "days": []})
ok("schedule add accepted", r.status_code == 302)
ok("schedule stored", len(main.setting("schedules")) == 1, str(main.setting("schedules")))
r = a.post("/admin/schedules/add", data={"csrf": tok, "type": "reboot",
                                         "time": "notatime", "value": "", "days": []})
ok("an invalid schedule time is rejected",
   len(main.setting("schedules")) == 1, str(main.setting("schedules")))
r = a.post("/admin/schedules/remove", data={"csrf": tok, "idx": "0"})
ok("schedule removal accepted", r.status_code == 302)
ok("schedule removed", len(main.setting("schedules")) == 0)

r = a.post("/admin/pppoe/save", data={"csrf": tok, "user": "juan", "password": "secret1",
                                      "plan_days": "30", "speed": "", "note": ""})
ok("PPPoE subscriber saved", r.status_code == 302)
ok("subscriber stored", any(x["user"] == "juan" for x in db.list_pppoe_accounts()))
r = a.post("/admin/pppoe/toggle", data={"csrf": tok, "user": "juan", "enable": "0"})
ok("subscriber can be disabled", r.status_code == 302)
r = a.post("/admin/pppoe/remove", data={"csrf": tok, "user": "juan"})
ok("subscriber removed", not any(x["user"] == "juan" for x in db.list_pppoe_accounts()))

# One customer must not be able to take the whole shared line.
r = a.post("/admin/speed", data={"csrf": tok, "speed_enabled": "on",
                                 "speed_down": "6", "speed_up": "4"})
ok("speed limit accepted", r.status_code == 302, str(r.status_code))
ok("cap stored as a real profile",
   main.setting("speed_profiles").get("default") == {"up": "4mbit", "down": "6mbit"},
   str(main.setting("speed_profiles").get("default")))
ok("cap is what new customers get", main.setting("default_speed") == "default",
   str(main.setting("default_speed")))
r = a.post("/admin/speed", data={"csrf": tok})          # unticked = no limit
ok("speed limit can be turned off", main.setting("default_speed") == "unlimited",
   str(main.setting("default_speed")))
r = a.post("/admin/speed", data={"csrf": tok, "speed_enabled": "on",
                                 "speed_down": "0", "speed_up": "-5"})
ok("a nonsense cap is clamped, not stored",
   main.setting("speed_profiles")["default"] == {"up": "1mbit", "down": "1mbit"},
   str(main.setting("speed_profiles")["default"]))

r = a.post("/admin/branding", data={"csrf": tok, "color": "#ff0000",
                                    "show_redeem": "on", "show_trial": "on"})
ok("branding saved", r.status_code == 302)
ok("accent colour stored", main.setting("branding").get("color") == "#ff0000",
   str(main.setting("branding")))
ok("an unticked box turns the button off",
   main.setting("branding").get("show_pause") in (False, None),
   str(main.setting("branding")))


# ===========================================================================
head("Admin -- hardware configuration")
# ===========================================================================
hw = dict(main.hwcfg())
r = a.post("/admin/hardware", data={
    "csrf": tok, "coin_gpio_pin": "7", "coin_edge": "rising", "coin_bounce_ms": "45",
    "relay_gpio_pin": "13", "relay_active_low": "on", "pulse_end_gap_s": "0.8",
    "insert_window_s": "90", "pulse_value_pesos": "1",
    "denominations": json.dumps({"1": 1, "5": 5})})
ok("hardware POST accepted", r.status_code in (200, 302), str(r.status_code))
ok("coin pin saved", main.setting("coin_gpio_pin") == 7, str(main.setting("coin_gpio_pin")))
ok("edge saved", main.setting("coin_edge") == "rising")
ok("denominations saved", main.setting("denominations") == {"1": 1, "5": 5},
   str(main.setting("denominations")))
ok("the live coin slot picked up the new pin", main.slot.cfg["coin_gpio_pin"] == 7,
   str(main.slot.cfg["coin_gpio_pin"]))

r = a.post("/admin/hardware", data={
    "csrf": tok, "coin_gpio_pin": "12", "coin_edge": "falling", "coin_bounce_ms": "30",
    "relay_gpio_pin": "0", "pulse_end_gap_s": "0.7", "insert_window_s": "60",
    "pulse_value_pesos": "1", "denominations": json.dumps({"1": 1})})
ok("a non-interrupt coin pin is refused", main.setting("coin_gpio_pin") == 7,
   "pin became %s" % main.setting("coin_gpio_pin"))

# The hold policy has to be editable from the UI, not only from a config file
# on a box the owner cannot SSH into.
r = a.post("/admin/hardware", data={
    "csrf": tok, "coin_gpio_pin": "7", "coin_edge": "rising", "coin_bounce_ms": "45",
    "relay_gpio_pin": "13", "relay_active_low": "on", "pulse_end_gap_s": "0.8",
    "insert_window_s": "90", "pulse_value_pesos": "1", "uncredited_hold_s": "120",
    "denominations": json.dumps({"1": 1, "5": 5})})
ok("unclaimed-coin hold is editable from the hardware form",
   main.setting("uncredited_hold_s") == 120, str(main.setting("uncredited_hold_s")))
ok("the live slot picked up the new hold", main.slot.cfg["uncredited_hold_s"] == 120,
   str(main.slot.cfg.get("uncredited_hold_s")))

# The dashboard has to warn when there is no relay, since that is the only
# thing standing between a customer and a lost coin.
db.set_setting("relay_gpio_pin", 0)
page = a.get("/admin").get_data(as_text=True)
ok("dashboard warns when no relay is configured", "No relay is configured" in page)
db.set_setting("relay_gpio_pin", 11)
page = a.get("/admin").get_data(as_text=True)
ok("that warning disappears once a relay is set",
   "No relay is configured" not in page)
ok("dashboard reports coins that arrived with no slot open",
   "arrived with no slot open" in page, "banner missing")


# ===========================================================================
head("Admin -- operational actions")
# ===========================================================================
r = a.post("/admin/kick", data={"csrf": tok, "mac": MAC})
ok("kick accepted", r.status_code == 302)
ok("kicked device has no time left", status()["remaining"] == 0, str(status()))

n = db.sales_summary()["all_time"]["count"]
c.post("/api/insert")                       # a window must be open to be credited
r = a.post("/admin/simulate-coin", data={"csrf": tok, "pesos": "5"})
ok("simulate-coin accepted", r.status_code == 302)
time.sleep(float(main.setting("pulse_end_gap_s")) + 0.4)   # train completion is async
c.post("/api/done")
ok("simulated coin recorded a sale",
   db.sales_summary()["all_time"]["count"] == n + 1,
   "%d -> %d" % (n, db.sales_summary()["all_time"]["count"]))

entries = db.list_audit(50)
ok("admin actions are audited", len(entries) > 0, "%d entries" % len(entries))

r = a.get("/admin/logout")
ok("logout redirects", r.status_code == 302)
ok("admin is locked out after logout",
   a.get("/admin").headers.get("Location", "").endswith("/admin/login"))


# ===========================================================================
head("Concurrency -- 10 customers at once, then 50")
# ===========================================================================
# The board is a 4-core H3 with ~1 GB RAM serving waitress with 8 worker
# threads, and one shared sqlite Db object used by the web, reconcile,
# scheduler and watchdog threads. sqlite3 connections cannot cross threads, so
# db.py keeps a per-thread cache -- this is the thing most likely to break
# under real load, and it cannot be seen with one customer.
import threading  # noqa: E402

def customer_session(n, results, errors):
    """One customer's whole journey, on its own thread with its own MAC."""
    mac = "aa:bb:cc:dd:%02x:%02x" % (n // 256, n % 256)
    cl = main.app.test_client()
    try:
        # Each thread must look like a different device.
        main.client_mac = lambda _m=mac: _m
        cl.get("/")
        main._grant(mac, 30, source="coin", pesos=5)
        st = main._status_payload(mac)
        results.append((mac, st["remaining"]))
    except Exception as e:                                    # noqa: BLE001
        errors.append("%s: %s: %s" % (mac, type(e).__name__, e))

real_client_mac = main.client_mac
for count in (10, 50):
    results, errors = [], []
    threads = [threading.Thread(target=customer_session, args=(i, results, errors))
               for i in range(count)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - t0
    main.client_mac = real_client_mac

    ok("%2d concurrent customers: no thread raised" % count, not errors,
       "; ".join(errors[:3]))
    ok("%2d concurrent customers: all completed" % count, len(results) == count,
       "%d of %d" % (len(results), count))
    ok("%2d concurrent customers: every one got time" % count,
       all(r[1] > 0 for r in results), str([r for r in results if r[1] <= 0][:3]))
    ok("%2d concurrent customers: finished under 30s" % count, elapsed < 30,
       "%.1fs" % elapsed)
    print("       (%d threads in %.2fs)" % (count, elapsed))

# Every one of those sessions must be visible and distinct afterwards.
active = db.active_sessions()
# The 10-run and the 50-run share MACs 0..9, so 50 distinct devices is correct.
ok("all 50 distinct concurrent sessions persisted", len(active) >= 50,
   "%d active" % len(active))
ok("no duplicate MACs in the session table",
   len({a["mac"] for a in active}) == len(active),
   "%d rows, %d unique" % (len(active), len({a["mac"] for a in active})))
n_sales = db.sales_summary()["all_time"]["count"]
ok("every concurrent grant recorded a sale", n_sales >= 60, "%d sales" % n_sales)

# The coin slot is ONE physical device: only one customer may hold the window.
main.client_mac = lambda: "aa:bb:cc:dd:ee:01"
c.post("/api/done")                                    # make sure it starts closed
r1 = main.app.test_client().post("/api/insert")
main.client_mac = lambda: "aa:bb:cc:dd:ee:02"
r2 = main.app.test_client().post("/api/insert")
ok("first customer gets the coin slot", r1.status_code == 200)
ok("a second customer is told the slot is busy (not silently ignored)",
   r2.status_code == 409, "got %s: %s" % (r2.status_code, r2.get_data(as_text=True)[:80]))
main.client_mac = lambda: "aa:bb:cc:dd:ee:01"
c.post("/api/done")
main.client_mac = real_client_mac

# Concurrent reads of the dashboard while sessions churn.
errors = []
def hammer_status(errors):
    cl = main.app.test_client()
    for _ in range(20):
        try:
            if cl.get("/api/status").status_code != 200:
                errors.append("status != 200")
        except Exception as e:                                # noqa: BLE001
            errors.append("%s: %s" % (type(e).__name__, e))

threads = [threading.Thread(target=hammer_status, args=(errors,)) for _ in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
ok("200 concurrent status polls all succeeded", not errors, "; ".join(errors[:3]))


# ===========================================================================
head("Security -- login lockout")
# ===========================================================================
b = main.app.test_client()
codes = [b.post("/admin/login", data={"password": "bad%d" % i}).status_code
         for i in range(12)]
ok("repeated failures eventually lock the door", 429 in codes, str(codes))
r = b.post("/admin/login", data={"password": "auditpass1"})
ok("the correct password is also refused while locked",
   r.status_code == 429 or b"locked" in r.data, str(r.status_code))


# ===========================================================================
print("\n" + "=" * 74)
if warns:
    print("%d WARNING(S):" % len(warns))
    for w in warns:
        print("  ~", w)
if fails:
    print("%d FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("ALL FUNCTIONAL CHECKS PASSED")
sys.exit(0)
