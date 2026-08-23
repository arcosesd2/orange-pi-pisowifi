"""PisoWiFi portal + vending server.

Flask app serving:
  * client captive portal (insert coin, countdown, pause/resume)
  * OS captive-portal probe endpoints (triggers the "sign in" popup)
  * admin dashboard (sales, active clients, rates, kick)
  * admin diagnostics (per-pin GPIO tester, coin pulse monitor, hardware config)
  * remote monitoring (signed heartbeats pushed to your website — see server/)

Run: PISOWIFI_CONFIG=/etc/pisowifi/config.json python3 main.py
"""
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta

from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_from_directory, session as websession,
)

import firewall
import pppoe
import shaper
from coinslot import CoinSlot
from db import Db, DEFAULT_ADMIN_PASSWORD
from diagnostics import PinManager, EINT_PINS, HEADER
from remote import RemoteReporter
from scheduler import Scheduler
from watchdog import Watchdog

__version__ = "2.4"

# Defaults for every runtime-editable setting. config.json overrides these,
# and DB settings (edited from the admin UI) override config.json — so config
# files from older installs keep working when new keys are added here.
DEFAULTS = {
    "hotspot_name": "PisoWiFi",
    "gateway_ip": "10.0.0.1",
    "portal_port": 8080,
    "db_path": "/var/lib/pisowifi/pisowifi.db",

    # interfaces / VLAN. While auto_detect_interfaces is true, lan_if and
    # wan_if are re-derived from the hardware present and rewritten into
    # config.json on every boot (pisowifi-detect), which is what lets one
    # SD card image run on any board. Set it false to pin them by hand.
    "auto_detect_interfaces": True,
    # Keep SSH/portal/ping open on the UPLINK interface. The firewall
    # otherwise drops everything arriving there, which is right for a
    # deployed machine and wrong for a bench board you still have to
    # finish building. seal.sh forces this back to false.
    "wan_management": False,
    # Default topology: eth0 = internet uplink (onboard), eth1 = antenna/LAN
    # (USB-ethernet). Set lan_if to wlan0 only for the Pi-broadcasts-its-own-WiFi
    # (hostapd) topology.
    "lan_if": "eth1",
    "wan_if": "eth0",
    "lan_vlan": 0,                 # 0 = off; e.g. 5 -> LAN is lan_if.5 (antenna VLAN)

    # hardware (physical/BOARD pin numbers — see admin Diagnostics page)
    "coin_gpio_pin": 3,
    "coin_edge": "falling",        # optocoupler pulls the line LOW per pulse
    "coin_bounce_ms": 30,
    "relay_gpio_pin": 11,          # 0 / null = no relay
    "relay_active_low": True,
    "pulse_end_gap_s": 0.7,
    "insert_window_s": 60,
    # Coins that land with no insert window open are held this long for
    # the next customer to claim, instead of being destroyed. 0 = go back
    # to discarding them (not recommended: the relay is the only other
    # thing stopping a customer losing their money).
    "uncredited_hold_s": 300,
    "denominations": {"1": 1, "5": 5, "10": 10, "20": 20},  # pulses -> pesos
    "pulse_value_pesos": 1,        # fallback for unmapped pulse counts

    # rates
    "minutes_per_peso": 20,
    "bonus_tiers": {"5": 120, "10": 260, "15": 400, "20": 560},

    # QoS / speed profiles (default "unlimited" = no shaping, preserving old behavior)
    "speed_profiles": {
        "default": {"up": "3mbit", "down": "5mbit"},
        "fast": {"up": "5mbit", "down": "10mbit"},
        "unlimited": None,
    },
    "default_speed": "unlimited",
    "rate_speed": {},              # optional {pesos: profile_name} override map

    # walled garden (destinations unpaid clients may reach) + reconcile cadence
    "walled_hosts": [],            # IPs / CIDRs (domains resolved at runtime)
    "walled_domains": [],
    "reconcile_interval_s": 20,

    # health watchdog (opt-in)
    "watchdog_enabled": False,
    "watchdog_interval_s": 60,
    "watchdog_wan_host": "8.8.8.8",
    "watchdog_services": ["dnsmasq"],
    "watchdog_reboot_after": 0,    # consecutive WAN fails before reboot (0 = never)
    "watchdog_reboot": False,

    # --- v2.4 ---
    # QoS / firewall toggles (some are consumed by install.sh from config.json)
    "gaming_priority": False,      # CAKE diffserv4 (prioritize game/VoIP)
    "anti_tether": False,          # TTL=1 so paid WiFi can't be re-shared
    "flow_offload": False,         # kernel fast-path (scope vs QoS — see README)
    # free trial
    "trial_enabled": False,
    "trial_minutes": 15,
    "trial_period_hours": 24,
    "trial_speed": None,           # None -> default_speed
    # wallet / credit carry-over
    "wallet_enabled": False,
    "wallet_save_leftover": False,
    # portal branding
    "branding": {"color": "#fbbf24", "logo": "", "banner": "",
                 "show_redeem": True, "show_trial": True, "show_pause": True},
    # real-time portal (opt-in; polling scales better for many clients).
    # Each open stream holds one of waitress's 8 worker threads for its whole
    # life, so streams are capped and expire — see /api/stream.
    "sse_enabled": False,
    "sse_max_clients": 4,
    "sse_max_seconds": 300,
    # dns filtering
    "dns_filter": False,
    "dns_blocklist_url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
    # scheduled tasks: list of {type, time "HH:MM", days [0-6], args...}
    "schedules": [],
    # pppoe
    "pppoe_enabled": False,

    # remote monitoring (see server/README.md)
    "remote_enabled": False,
    "remote_url": "",
    "remote_key": "",
    "remote_interval_s": 300,
    "device_id": "",

    "admin_password": DEFAULT_ADMIN_PASSWORD,
    # Who may reach /admin from the customer LAN. The portal port has to be
    # open to every client for the captive portal to work, so /admin sits on a
    # port customers can reach — this decides whether they get past the door.
    #   "whitelist" (default) — only whitelisted devices, once one exists
    #   "any"                 — pre-v2.5 behavior; only for a lab/bench setup
    "admin_lan_access": "whitelist",
}

HW_KEYS = ("coin_gpio_pin", "coin_edge", "coin_bounce_ms", "relay_gpio_pin",
           "relay_active_low", "pulse_end_gap_s", "insert_window_s",
           "denominations", "pulse_value_pesos", "uncredited_hold_s")

CFG_PATH = os.environ.get(
    "PISOWIFI_CONFIG", os.path.join(os.path.dirname(__file__), "config.json")
)
with open(CFG_PATH) as f:
    CFG = {**DEFAULTS, **json.load(f)}

# On a dev PC (no nft/GPIO) run fully mocked.
MOCK = not sys.platform.startswith("linux")

db = Db(CFG["db_path"] if not MOCK else os.path.join(os.path.dirname(__file__), "dev.db"))

