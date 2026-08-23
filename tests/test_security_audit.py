"""Adversarial audit: what can a customer do that they should not?

Threat model. The attacker is someone standing in the shop with a phone or a
laptop on the customer WiFi. They may or may not have paid. They want to:

  1. reach the owner's own network, or the ISP router behind the machine
  2. attack another customer
  3. take over the machine or its admin panel
  4. get online without paying
  5. deny service to everyone else

Everything here is asserted from the customer's side of the wire.

    python tests/test_security_audit.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

DEV_DB = os.path.join(APP, "dev.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DEV_DB + suffix)
    except OSError:
        pass

import main            # noqa: E402
import test_reachability as R   # noqa: E402

if not main.MOCK:
    print("refusing to run against real hardware -- dev PC only")
    sys.exit(1)

fails, warns, section = [], [], ""


def head(t):
    global section
    section = t
    print("\n=== %s ===" % t)


def ok(label, cond, detail=""):
    print("  %-58s %s%s" % (label, "ok" if cond else "FAIL",
                            ("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append("[%s] %s %s" % (section, label, detail))


def warn(label, detail):
    print("  %-58s WARN  %s" % (label, detail))
    warns.append("[%s] %s -- %s" % (section, label, detail))


RULES = R.render()


def forwards_to(dst, mac_in, proto="tcp", dport=80):
    p = R.Packet(iif=R.LAN, oif=R.WAN, saddr="10.0.0.50", daddr=dst,
                 proto=proto, dport=dport, mac_in=mac_in, ctstate="new")
    act, rule = R.verdict(R.chain(RULES, "forward"), p)
    return act in ("accept", "policy accept"), rule


# ===========================================================================
head("1. Can a customer reach the owner's own network?")
# ===========================================================================
# The documented topology plugs the uplink into the owner's home router, so
# everything on that LAN sits one hop behind the machine.
PRIVATE = [
    ("ISP router admin page",        "192.168.254.254"),
    ("another host on the home LAN", "192.168.254.50"),
    ("a 10.x corporate/home host",   "10.20.30.40"),
    ("a 172.16.x host",              "172.16.5.9"),
    ("link-local / cloud metadata",  "169.254.169.254"),
]
for label, dst in PRIVATE:
    reach, rule = forwards_to(dst, {"allowed"})
    ok("paid customer CANNOT reach %s" % label, not reach,
       "%s reachable via: %s" % (dst, (rule or "").strip()))

reach, _ = forwards_to("1.1.1.1", {"allowed"})
ok("paid customer CAN still reach the public internet", reach)
reach, _ = forwards_to("192.168.254.254", set())
ok("unpaid customer cannot reach the home LAN either", not reach)

# The block must not lock out the owner or remove the deliberate escape hatch.
reach, _ = forwards_to("192.168.254.50", {"whitelist"})
ok("the owner's WHITELISTED devices keep full LAN access", reach,
   "whitelist should bypass the private-network block")

p = R.Packet(iif=R.LAN, oif=R.WAN, saddr="10.0.0.50", daddr="192.168.254.77",
             proto="tcp", dport=80, mac_in={"allowed"}, ctstate="new", walled=True)
act, _ = R.verdict(R.chain(RULES, "forward"), p)
ok("a host added to the walled garden is still reachable",
   act in ("accept", "policy accept"),
   "walled garden must override the block, or the owner has no way to allow "
   "a local server on purpose")


# ===========================================================================
head("2. Can a customer attack the machine itself?")
# ===========================================================================
def to_box(dport, mac_in, proto="tcp"):
    p = R.Packet(iif=R.LAN, saddr="10.0.0.50", daddr=R.GW, proto=proto,
                 dport=dport, mac_in=mac_in, ctstate="new")
    act, _ = R.verdict(R.chain(RULES, "input"), p)
    return act

for state, sets in (("unpaid", set()), ("paid", {"allowed"})):
    ok("%s customer cannot reach SSH on the box" % state,
       to_box(22, sets) not in ("accept", "policy accept"), str(to_box(22, sets)))
    for port in (445, 3389, 5355, 631, 9090):
        ok("%s customer cannot reach tcp/%d on the box" % (state, port),
           to_box(port, sets) not in ("accept", "policy accept"))

ok("customers can reach DNS (captive detection needs it)",
   to_box(53, set(), "udp") == "accept")
ok("customers can reach the portal", to_box(8080, set()) == "accept")

# Source-address spoofing from the LAN: only the client subnet may talk to us.
p = R.Packet(iif=R.LAN, saddr="8.8.8.8", daddr=R.GW, proto="tcp",
             dport=8080, mac_in=set(), ctstate="new")
act, _ = R.verdict(R.chain(RULES, "input"), p)
ok("a spoofed off-subnet source is dropped", act == "drop", str(act))


# ===========================================================================
head("3. Can a customer get online without paying?")
# ===========================================================================
reach, _ = forwards_to("1.1.1.1", set())
ok("unpaid customer cannot browse", not reach)
reach, _ = forwards_to("1.1.1.1", set(), proto="udp", dport=443)
ok("unpaid customer cannot tunnel over UDP/443 (QUIC)", not reach)
reach, _ = forwards_to("1.1.1.1", set(), proto="icmp", dport=0)
ok("unpaid customer cannot tunnel over ICMP", not reach)
reach, _ = forwards_to("8.8.8.8", set(), proto="udp", dport=53)
ok("unpaid customer cannot use an EXTERNAL resolver (DNS tunnelling)", not reach)

# The box's own resolver is reachable by design -- captive detection needs it.
# That is a data channel, so say so out loud rather than call it safe.
if to_box(53, set(), "udp") == "accept":
    warn("DNS to the box is open to unpaid clients",
         "required for captive-portal detection, but it is a low-bandwidth "
         "data channel a determined user can tunnel over; dnsmasq forwards it "
         "upstream. Acceptable, not zero-risk")

ok("outbound SMTP is blocked so customer malware cannot spam",
   not forwards_to("1.1.1.1", {"allowed"}, dport=25)[0]
   and not forwards_to("1.1.1.1", {"allowed"}, dport=587)[0])


# ===========================================================================
head("4. Can a customer attack another customer?")
# ===========================================================================
# Two clients on 10.0.0.0/24 are on one switched segment: their traffic never
# reaches the router, so nftables cannot see it, let alone block it. This is
# the access point's job and nothing in this repo can assert it.
warn("customer-to-customer traffic is not filtered by this machine",
     "same subnet, so it is switched not routed -- isolation depends entirely "
     "on the AP. hostapd config sets ap_isolate=1, but the wired topology uses "
     "an external AP whose isolation setting must be checked by hand")
ok("hostapd config enables client isolation (own-radio topology)",
   "ap_isolate=1" in open(os.path.join(HERE, os.pardir, "network", "hostapd.conf"),
                          encoding="utf-8").read())

# Time is keyed on MAC, which a customer can change at will.
warn("paid time is keyed on MAC address",
     "a customer who copies a paying customer's MAC inherits their time. "
     "Inherent to a captive portal with no login; the mitigation is that both "
     "devices then fight over one IP and neither works well")


# ===========================================================================
head("5. Application: injection, traversal, and stored XSS")
# ===========================================================================
c = main.app.test_client()
a = main.app.test_client()

# Log in and complete first-run setup so admin pages are reachable.
a.post("/admin/login", data={"password": main.setting("admin_password")})
page = a.get("/admin/setup").get_data(as_text=True)
import re as _re  # noqa: E402
m = _re.search(r'name="csrf"\s+value="([^"]+)"', page)
a.post("/admin/setup", data={"password": "seCurity!99", "confirm": "seCurity!99",
                             "csrf": m.group(1) if m else ""})
page = a.get("/admin").get_data(as_text=True)
m = _re.search(r"i\.value = \"([0-9a-f]{16,})\"", page) or \
    _re.search(r"i\.value = '([0-9a-f]{16,})'", page)
tok = m.group(1) if m else ""
ok("obtained an admin CSRF token", bool(tok))

XSS = "<script>alert(1)</script>"
SQLI = "'; DROP TABLE sales;--"

# Stored XSS through fields an attacker or a careless owner can set.
a.post("/admin/devices/add", data={"csrf": tok, "mac": "de:ad:be:ef:00:01",
                                   "label": XSS, "kind": "white", "speed": ""})
body = a.get("/admin/devices").get_data(as_text=True)
ok("device label is escaped, not executed",
   XSS not in body and "&lt;script&gt;" in body)

a.post("/admin/settings", data={"csrf": tok, "hotspot_name": XSS,
                                "minutes_per_peso": "20",
                                "bonus_tiers": json.dumps({"5": 120})})
ok("hotspot name is escaped in the admin page",
   XSS not in a.get("/admin").get_data(as_text=True))
ok("hotspot name is escaped on the customer portal",
   XSS not in c.get("/").get_data(as_text=True))
a.post("/admin/settings", data={"csrf": tok, "hotspot_name": "PisoWiFi",
                                "minutes_per_peso": "20",
                                "bonus_tiers": json.dumps({"5": 120})})

a.post("/admin/pppoe/save", data={"csrf": tok, "user": "x1", "password": "p",
                                  "plan_days": "30", "speed": "", "note": XSS})
ok("PPPoE note is escaped", XSS not in a.get("/admin/pppoe").get_data(as_text=True))

# SQL injection through the customer-facing voucher endpoint.
c.post("/api/redeem", json={"code": SQLI})
try:
    main.db.sales_summary()
    ok("SQL injection through /api/redeem did not damage the database", True)
except Exception as e:                                            # noqa: BLE001
    ok("SQL injection through /api/redeem did not damage the database", False, str(e))

# Path traversal through the branding file server.
for probe in ("../config.json", "..%2f..%2fetc%2fpasswd", "....//config.json"):
    r = c.get("/branding/" + probe)
    ok("branding path traversal refused: %s" % probe, r.status_code in (400, 403, 404),
       "status %s" % r.status_code)


# ===========================================================================
head("6. Admin surface: authentication and CSRF on every route")
# ===========================================================================
anon = main.app.test_client()
GETS = ["/admin", "/admin/vouchers", "/admin/devices", "/admin/branding",
        "/admin/schedules", "/admin/pppoe", "/admin/audit", "/admin/diag",
        "/admin/sales.csv", "/admin/vouchers.csv", "/admin/backup",
        "/admin/api/diag"]
for path in GETS:
    r = anon.get(path)
    ok("anonymous GET %s is refused" % path,
       r.status_code in (302, 401, 403) or (r.is_json and r.get_json().get("error")),
       "status %s" % r.status_code)

POSTS = ["/admin/settings", "/admin/remote", "/admin/kick", "/admin/simulate-coin",
         "/admin/vouchers/generate", "/admin/vouchers/revoke", "/admin/devices/add",
         "/admin/devices/remove", "/admin/branding", "/admin/schedules/add",
         "/admin/schedules/remove", "/admin/dns", "/admin/pppoe/save",
         "/admin/pppoe/toggle", "/admin/pppoe/remove", "/admin/hardware",
         "/admin/restore"]
for path in POSTS:
    r = anon.post(path, data={})
    ok("anonymous POST %s is refused" % path,
       r.status_code in (302, 400, 401, 403), "status %s" % r.status_code)

# Logged in, but no CSRF token: must still be refused.
for path in ("/admin/settings", "/admin/devices/add", "/admin/hardware",
             "/admin/kick", "/admin/restore"):
    r = a.post(path, data={"mac": "aa:bb:cc:dd:ee:ff"})
    ok("POST %s without CSRF is refused" % path, r.status_code in (400, 403),
       "status %s" % r.status_code)

# The diagnostics JSON API can drive GPIO -- it must be gated too.
for path in ("/admin/api/diag/pin", "/admin/api/diag/relay",
             "/admin/api/diag/scan", "/admin/api/diag/pulse"):
    r = anon.post(path, json={})
    body = r.get_json(silent=True) or {}
    ok("anonymous POST %s is refused" % path,
       r.status_code in (302, 401, 403) or bool(body.get("error")),
       "status %s body %s" % (r.status_code, body))


# ===========================================================================
head("7. Secrets and session handling")
# ===========================================================================
cfg = main.db.export_config(include_sales=True, include_secrets=False)
blob = json.dumps(cfg)
ok("backup export omits the Flask session key", "SECRET_KEY" not in blob)
ok("backup export omits the admin password hash", "admin_pw_hash" not in blob)
ok("backup export omits the remote dashboard key",
   "remote_key" not in blob or not cfg.get("settings", {}).get("remote_key"))

ok("admin password is stored hashed, never in clear",
   main.db.get_setting("admin_pw_hash") and
   "seCurity!99" not in json.dumps(main.db.get_setting("admin_pw_hash")))
ok("session cookie is HttpOnly", main.app.config["SESSION_COOKIE_HTTPONLY"] is True)
ok("session cookie is SameSite=Lax or stricter",
   main.app.config["SESSION_COOKIE_SAMESITE"] in ("Lax", "Strict"),
   str(main.app.config["SESSION_COOKIE_SAMESITE"]))
ok("admin sessions idle out", main.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() <= 3600,
   str(main.app.config["PERMANENT_SESSION_LIFETIME"]))

r = c.get("/nonexistent-page-xyz")
ok("errors do not leak a stack trace", b"Traceback" not in r.data)
ok("debug mode is off", main.app.debug is False)


# ===========================================================================
head("8. Denial of service by one customer")
# ===========================================================================
codes = [c.post("/api/insert").status_code for _ in range(30)]
ok("coin-window spam is rate limited", 429 in codes,
   "no 429 in %d requests" % len(codes))
codes = [c.post("/api/redeem", json={"code": "AAAA%02d" % i}).status_code
         for i in range(15)]
ok("voucher brute force is rate limited", 429 in codes, str(sorted(set(codes))))

main.db.set_setting("trial_enabled", True)
codes = [c.post("/api/trial").status_code for _ in range(10)]
ok("trial spam is rate limited or refused",
   429 in codes, str(sorted(set(codes))))
main.db.set_setting("trial_enabled", False)

ok("per-client connection cap is present in the ruleset",
   "ct count over" in RULES)
ok("SYN-flood limits are present on both input and forward",
   RULES.count("limit rate over") >= 2)
ok("realtime streams are capped", int(main.setting("sse_max_clients")) <= 8,
   str(main.setting("sse_max_clients")))

# A customer must not be able to fill the disk with sessions or audit rows.
ok("audit log is pruned", hasattr(main.db, "prune_audit"))


# ===========================================================================
print("\n" + "=" * 76)
if warns:
    print("%d ACCEPTED RISK(S) / NOTE(S):" % len(warns))
    for w in warns:
        print("  ~", w)
if fails:
    print("\n%d SECURITY FAILURE(S):" % len(fails))
    for f in fails:
        print("  *", f)
    sys.exit(1)
print("\nno security failures")
sys.exit(0)
