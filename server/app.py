"""PisoWiFi cloud dashboard — receives heartbeats from your machines and
shows earnings from any phone/browser.

Deploy anywhere that runs Python (VPS, Render, Railway, PythonAnywhere, or a
spare PC at home). The machines PUSH reports to this server, so it works even
though the machines themselves sit behind CGNAT with no public IP.

    export PISO_KEY="pick-a-long-random-secret"   # same key on every machine
    export DASH_PASSWORD="your-viewing-password"  # optional but recommended
    pip install flask
    python3 app.py                                # listens on :8000 (env PORT)

Then on each machine's admin page → Remote monitoring:
    Report URL = https://your-host/api/heartbeat
    Shared key = the same PISO_KEY

Security: every report is authenticated (HMAC-SHA256 of the body with
PISO_KEY) — forged posts are rejected. The dashboard is read-only.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time

from flask import Flask, abort, jsonify, redirect, render_template, request, session

PISO_KEY = os.environ.get("PISO_KEY", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
DB_PATH = os.environ.get("PISO_DB", os.path.join(os.path.dirname(__file__), "dashboard.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", hashlib.sha256(
    (PISO_KEY + DASH_PASSWORD).encode()).hexdigest())

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT,
    last_seen REAL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    device TEXT NOT NULL,
    ts REAL NOT NULL,
    today_pesos INTEGER,
    all_time_pesos INTEGER,
    active_clients INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(device, ts);
"""


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


# ---------- receiving ----------

@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    if not PISO_KEY:
        return jsonify(error="server has no PISO_KEY set"), 500
    body = request.get_data()
    sig = request.headers.get("X-Piso-Signature", "")
    want = hmac.new(PISO_KEY.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        abort(403)
    p = json.loads(body)
    dev = str(p.get("device_id") or request.headers.get("X-Piso-Device") or "unknown")
    now = time.time()
    with db() as c:
        c.execute(
            "INSERT INTO devices(id,name,last_seen,payload) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "last_seen=excluded.last_seen, payload=excluded.payload",
            (dev, p.get("name") or dev, now, body.decode()),
        )
        sales = p.get("sales") or {}
        c.execute(
            "INSERT INTO snapshots(device,ts,today_pesos,all_time_pesos,active_clients) "
            "VALUES(?,?,?,?,?)",
            (dev, now,
             (sales.get("today") or {}).get("pesos", 0),
             (sales.get("all_time") or {}).get("pesos", 0),
             p.get("active_clients", 0)),
        )
        # keep 90 days of snapshots
        c.execute("DELETE FROM snapshots WHERE ts < ?", (now - 90 * 86400,))
    return jsonify(ok=True)


# ---------- viewing ----------

def viewer_ok():
    # DASH_PASSWORD is REQUIRED. It used to be optional, and `not DASH_PASSWORD`
    # then made every visitor a viewer -- so an unconfigured dashboard published
    # your takings, machine list and uptime to anyone who found the URL. This is
    # the only internet-facing part of the system, so it fails closed instead.
    return bool(DASH_PASSWORD) and session.get("viewer") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASH_PASSWORD:
            session["viewer"] = True
            return redirect("/")
        return render_template("dashboard.html", login=True, error="Wrong password")
    return render_template("dashboard.html", login=True, error=None)


@app.route("/")
def index():
    if not viewer_ok():
        return redirect("/login")
    return render_template("dashboard.html", login=False, error=None)


@app.route("/api/devices")
def api_devices():
    if not viewer_ok():
        return jsonify(error="login required"), 401
    now = time.time()
    out = []
    with db() as c:
        for d in c.execute("SELECT * FROM devices ORDER BY name").fetchall():
            p = json.loads(d["payload"])
            interval = 300
            # daily earnings, last 14 days: max of each day's running "today" total
            days = c.execute(
                "SELECT date(ts,'unixepoch','localtime') day, MAX(today_pesos) pesos "
                "FROM snapshots WHERE device=? AND ts>? GROUP BY day ORDER BY day",
                (d["id"], now - 14 * 86400),
            ).fetchall()
            out.append({
                "id": d["id"], "name": d["name"],
                "last_seen_s": int(now - d["last_seen"]),
                "online": now - d["last_seen"] < 2.5 * interval,
                "sales": p.get("sales", {}),
                "active_clients": p.get("active_clients", 0),
                "sys": p.get("sys", {}),
                "days": [{"day": r["day"], "pesos": r["pesos"] or 0} for r in days],
            })
    return jsonify(devices=out, ts=now)


if __name__ == "__main__":
    # Refuse to start misconfigured rather than serving a silently open
    # dashboard. Both secrets are load-bearing: PISO_KEY authenticates the
    # machines pushing reports in, DASH_PASSWORD gates who may read them.
    missing = [n for n, v in (("PISO_KEY", PISO_KEY),
                              ("DASH_PASSWORD", DASH_PASSWORD)) if not v]
    if missing:
        raise SystemExit(
            "refusing to start: %s not set.\n"
            "  export PISO_KEY='a-long-random-secret'      # shared with every machine\n"
            "  export DASH_PASSWORD='your-viewing-password'\n"
            "Without DASH_PASSWORD the dashboard would be readable by anyone who\n"
            "finds the URL, and it shows your earnings." % " and ".join(missing))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