# uploaded portal branding (logo/banner) lives next to the DB
BRAND_DIR = os.path.join(os.path.dirname(db.path) or ".", "branding")
os.makedirs(BRAND_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = db.secret_key()   # persisted in DB -> admin sessions survive restarts
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",         # blocks cross-site POST cookies (core CSRF defense)
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),   # admin idle logout
)
BOOT_TIME = time.time()


# ---------- CSRF + audit helpers ----------

def _csrf_token():
    tok = websession.get("csrf")
    if not tok:
        tok = secrets.token_hex(16)
        websession["csrf"] = tok
    return tok


@app.context_processor
def _inject_csrf():
    return {"csrf": _csrf_token()}


@app.template_filter("ts")
def _ts_filter(t):
    try:
        return time.strftime("%b %d %H:%M", time.localtime(float(t)))
    except Exception:
        return ""


# CSRF token required on state-changing admin form POSTs. The JSON AJAX
# endpoints (/admin/api/*), remote-test and login are covered by SameSite=Lax
# + the session instead, so they stay usable without token plumbing.
_CSRF_EXEMPT = ("/admin/login", "/admin/remote-test")


@app.before_request
def _csrf_guard():
    p = request.path
    if (request.method == "POST" and p.startswith("/admin")
            and p not in _CSRF_EXEMPT and not p.startswith("/admin/api/")):
        sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not hmac.compare_digest(sent, websession.get("csrf", "")):
            return "bad or missing CSRF token", 400


def _audit(action, detail=None):
    db.log_audit(action, detail, request.remote_addr)


# ---------- admin exposure control ----------
# The captive portal forces the portal port open to every client, and /admin
# lives on that same port — so without this guard any customer on the open
# SSID could reach the login form and start guessing. The rule: customers are
# refused at the door; the owner reaches admin either from a whitelisted
# device, from the box itself, or over a private path like Tailscale.
#
# The bootstrap hole is deliberate and closes itself: SSH is blocked on both
# interfaces after install, so on a fresh machine there is no other way in.
# Admin stays LAN-reachable until the first device is whitelisted, and adding
# that device is what locks the machine down.

def _customer_net():
    """The customer subnet, derived from the gateway address."""
    try:
        return ipaddress.ip_network(f"{setting('gateway_ip')}/24", strict=False)
    except ValueError:
        return None


def _from_customer_lan():
    """True if this request came from a client on the customer network.
    Anything else — localhost, Tailscale, another interface — is the owner."""
    net = _customer_net()
    if not net:
        return False
    try:
        return ipaddress.ip_address(request.remote_addr or "") in net
    except ValueError:
        return False


def _admin_lan_denied():
    """Why (if at all) this request must not reach /admin. None = allowed."""
    if (setting("admin_lan_access") or "whitelist") == "any":
        return None
    if not _from_customer_lan():
        return None                      # box itself / Tailscale / other iface
    if not db.has_whitelist():
        return None                      # fresh machine — see note above
    if db.is_whitelisted(client_mac()):
        return None
    return ("Admin is not reachable from the customer network on this machine.\n\n"
            "Open it from a whitelisted device, from the machine itself, or over "
            "Tailscale. To recover a lockout, set \"admin_lan_access\": \"any\" in "
            "/etc/pisowifi/config.json and restart the pisowifi service.")


@app.before_request
def _admin_guard():
    """Door policy for /admin: who may reach it, and the forced first-run
    password change once they have."""
    if not request.path.startswith("/admin"):
        return None
    denied = _admin_lan_denied()
    if denied:
        return denied, 403
    # A machine still on the shipped password can do nothing but change it.
    if (admin_ok() and db.admin_password_is_default()
            and request.path not in ("/admin/setup", "/admin/logout")):
        return redirect("/admin/setup")
    return None


# ---------- settings: DB overrides config.json defaults ----------

def setting(key):
    return db.get_setting(key, CFG[key])


def hwcfg():
    return {k: setting(k) for k in HW_KEYS}


def minutes_for(pesos):
    tiers = setting("bonus_tiers") or {}
    if str(pesos) in tiers:
        return int(tiers[str(pesos)])
    return int(pesos * setting("minutes_per_peso"))


def rate_rows():
    """Sorted (pesos, minutes) price list for display: the per-denomination
    table, with the ₱1 fallback rate filled in if not explicitly listed."""
    tiers = setting("bonus_tiers") or {}
    rows = sorted((int(p), int(m)) for p, m in tiers.items())
    if not any(p == 1 for p, _ in rows):
        rows.insert(0, (1, int(setting("minutes_per_peso"))))
    return rows


# ---------- client identification ----------

def client_mac():
    ip = request.remote_addr
    if MOCK:
        return "aa:bb:cc:dd:ee:01"
    try:
        out = subprocess.run(
            ["ip", "neigh", "show", ip], capture_output=True, text=True
        ).stdout
        for tok_i, tok in enumerate(parts := out.split()):
            if tok == "lladdr":
                return parts[tok_i + 1].lower()
    except Exception:
        pass
    return None


# ---------- speed profiles (QoS) ----------

def _speed_for_pesos(pesos):
    """Which speed profile a coin amount buys (rate_speed map, else default)."""
    return (setting("rate_speed") or {}).get(str(pesos)) or setting("default_speed")


def _resolve_speed(name):
    """Profile name -> {'up','down'} rate strings, or None for unlimited."""
    return (setting("speed_profiles") or {}).get(name)


def _ip_for_mac(mac):
    """Reverse of client_mac(): current LAN IP for a MAC, via the neighbor table."""
    if MOCK or not mac:
        return None
    try:
        out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if "lladdr" in parts and parts[parts.index("lladdr") + 1].lower() == mac:
                return parts[0]
    except Exception:
        pass
    return None


def _apply_speed(mac, speed_name):
    """Install (or clear) the per-client bandwidth cap for mac's current IP."""
    if MOCK:
        return
    ip = _ip_for_mac(mac)
    if not ip:
        return
    prof = _resolve_speed(speed_name or setting("default_speed"))
    if prof and prof.get("down"):
        shaper.limit(ip, prof.get("up"), prof.get("down"))
    else:
        shaper.unlimit(ip)


def _unapply_speed(mac):
    """Drop any bandwidth cap on mac's current IP (on pause/kick)."""
    if MOCK:
        return
    ip = _ip_for_mac(mac)
    if ip:
        shaper.unlimit(ip)


# ---------- credit application ----------

def _grant(mac, minutes, source="coin", pesos=0, speed=None):
    """Add `minutes` to mac's session, open the firewall, apply QoS, log a sale.
    Pause-aware: buying while paused resumes; while active, stacks on top.
    Shared by the coin path and voucher redemption."""
    minutes = int(minutes)
    if minutes <= 0:
        return
    now = time.time()
    sess = db.get_session(mac)
    base = now
    if sess:
        if sess["paused_remaining"] is not None:
            base = now + sess["paused_remaining"]  # buying while paused resumes
        elif sess["expires_at"] > now:
            base = sess["expires_at"]
    expires = base + minutes * 60
    db.set_session(mac, expires, None)
    db.set_session_speed(mac, speed)
    db.record_sale(mac, pesos, minutes)
    if not MOCK:
        firewall.allow(mac, expires - now)
        _apply_speed(mac, speed)
    app.logger.info("grant %s via %s: +%d min (P%s) until %s", mac, source, minutes,
                    pesos, time.strftime("%H:%M", time.localtime(expires)))


