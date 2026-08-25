"""Render every template, and check the JavaScript that comes out.

Rendering alone is not enough. The bug that froze the whole admin UI rendered
perfectly: `const TIERS = {{ tiers }}` produced valid HTML containing invalid
JavaScript, because autoescaping turned the quotes into numeric entities. The
script block died, and it happened to be the block that stamped CSRF tokens
into every form -- so every POST on the page failed silently.

So this checks three things:
  1. every template renders against representative context
  2. no rendered <script> contains HTML entities
  3. if node is available, each <script> actually parses as JavaScript

    python tests/test_templates.py
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, os.pardir, "app", "templates")

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

env = Environment(loader=FileSystemLoader(TPL),
                  autoescape=select_autoescape(default_for_string=True, default=True))
env.filters["ts"] = lambda t: "Aug 22 19:00"


class Req:
    """Only .path is used, by the nav's active-tab logic."""
    path = "/admin"


COMMON = dict(name="AJ PisoWiFi", csrf="deadbeefdeadbeefdeadbeef", request=Req())

SALES = {"today": {"pesos": 120, "count": 8}, "week": {"pesos": 640, "count": 41},
         "all_time": {"pesos": 1980, "count": 130},
         "recent": [{"when": "19:02", "mac": "aa:bb:cc:dd:ee:ff", "pesos": 5, "minutes": 120}]}

HW = {"coin_gpio_pin": 3, "coin_edge": "falling", "coin_bounce_ms": 30,
      "relay_gpio_pin": 11, "relay_active_low": True, "pulse_end_gap_s": 0.7,
      "insert_window_s": 60, "denominations": {"1": 1, "5": 5, "10": 10},
      "pulse_value_pesos": 1, "uncredited_hold_s": 300}

COIN = dict(pending=5, unclaimed_total=15, unclaimed_events=2, relay_pin=11, hold_s=300)

PAGES = {
    "admin.html": dict(
        sales=SALES, active=[{"mac": "aa:bb:cc:dd:ee:01", "paused": False, "remaining": 3720}],
        rate=20, tiers={"5": 120, "10": 260, "20": 560}, mock=False, coin=COIN,
        speed={"enabled": True, "down": 5, "up": 3, "shaped_now": 2,
               "unshaped": 1, "reasons": ["no IP yet (client not seen on the network)"]},
        remote={"remote_enabled": False, "remote_url": "", "remote_key": "",
                "remote_interval_s": 300, "device_id": ""},
        remote_last={"at": None, "ok": False, "detail": ""}, device_id_default="pisowifi-42af48",
        sys={"version": "2.4", "uptime_s": 7400, "cpu_temp_c": 45, "mem_free_mb": 700},
        daily=[("2026-08-%02d" % d, d * 3, d) for d in range(9, 23)],
        vouchers={"unused": 4, "used": 1, "total": 5},
        watch={"at": 1, "wan": True, "wan_fails": 0, "services": {"dnsmasq": True}}),
    "portal.html": dict(
        rates=[(1, 20), (5, 120)], brand={"color": "#fbbf24", "logo": "", "banner": "",
        "show_redeem": True, "show_trial": True, "show_pause": True},
        trial_enabled=True, trial_minutes=15, wallet_enabled=True, sse_enabled=False,
        unconfigured=True),
    "vouchers.html": dict(stats={"unused": 4, "used": 1, "total": 5}, profiles=["default"],
        vouchers=[{"code": "AB12CD", "minutes": 60, "price": 5, "speed": None,
                   "batch": "b1", "used_at": None, "used_by_mac": None}]),
    "devices.html": dict(profiles=["default"],
        whitelist=[{"mac": "aa:bb:cc:dd:ee:ff", "label": "phone", "speed": None}], blacklist=[]),
    "audit.html": dict(entries=[{"at": 1700000000, "ip": "10.0.0.5",
                                 "action": "admin login", "detail": ""}]),
    "branding.html": dict(b={"color": "#fbbf24", "logo": "", "banner": "",
                             "show_redeem": True, "show_trial": True, "show_pause": True}),
    "schedules.html": dict(schedules=[{"type": "reboot", "time": "03:00", "days": []}],
        dns_filter=False, error=None,
        blocklist={"running": False, "ok": None, "detail": "", "at": None}),
    "pppoe.html": dict(enabled=False, profiles=["default"],
        accounts=[{"user": "juan", "plan_days": 30, "expires_at": 1700000000,
                   "speed": None, "enabled": True}]),
    "diag.html": dict(mock=False, error=None, hw=HW, denominations=HW["denominations"],
                      eint_pins=[3, 5, 7, 8, 10, 11, 13, 15, 22, 26]),
    "admin_login.html": dict(error=None),
    "admin_setup.html": dict(error=None, mac="aa:bb:cc:dd:ee:01", on_lan=True),
}

# Only the entities that mean autoescaping mangled JavaScript *syntax*: quotes.
# Deliberate glyph entities such as &#10005; or &rarr; are fine and common --
# they sit inside strings destined for innerHTML, which decodes them. Flagging
# those made this check cry wolf on correct code, and a check that cries wolf
# gets ignored, which is how the original bug survived.
ENTITIES = re.compile(r"&(?:#0*34|#x0*22|quot|#0*39|#x0*27|apos);")


def main():
    node = shutil.which("node")
    fails = []
    print("  %-20s %8s %8s  %s" % ("template", "bytes", "scripts", "javascript"))
    for tpl in sorted(PAGES):
        try:
            out = env.get_template(tpl).render(**COMMON, **PAGES[tpl])
        except Exception as e:                                    # noqa: BLE001
            print("  %-20s  RENDER FAIL: %s: %s" % (tpl, type(e).__name__, e))
            fails.append("%s: %s" % (tpl, e))
            continue

        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", out, re.S)
        verdict = "ok"

        for i, s in enumerate(scripts, 1):
            if ENTITIES.search(s):
                bad = set(ENTITIES.findall(s))
                verdict = "ENTITIES %s" % sorted(bad)[:3]
                fails.append("%s script #%d contains HTML entities %s" % (tpl, i, sorted(bad)[:3]))
                continue
            if node:
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as f:
                    f.write(s)
                    path = f.name
                p = subprocess.run([node, "--check", path], capture_output=True, text=True)
                os.unlink(path)
                if p.returncode != 0:
                    err = (p.stderr.strip().splitlines() or ["?"])[-1]
                    verdict = "SYNTAX ERROR"
                    fails.append("%s script #%d: %s" % (tpl, i, err))
        if verdict == "ok" and not node:
            verdict = "ok (node absent, entity check only)"
        print("  %-20s %8d %8d  %s" % (tpl, len(out), len(scripts), verdict))

    print()
    if fails:
        print("%d TEMPLATE FAILURE(S):" % len(fails))
        for f in fails:
            print("  *", f)
        return 1
    print("all %d templates render, and their JavaScript is clean" % len(PAGES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