def apply_credit(mac, pesos):
    """Coin path: convert inserted pesos to time (unchanged public signature)."""
    if pesos <= 0:
        return
    _grant(mac, minutes_for(pesos), source="coin", pesos=pesos,
           speed=_speed_for_pesos(pesos))


slot = CoinSlot(
    hwcfg(),
    on_coin=lambda mac, pesos: app.logger.info("coin: P%d from %s", pesos, mac),
    on_timeout=apply_credit,  # never swallow money if the user walks away
)


def _reserved_pins():
    cfg = slot.cfg
    r = {int(cfg["coin_gpio_pin"]): "coin"}
    if cfg.get("relay_gpio_pin"):
        r[int(cfg["relay_gpio_pin"])] = "relay"
    return r


pins = PinManager(_reserved_pins)


# ---------- remote monitoring ----------

def _remote_settings():
    return {k: setting(k) for k in
            ("remote_enabled", "remote_url", "remote_key",
             "remote_interval_s", "device_id")}


def sysinfo():
    info = {"uptime_s": int(time.time() - BOOT_TIME), "version": __version__}
    try:
        with open("/proc/uptime") as f:
            info["uptime_s"] = int(float(f.read().split()[0]))
        info["load"] = round(os.getloadavg()[0], 2)
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        info["mem_free_mb"] = mem.get("MemAvailable", 0) // 1024
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = round(int(f.read()) / 1000, 1)
    except Exception:
        pass
    return info


def _heartbeat_payload():
    now = time.time()
    return {
        "device_id": setting("device_id") or socket.gethostname(),
        "name": setting("hotspot_name"),
        "ts": now,
        "sales": db.sales_summary(),
        "active_clients": len(db.active_sessions()),
        "sys": sysinfo(),
    }


reporter = RemoteReporter(_remote_settings, _heartbeat_payload, app.logger)


# ---------- rate limiting + voucher codes ----------

_RL = {}
_RL_LOCK = threading.Lock()


def rate_limited(key, limit, window):
    """True if `key` has exceeded `limit` events in the last `window` seconds
    (simple in-memory sliding window; abuse protection for insert/redeem/login)."""
    now = time.time()
    with _RL_LOCK:
        q = _RL.setdefault(key, [])
        cutoff = now - window
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= limit:
            return True
        q.append(now)
        return False


_VCHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous 0/O/1/I


def _gen_code(n=6):
    return "".join(secrets.choice(_VCHARS) for _ in range(n))


# ---------- admin login throttling ----------
# The sliding window above is keyed on the client IP, which a customer chooses
# for themselves (renew the lease, or just set a static address), so on its own
# it is no brake at all on password guessing. Lockouts are therefore keyed on
# the MAC — changing that drops the DHCP lease and the session with it — and
# they back off exponentially instead of resetting every minute.

_LOGIN_FAILS = {}                  # key -> [consecutive_failures, locked_until]
_LOGIN_FAILS_LOCK = threading.Lock()
_LOGIN_FREE_TRIES = 5
_LOGIN_MAX_LOCK_S = 3600


def _login_key():
    return client_mac() or request.remote_addr or "?"


def _login_lock_remaining(key):
    with _LOGIN_FAILS_LOCK:
        rec = _LOGIN_FAILS.get(key)
        return max(0, int(rec[1] - time.time())) if rec else 0


def _login_failed(key):
    """Record a failure and arm the next lockout: 1 min, 2, 4, 8… up to an hour."""
    with _LOGIN_FAILS_LOCK:
        rec = _LOGIN_FAILS.setdefault(key, [0, 0.0])
        rec[0] += 1
        if rec[0] >= _LOGIN_FREE_TRIES:
            back = min(_LOGIN_MAX_LOCK_S, 60 * 2 ** (rec[0] - _LOGIN_FREE_TRIES))
            rec[1] = time.time() + back
        return rec[0]


def _login_succeeded(key):
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.pop(key, None)


# ---------- client portal ----------

@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def portal():
    return render_template(
        "portal.html", name=setting("hotspot_name"), rates=rate_rows(),
        brand=setting("branding") or {},
        # Drives the "Finish setup" banner: on a card fresh off a master image
        # the captive portal is the only thing the owner sees, so it has to be
        # what points them at /admin.
        unconfigured=db.admin_password_is_default(),
        trial_enabled=setting("trial_enabled"), trial_minutes=setting("trial_minutes"),
        wallet_enabled=setting("wallet_enabled"), sse_enabled=setting("sse_enabled"),
    )


@app.route("/branding/<path:fn>")
def branding_file(fn):
    return send_from_directory(BRAND_DIR, fn)


@app.route("/api/capport")
def api_capport():
    """RFC 8908 captive-portal API (advertised via DHCP option 114).
    Phones poll this and keep a persistent "Sign in" notification while
    `captive` is true — i.e. until this client has paid time running."""
    mac = client_mac()
    sess = db.get_session(mac) if mac else None
    remaining = 0
    if sess and sess["paused_remaining"] is None:
        remaining = max(0, int(sess["expires_at"] - time.time()))
    resp = jsonify({
        "captive": remaining <= 0,
        "user-portal-url": f"http://{CFG['gateway_ip']}:{CFG['portal_port']}/",
        "seconds-remaining": remaining,
        "can-extend-session": True,
    })
    resp.headers["Content-Type"] = "application/captive+json"
    return resp


def _status_payload(mac):
    now = time.time()
    sess = db.get_session(mac)
    paused = bool(sess and sess["paused_remaining"] is not None)
    remaining = 0
    if sess:
        remaining = int(sess["paused_remaining"] if paused
                        else max(0, sess["expires_at"] - now))
    st = slot.status(mac)
    return {
        "mac": mac, "remaining": remaining, "paused": paused, "window": st,
        "projected_minutes": minutes_for(st["pesos"]) if st["pesos"] else 0,
        "wallet": db.wallet_balance(mac) if setting("wallet_enabled") else 0,
    }


@app.route("/api/status")
def api_status():
    mac = client_mac()
    if not mac:
        return jsonify(error="Could not identify your device — reconnect to WiFi."), 400
    return jsonify(**_status_payload(mac))


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_sse_open = 0
_sse_lock = threading.Lock()


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events push of this client's status (opt-in; the portal
    falls back to polling when disabled).

    An open stream occupies one waitress worker thread for its entire life and
    there are only 8 of them, so without a limit a handful of phones would
    starve the portal and admin for everyone. Two bounds keep that from
    happening: at most `sse_max_clients` streams at once, and each expires
    after `sse_max_seconds` (the browser's EventSource reconnects, which hands
    the thread back). Clients refused here just keep using the portal's poll."""
    global _sse_open
    if not setting("sse_enabled"):
        return jsonify(error="disabled"), 404
    mac = client_mac()
    if not mac:
        return jsonify(error="unidentified device"), 400
    if request.args.get("once"):
        return app.response_class(
            "data: " + json.dumps(_status_payload(mac)) + "\n\n",
            mimetype="text/event-stream", headers=_SSE_HEADERS)

    cap = max(1, int(setting("sse_max_clients") or 4))
    with _sse_lock:
        if _sse_open >= cap:
            return jsonify(error="too many live streams — falling back to polling"), 503
        _sse_open += 1

    def gen():
        global _sse_open
        try:
            deadline = time.monotonic() + max(30, int(setting("sse_max_seconds") or 300))
            while time.monotonic() < deadline:
                yield "data: " + json.dumps(_status_payload(mac)) + "\n\n"
                time.sleep(1)
        finally:
            with _sse_lock:
                _sse_open -= 1
    return app.response_class(gen(), mimetype="text/event-stream",
                              headers=_SSE_HEADERS)


@app.route("/api/insert", methods=["POST"])
def api_insert():
    mac = client_mac()
    if not mac:
        return jsonify(error="unidentified device"), 400
    if rate_limited("insert:" + (request.remote_addr or mac), 20, 60):
        return jsonify(error="Too many requests — slow down."), 429
    if not slot.open_window(mac):
        return jsonify(error="Coin slot is in use by another customer — try again shortly."), 409
    return jsonify(ok=True)


@app.route("/api/done", methods=["POST"])
def api_done():
    mac = client_mac()
    pesos = slot.close_window(mac)
    apply_credit(mac, pesos)
    return jsonify(ok=True, pesos=pesos)


@app.route("/api/pause", methods=["POST"])
def api_pause():
    mac = client_mac()
    sess = db.get_session(mac)
    now = time.time()
    if not sess or sess["paused_remaining"] is not None or sess["expires_at"] <= now:
        return jsonify(error="no active time to pause"), 400
    db.set_session(mac, now, sess["expires_at"] - now)
    if not MOCK:
        firewall.revoke(mac)
        _unapply_speed(mac)
    return jsonify(ok=True)


@app.route("/api/resume", methods=["POST"])
def api_resume():
    mac = client_mac()
    sess = db.get_session(mac)
    if not sess or sess["paused_remaining"] is None:
        return jsonify(error="nothing to resume"), 400
    remaining = sess["paused_remaining"]
    db.set_session(mac, time.time() + remaining, None)
    if not MOCK:
        firewall.allow(mac, remaining)
        _apply_speed(mac, sess["speed"])
    return jsonify(ok=True)


@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    """Redeem a voucher code for time (same grant path as coins)."""
    mac = client_mac()
    if not mac:
        return jsonify(error="unidentified device"), 400
    if rate_limited("redeem:" + (request.remote_addr or mac), 8, 60):
        return jsonify(error="Too many tries — wait a moment."), 429
    data = request.get_json(silent=True) or request.form
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify(error="Enter a voucher code"), 400
    v = db.redeem_voucher(code, mac)
    if not v:
        return jsonify(error="Invalid, already-used, or expired code"), 400
    _grant(mac, v["minutes"], source="voucher", pesos=v["price"] or 0, speed=v["speed"])
    return jsonify(ok=True, minutes=v["minutes"])


@app.route("/api/trial", methods=["POST"])
def api_trial():
    """Grant a free trial, once per configured period per device."""
    if not setting("trial_enabled"):
        return jsonify(error="Free trial is not available"), 400
    mac = client_mac()
    if not mac:
        return jsonify(error="unidentified device"), 400
    if rate_limited("trial:" + (request.remote_addr or mac), 5, 60):
        return jsonify(error="Too many tries — wait a moment."), 429
    period = float(setting("trial_period_hours")) * 3600
    t = db.get_trial(mac)
    if t and (time.time() - t["last_at"]) < period:
        hrs = (period - (time.time() - t["last_at"])) / 3600
        return jsonify(error=f"Free trial already used — try again in ~{hrs:.0f}h."), 429
    db.mark_trial(mac)
    _grant(mac, int(setting("trial_minutes")), source="trial", pesos=0,
           speed=setting("trial_speed") or setting("default_speed"))
    return jsonify(ok=True, minutes=int(setting("trial_minutes")))


@app.route("/api/wallet/use", methods=["POST"])
def api_wallet_use():
    """Apply the device's saved credit balance to a new session."""
    if not setting("wallet_enabled"):
        return jsonify(error="Saved credit is not available"), 400
    mac = client_mac()
    if not mac:
        return jsonify(error="unidentified device"), 400
    speed = db.wallet_speed(mac)       # banked time keeps the speed it was bought at
    secs = db.wallet_take(mac)
    if secs < 60:
        if secs:
            db.wallet_add(mac, secs, speed)   # too little to grant; keep it
        return jsonify(error="No saved credit to use"), 400
    db.wallet_add(mac, secs % 60, speed)      # bank the leftover < 1 min
    _grant(mac, secs // 60, source="wallet", pesos=0, speed=speed)
    return jsonify(ok=True, minutes=secs // 60)


@app.route("/api/wallet/save", methods=["POST"])
def api_wallet_save():
    """Bank the device's remaining time and disconnect (use it later)."""
    if not setting("wallet_enabled"):
        return jsonify(error="Saved credit is not available"), 400
    mac = client_mac()
    sess = db.get_session(mac) if mac else None
    now = time.time()
    remaining = 0
    if sess:
        remaining = int(sess["paused_remaining"] if sess["paused_remaining"] is not None
                        else max(0, sess["expires_at"] - now))
    if remaining <= 0:
        return jsonify(error="No time to save"), 400
    db.wallet_add(mac, remaining, sess["speed"])
    db.delete_session(mac)
    if not MOCK:
        firewall.revoke(mac)
        _unapply_speed(mac)
    return jsonify(ok=True, saved_minutes=remaining // 60)


# ---------- OS captive-portal probes ----------
# Unpaid clients' port-80 traffic is redirected here; answering the probe
# with a redirect is what makes phones pop the "sign in to network" screen.
# (Paid clients bypass the redirect entirely, so these never lie to them.)

_PROBES = [
    "/generate_204", "/gen_204",                      # Android
    "/hotspot-detect.html", "/library/test/success.html",  # Apple
    "/ncsi.txt", "/connecttest.txt", "/redirect",     # Windows
    "/success.txt", "/canonical.html",                # Firefox/NM
]
for p in _PROBES:
    app.add_url_rule(
        p, f"probe_{p}",
        lambda: redirect(f"http://{CFG['gateway_ip']}:{CFG['portal_port']}/", 302),
    )


@app.route("/<path:_any>")
def catchall(_any):
    return redirect(f"http://{CFG['gateway_ip']}:{CFG['portal_port']}/", 302)


# ---------- admin ----------

def admin_ok():
    return websession.get("admin") is True


def _admin_api_guard():
    if not admin_ok():
        return jsonify(error="not logged in"), 401
    return None


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        key = _login_key()
        locked = _login_lock_remaining(key)
        if locked:
            mins = max(1, locked // 60)
            _audit("login blocked (locked out)", key)
            return render_template(
                "admin_login.html",
                error=f"Too many failed attempts — locked for ~{mins} min."), 429
        pw = request.form.get("password") or ""
        # verify_admin seeds/upgrades the legacy plaintext to a hash on first use
        if db.verify_admin(pw, legacy_plaintext=setting("admin_password")):
            _login_succeeded(key)
            websession.permanent = True
            websession["admin"] = True
            _csrf_token()
            _audit("admin login")
            return redirect("/admin/setup" if db.admin_password_is_default()
                            else "/admin")
        n = _login_failed(key)
        _audit("failed login", f"{key} (attempt {n})")
        return render_template("admin_login.html", error="Wrong password")
    return render_template("admin_login.html", error=None)


@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    """Forced first-run password change. A machine on the shipped password is
    treated as unconfigured — every other admin page bounces here until it is
    changed, so a box can't be left in the field on `changeme`."""
    if not admin_ok():
        return redirect("/admin/login")
    if not db.admin_password_is_default():
        return redirect("/admin")
    error = None
    if request.method == "POST":
        pw = request.form.get("password") or ""
        if pw != (request.form.get("confirm") or ""):
            error = "The two passwords don't match."
        elif len(pw) < 8:
            error = "Use at least 8 characters."
        elif pw == DEFAULT_ADMIN_PASSWORD:
            error = "That's the shipped default — pick something else."
        else:
            db.set_admin_password(pw)
            # drop the plaintext seed so the legacy fallback can't be used again
            db.set_setting("admin_password", "")
            mac = client_mac()
            # Whitelisting the device that does the setup keeps the owner's own
            # access working the moment the machine locks down.
            if mac and request.form.get("whitelist_me") == "on":
                db.set_device(mac, "white", "owner (set up this machine)")
                if not MOCK:
                    firewall.whitelist_add(mac)
                _audit("device add", f"white {mac} (first-run)")
            _audit("admin password set (first run)")
            return redirect("/admin")
    return render_template("admin_setup.html", name=setting("hotspot_name"),
                           error=error, mac=client_mac(),
                           on_lan=_from_customer_lan())


@app.route("/admin/logout")
def admin_logout():
    websession.clear()
    return redirect("/admin/login")


@app.route("/admin")
def admin():
    if not admin_ok():
        return redirect("/admin/login")
    now = time.time()
    active = [
        {
            "mac": s["mac"],
            "paused": s["paused_remaining"] is not None,
            "remaining": int(s["paused_remaining"] if s["paused_remaining"] is not None
                             else max(0, s["expires_at"] - now)),
        }
        for s in db.active_sessions()
    ]
    return render_template(
        "admin.html", sales=db.sales_summary(), active=active,
        rate=setting("minutes_per_peso"),
        tiers=setting("bonus_tiers"),
        name=setting("hotspot_name"), mock=slot.mock,
        remote=_remote_settings(), remote_last=reporter.last,
        device_id_default=socket.gethostname(), sys=sysinfo(),
        daily=db.daily_sales(14), vouchers=db.voucher_stats(),
        watch=watch.last,
    )


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    if f.get("minutes_per_peso"):
        db.set_setting("minutes_per_peso", int(f["minutes_per_peso"]))
    if f.get("bonus_tiers"):
        db.set_setting("bonus_tiers", json.loads(f["bonus_tiers"]))
    if f.get("hotspot_name"):
        db.set_setting("hotspot_name", f["hotspot_name"])
    if f.get("admin_password"):
        db.set_admin_password(f["admin_password"])
    _audit("settings updated")
    return redirect("/admin")


@app.route("/admin/remote", methods=["POST"])
def admin_remote():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    db.set_setting("remote_enabled", f.get("remote_enabled") == "on")
    db.set_setting("remote_url", f.get("remote_url", "").strip())
    if f.get("remote_key"):
        db.set_setting("remote_key", f["remote_key"].strip())
    db.set_setting("remote_interval_s", max(60, int(f.get("remote_interval_s") or 300)))
    db.set_setting("device_id", f.get("device_id", "").strip())
    reporter.poke()
    _audit("remote settings updated")
    return redirect("/admin")


@app.route("/admin/remote-test", methods=["POST"])
def admin_remote_test():
    guard = _admin_api_guard()
    if guard:
        return guard
    return jsonify(reporter.send_now())


@app.route("/admin/kick", methods=["POST"])
def admin_kick():
    if not admin_ok():
        return redirect("/admin/login")
    mac = request.form["mac"]
    if setting("wallet_enabled") and setting("wallet_save_leftover"):
        sess = db.get_session(mac)
        now = time.time()
        if sess:
            rem = int(sess["paused_remaining"] if sess["paused_remaining"] is not None
                      else max(0, sess["expires_at"] - now))
            if rem > 0:
                db.wallet_add(mac, rem, sess["speed"])
    db.delete_session(mac)
    if not MOCK:
        firewall.revoke(mac)
        _unapply_speed(mac)
    _audit("kick", mac)
    return redirect("/admin")


@app.route("/admin/simulate-coin", methods=["POST"])
def admin_simulate():
    if not admin_ok() or not slot.mock:
        return redirect("/admin/login")
    slot.simulate(int(request.form.get("pesos", 1)))
    return redirect("/admin")


# ---------- admin: vouchers ----------

@app.route("/admin/vouchers")
def admin_vouchers():
    if not admin_ok():
        return redirect("/admin/login")
    return render_template(
        "vouchers.html", name=setting("hotspot_name"),
        vouchers=db.list_vouchers("all", 300), stats=db.voucher_stats(),
        profiles=list((setting("speed_profiles") or {}).keys()),
    )


@app.route("/admin/vouchers/generate", methods=["POST"])
def admin_vouchers_generate():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    count = max(1, min(500, int(f.get("count") or 1)))
    minutes = max(1, int(f.get("minutes") or 60))
    price = int(f.get("price") or 0)
    speed = f.get("speed") or None
    batch = (f.get("batch") or time.strftime("%Y%m%d-%H%M")).strip()
    days = int(f.get("expires_days") or 0)
    expires_at = time.time() + days * 86400 if days else None
    for _ in range(count):
        for _try in range(12):
            code = _gen_code()
            if not db.get_voucher(code):
                db.create_voucher(code, minutes, speed, price, batch, expires_at)
                break
    _audit("vouchers generated", f"{count} x {minutes}min (batch {batch})")
    return redirect("/admin/vouchers")


@app.route("/admin/vouchers/revoke", methods=["POST"])
def admin_vouchers_revoke():
    if not admin_ok():
        return redirect("/admin/login")
    code = (request.form.get("code") or "").strip().upper()
    db.revoke_voucher(code)
    _audit("voucher revoked", code)
    return redirect("/admin/vouchers")


@app.route("/admin/vouchers.csv")
def admin_vouchers_csv():
    if not admin_ok():
        return redirect("/admin/login")
    cols = ("code", "minutes", "speed", "price", "batch",
            "created_at", "expires_at", "used_at", "used_by_mac")

    def gen():
        yield ",".join(cols) + "\n"
        for v in db.list_vouchers("all", 100000):
            yield ",".join(str(v.get(k) if v.get(k) is not None else "") for k in cols) + "\n"
    return app.response_class(gen(), mimetype="text/csv",
                              headers={"Content-Disposition": "attachment; filename=vouchers.csv"})


# ---------- admin: analytics export / backup ----------

@app.route("/admin/sales.csv")
def admin_sales_csv():
    if not admin_ok():
        return redirect("/admin/login")

    def gen():
        yield "id,mac,pesos,minutes,created_at,datetime\n"
        for s in db.iter_sales():
            dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["created_at"]))
            yield f'{s["id"]},{s["mac"]},{s["pesos"]},{s["minutes"]},{s["created_at"]},{dt}\n'
    return app.response_class(gen(), mimetype="text/csv",
                              headers={"Content-Disposition": "attachment; filename=sales.csv"})


@app.route("/admin/backup")
def admin_backup():
    if not admin_ok():
        return redirect("/admin/login")
    data = db.export_config(include_sales=request.args.get("sales") == "1")
    return app.response_class(
        json.dumps(data, indent=2), mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=pisowifi-backup.json"})


@app.route("/admin/restore", methods=["POST"])
def admin_restore():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.files.get("file")
    if f:
        try:
            db.import_config(json.loads(f.read().decode()))
            if not MOCK:
                firewall.load_device_sets(db.list_devices())
                _refresh_walled()
            _audit("config restored")
        except Exception as e:
            app.logger.warning("restore failed: %s", e)
    return redirect("/admin")


# ---------- admin: devices (whitelist / blacklist) ----------

@app.route("/admin/devices")
def admin_devices():
    if not admin_ok():
        return redirect("/admin/login")
    return render_template(
        "devices.html", name=setting("hotspot_name"),
        whitelist=db.list_devices("white"), blacklist=db.list_devices("black"),
        profiles=list((setting("speed_profiles") or {}).keys()),
    )


@app.route("/admin/devices/add", methods=["POST"])
def admin_devices_add():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    mac = (f.get("mac") or "").strip().lower()
    kind = "black" if f.get("kind") == "black" else "white"
    if firewall._MAC_RE.match(mac):
        db.set_device(mac, kind, f.get("label"), f.get("speed") or None)
        if not MOCK:
            if kind == "white":
                firewall.block_remove(mac)
                firewall.whitelist_add(mac)
            else:
                firewall.whitelist_remove(mac)
                firewall.block_add(mac)
        _audit("device add", f"{kind} {mac}")
    return redirect("/admin/devices")


@app.route("/admin/devices/remove", methods=["POST"])
def admin_devices_remove():
    if not admin_ok():
        return redirect("/admin/login")
    mac = (request.form.get("mac") or "").strip().lower()
    db.delete_device(mac)
    if not MOCK and firewall._MAC_RE.match(mac):
        firewall.whitelist_remove(mac)
        firewall.block_remove(mac)
    _audit("device remove", mac)
    return redirect("/admin/devices")


# ---------- admin: audit log ----------

@app.route("/admin/audit")
def admin_audit():
    if not admin_ok():
        return redirect("/admin/login")
    return render_template("audit.html", name=setting("hotspot_name"),
                           entries=db.list_audit(300))


# ---------- admin: branding ----------

@app.route("/admin/branding", methods=["GET", "POST"])
def admin_branding():
    if not admin_ok():
        return redirect("/admin/login")
    b = dict(setting("branding") or {})
    if request.method == "POST":
        f = request.form
        b["color"] = f.get("color") or b.get("color", "#fbbf24")
        for k in ("show_redeem", "show_trial", "show_pause"):
            b[k] = f.get(k) == "on"
        for key in ("logo", "banner"):
            if f.get("clear_" + key):
                b[key] = ""
            up = request.files.get(key)
            if up and up.filename:
                ext = os.path.splitext(up.filename)[1].lower()
                if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
                    name = key + ext
                    up.save(os.path.join(BRAND_DIR, name))
                    b[key] = name
        db.set_setting("branding", b)
        _audit("branding updated")
        return redirect("/admin/branding")
    return render_template("branding.html", name=setting("hotspot_name"), b=b)


# ---------- admin: scheduled tasks + DNS blocklist ----------

@app.route("/admin/schedules")
def admin_schedules():
    if not admin_ok():
        return redirect("/admin/login")
    return _schedules_page()


# Job types the scheduler actually implements. Anything else is accepted into
# the list and then logged as "unknown job type" once a day, where nobody sees
# it — so reject it at the door instead.
SCHEDULE_TYPES = ("reboot", "backup", "set_rate", "blocklist_refresh", "report")


def _schedules_page(error=None):
    return render_template(
        "schedules.html", name=setting("hotspot_name"),
        schedules=setting("schedules") or [], error=error,
        dns_filter=setting("dns_filter"), blocklist=blocklist_status)


@app.route("/admin/schedules/add", methods=["POST"])
def admin_schedules_add():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    jobs = list(setting("schedules") or [])
    # Validate before storing. The scheduler fires a job by comparing its
    # `time` to strftime("%H:%M"), so anything that is not exactly HH:MM simply
    # never matches -- the job is accepted, listed in the table, and silently
    # never runs. That is worst for the jobs you notice least: nightly backups
    # and the auto-reboot.
    hhmm = (f.get("time") or "03:00").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hhmm):
        return _schedules_page(error=f"Time must be HH:MM in 24-hour form — {hhmm!r} is not.")
    jtype = f.get("type")
    if jtype not in SCHEDULE_TYPES:
        return _schedules_page(error=f"Unknown job type {jtype!r}.")
    job = {"type": jtype, "time": hhmm,
           "days": sorted({int(d) for d in f.getlist("days") if d.isdigit() and 0 <= int(d) <= 6})}
    if job["type"] == "set_rate":
        try:
            job["value"] = max(1, int(f.get("value") or 20))
        except ValueError:
            return _schedules_page(error="Rate change needs a whole number of minutes.")
    jobs.append(job)
    db.set_setting("schedules", jobs)
    _audit("schedule add", job.get("type"))
    return redirect("/admin/schedules")


@app.route("/admin/schedules/remove", methods=["POST"])
def admin_schedules_remove():
    if not admin_ok():
        return redirect("/admin/login")
    idx = int(request.form.get("idx", -1))
    jobs = list(setting("schedules") or [])
    if 0 <= idx < len(jobs):
        jobs.pop(idx)
        db.set_setting("schedules", jobs)
        _audit("schedule remove")
    return redirect("/admin/schedules")


@app.route("/admin/dns", methods=["POST"])
def admin_dns():
    if not admin_ok():
        return redirect("/admin/login")
    db.set_setting("dns_filter", request.form.get("dns_filter") == "on")
    if request.form.get("dns_blocklist_url"):
        db.set_setting("dns_blocklist_url", request.form["dns_blocklist_url"].strip())
    # Fetching and installing the list takes tens of seconds. Doing it inline
    # would hold one of the 8 worker threads for that whole time (and time the
    # page out), so hand it to a background thread; the schedules page reports
    # how it went.
    threading.Thread(target=_refresh_blocklist, daemon=True).start()
    _audit("dns filter", "refresh started")
    return redirect("/admin/schedules")


# ---------- admin: PPPoE monthly accounts ----------

@app.route("/admin/pppoe")
def admin_pppoe():
    if not admin_ok():
        return redirect("/admin/login")
    return render_template(
        "pppoe.html", name=setting("hotspot_name"),
        enabled=setting("pppoe_enabled"), accounts=db.list_pppoe_accounts(),
        profiles=list((setting("speed_profiles") or {}).keys()))


@app.route("/admin/pppoe/save", methods=["POST"])
def admin_pppoe_save():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    user = (f.get("user") or "").strip()
    if user and f.get("password"):
        db.set_pppoe_account(
            user, f["password"], plan_days=int(f.get("plan_days") or 0) or None,
            speed=f.get("speed") or None, note=f.get("note"))
        if not MOCK:
            pppoe.enforce(db)
        _audit("pppoe save", user)
    return redirect("/admin/pppoe")


@app.route("/admin/pppoe/toggle", methods=["POST"])
def admin_pppoe_toggle():
    if not admin_ok():
        return redirect("/admin/login")
    db.set_pppoe_enabled(request.form.get("user"), request.form.get("enable") == "1")
    if not MOCK:
        pppoe.enforce(db)
    _audit("pppoe toggle", request.form.get("user"))
    return redirect("/admin/pppoe")


@app.route("/admin/pppoe/remove", methods=["POST"])
def admin_pppoe_remove():
    if not admin_ok():
        return redirect("/admin/login")
    user = request.form.get("user")
    db.delete_pppoe_account(user)
    if not MOCK:
        pppoe.enforce(db)
    _audit("pppoe remove", user)
    return redirect("/admin/pppoe")


# ---------- admin: diagnostics ----------

@app.route("/admin/diag")
def admin_diag():
    if not admin_ok():
        return redirect("/admin/login")
    hw = hwcfg()
    return render_template(
        "diag.html", name=setting("hotspot_name"), hw=hw,
        denominations=hw["denominations"], mock=slot.mock,
        eint_pins=EINT_PINS,
    )


@app.route("/admin/hardware", methods=["POST"])
def admin_hardware():
    if not admin_ok():
        return redirect("/admin/login")
    f = request.form
    try:
        coin_pin = int(f["coin_gpio_pin"])
        if coin_pin not in EINT_PINS:
            raise ValueError(
                f"coin pin must be interrupt-capable (one of physical pins "
                f"{EINT_PINS} — the PA* pins); pin {coin_pin} "
                f"({HEADER.get(coin_pin, ('?',))[0]}) can't detect coin pulses reliably")
        db.set_setting("coin_gpio_pin", coin_pin)
        db.set_setting("coin_edge",
                       "rising" if f.get("coin_edge") == "rising" else "falling")
        db.set_setting("coin_bounce_ms", max(1, int(f["coin_bounce_ms"])))
        db.set_setting("relay_gpio_pin", int(f.get("relay_gpio_pin") or 0))
        db.set_setting("relay_active_low", f.get("relay_active_low") == "on")
        db.set_setting("pulse_end_gap_s", max(0.1, float(f["pulse_end_gap_s"])))
        db.set_setting("insert_window_s", max(10, int(f["insert_window_s"])))
        db.set_setting("pulse_value_pesos", max(1, int(f["pulse_value_pesos"])))
        denoms = json.loads(f["denominations"])
        db.set_setting("denominations",
                       {str(int(k)): int(v) for k, v in denoms.items()})
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return render_template(
            "diag.html", name=setting("hotspot_name"), hw=hwcfg(),
            denominations=setting("denominations"),
            mock=slot.mock, eint_pins=EINT_PINS,
            error=f"Not saved — invalid value: {e}",
        ), 400
    hw = hwcfg()
    for pin in (hw["coin_gpio_pin"], hw.get("relay_gpio_pin")):
        if pin:
            pins.force_release(int(pin))  # pin tester must let go before the slot re-arms
    slot.reconfigure(hw)
    return redirect("/admin/diag")


@app.route("/admin/api/diag")
def api_diag():
    guard = _admin_api_guard()
    if guard:
        return guard
    return jsonify(coin=slot.diag(), pins=pins.state(), scan=pins.scan_state(),
                   sys=sysinfo(), mock_app=MOCK)


@app.route("/admin/api/diag/pin", methods=["POST"])
def api_diag_pin():
    guard = _admin_api_guard()
    if guard:
        return guard
    d = request.get_json(force=True)
    try:
        action = d.get("action")
        if action == "watch":
            pins.watch(d["pin"], d.get("pull", "none"))
        elif action == "drive":
            pins.drive(d["pin"], d.get("level", 0))
        elif action == "release":
            pins.release(d["pin"])
        elif action == "reset_edges":
            pins.reset_edges(d["pin"])
        else:
            return jsonify(error="unknown action"), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/admin/api/diag/scan", methods=["POST"])
def api_diag_scan():
    guard = _admin_api_guard()
    if guard:
        return guard
    if request.get_json(force=True).get("on"):
        pins.scan_start()
    else:
        pins.scan_stop()
    return jsonify(ok=True)


@app.route("/admin/api/diag/relay", methods=["POST"])
def api_diag_relay():
    guard = _admin_api_guard()
    if guard:
        return guard
    d = request.get_json(force=True)
    slot.relay_test(bool(d.get("on")))
    return jsonify(ok=True, relay=slot.relay_state)


@app.route("/admin/api/diag/pulse", methods=["POST"])
def api_diag_pulse():
    guard = _admin_api_guard()
    if guard:
        return guard
    d = request.get_json(force=True)
    slot.inject(int(d.get("pulses", 1)))
    return jsonify(ok=True)


# ---------- VLAN-aware LAN device + walled garden ----------

def lan_dev():
    """The kernel interface carrying client traffic. With lan_vlan set it's the
    tagged sub-interface (e.g. eth0.5) that pairs with the antenna's VLAN."""
    vid = int(setting("lan_vlan") or 0)
    base = setting("lan_if")
    return f"{base}.{vid}" if vid else base


def _refresh_walled():
    """Rebuild the walled-garden nft set from configured IPs/CIDRs + resolved
    domains, so unpaid clients can reach owner-chosen free hosts."""
    if MOCK:
        return
    hosts = list(setting("walled_hosts") or [])
    for dom in (setting("walled_domains") or []):
        try:
            hosts += [i[4][0] for i in socket.getaddrinfo(dom, None, socket.AF_INET)]
        except Exception:
            pass
    firewall.set_walled(hosts)


# dnsmasq auto-includes /etc/dnsmasq.d/*.conf, so we just drop the blocklist there.
_BLOCKLIST_PATH = "/etc/dnsmasq.d/pisowifi-blocklist.conf"


def _reload_dnsmasq():
    subprocess.run(["systemctl", "reload", "dnsmasq"], capture_output=True)


# Cap the installed list: dnsmasq holds every name in RAM and this board has
# 512 MB. The popular source lists sit well under this.
_BLOCKLIST_MAX = 120_000
_BLOCKLIST_SKIP = ("localhost", "localhost.localdomain", "broadcasthost", "0.0.0.0")

_blocklist_lock = threading.Lock()
blocklist_status = {"ok": None, "detail": "not run yet", "at": None, "running": False}


def _blocklist_result(ok, detail):
    blocklist_status.update(ok=ok, detail=detail, at=time.time())
    return {"ok": ok, "detail": detail}


def _blocklist_rules(lines, limit=_BLOCKLIST_MAX):
    """hosts-format lines (str or bytes) -> dnsmasq `address=` rules.

    A generator, so the caller can stream straight from the HTTP response to
    disk without ever holding the whole list in memory. Stops at `limit`."""
    n = 0
    for raw in lines:
        ln = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split()
        dom = parts[1] if len(parts) >= 2 else parts[0]
        if "." not in dom or dom in _BLOCKLIST_SKIP:
            continue
        yield "address=/%s/0.0.0.0\n" % dom
        n += 1
        if n >= limit:
            return


def _refresh_blocklist():
    """Fetch the DNS blocklist -> dnsmasq address= rules -> reload.

    The source list is several MB; it is streamed line by line rather than
    read whole, so the Python process never holds it all in memory. The file
    is built under a temp name and renamed into place, so dnsmasq can never
    read a half-written ruleset. Slow enough that callers should run it off
    the request thread — /admin/dns does.

    Returns a status dict (also kept in `blocklist_status` for the admin page,
    since the refresh now finishes after the HTTP response has gone out)."""
    if MOCK:
        return {"ok": False, "detail": "mock mode"}
    if not _blocklist_lock.acquire(blocking=False):
        return {"ok": False, "detail": "a refresh is already running"}
    blocklist_status["running"] = True
    try:
        if not setting("dns_filter"):
            try:
                open(_BLOCKLIST_PATH, "w").close()
                _reload_dnsmasq()
            except Exception as e:
                return _blocklist_result(False, str(e)[:200])
            return _blocklist_result(True, "filtering off — cleared")
        import urllib.request
        tmp = _BLOCKLIST_PATH + ".tmp"
        n = 0
        try:
            with urllib.request.urlopen(setting("dns_blocklist_url"), timeout=30) as r, \
                    open(tmp, "w") as out:
                for rule in _blocklist_rules(r):   # streamed, never held whole
                    out.write(rule)
                    n += 1
            os.replace(tmp, _BLOCKLIST_PATH)       # atomic swap
            _reload_dnsmasq()
            app.logger.info("dns blocklist installed: %d domains", n)
            return _blocklist_result(True, f"{n:,} domains")
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            app.logger.warning("blocklist refresh failed: %s", e)
            return _blocklist_result(False, str(e)[:200])
    finally:
        blocklist_status["running"] = False
        _blocklist_lock.release()


# ---------- reconcile: align kernel state with the DB ----------
# Paid access expires *silently* in the kernel (nft set timeout), so a periodic
# pass re-applies QoS on IP change, clears caps for expired clients, and
# refreshes the walled set.

_last_prune = 0.0


def _reconcile_once():
    global _last_prune
    shaper.configure(setting("gaming_priority"))
    wanted = {}
    for s in db.active_sessions():
        if s["paused_remaining"] is not None:
            continue
        ip = _ip_for_mac(s["mac"])
        if ip:
            wanted[format(shaper._minor(ip), "x")] = (s["mac"], s["speed"])
    for mac, speed in wanted.values():
        _apply_speed(mac, speed)
    for minor_hex in shaper.active_ips():
        if minor_hex not in wanted:
            shaper.unlimit_minor(minor_hex)
    _refresh_walled()
    if setting("pppoe_enabled"):
        pppoe.enforce(db)   # expire overdue plans + keep chap-secrets in sync
    # keep the audit log bounded so it can't grow for the life of the SD card
    now = time.time()
    if now - _last_prune > 3600:
        _last_prune = now
        db.prune_audit()


def reconcile():
    while True:
        time.sleep(max(10, int(setting("reconcile_interval_s") or 20)))
        if MOCK:
            continue
        try:
            _reconcile_once()
        except Exception as e:
            app.logger.warning("reconcile: %s", e)


# ---------- boot: resync kernel state with DB after a restart ----------

def resync():
    if MOCK:
        return
    shaper.configure(setting("gaming_priority"))
    shaper.setup(lan_dev())
    firewall.load_device_sets(db.list_devices())
    _refresh_walled()
    now = time.time()
    for s in db.active_sessions():
        if s["paused_remaining"] is None and s["expires_at"] > now:
            firewall.allow(s["mac"], s["expires_at"] - now)
            _apply_speed(s["mac"], s["speed"])


def _watchdog_settings():
    return {k: setting(k) for k in (
        "watchdog_enabled", "watchdog_interval_s", "watchdog_wan_host",
        "watchdog_services", "watchdog_reboot_after", "watchdog_reboot")}


watch = Watchdog(_watchdog_settings, app.logger)   # started in __main__; .last shown on admin
scheduler = Scheduler(
    lambda: {"schedules": setting("schedules")}, db, app.logger,
    actions={"report": reporter.send_now, "blocklist_refresh": _refresh_blocklist})


if __name__ == "__main__":
    resync()
    reporter.start()
    watch.start()
    scheduler.start()
    threading.Thread(target=reconcile, daemon=True).start()
    port = CFG["portal_port"]
    try:
        from waitress import serve   # production WSGI server (real concurrency)
        app.logger.info("serving on :%d via waitress", port)
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        # Never fail quietly into the dev server on a machine taking money —
        # it stalls the portal under real client load. Say so on every boot.
        app.logger.error(
            "waitress is NOT installed — falling back to the Flask development "
            "server. This is not fit for a live site; install it with: "
            "pip3 install --break-system-packages waitress")
        app.run(host="0.0.0.0", port=port, threaded=True)
