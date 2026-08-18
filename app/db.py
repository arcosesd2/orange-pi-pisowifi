"""SQLite persistence: sales log, sessions, runtime-editable settings."""
import json
import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    pesos INTEGER NOT NULL,
    minutes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    mac TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    paused_remaining REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vouchers (
    code TEXT PRIMARY KEY,
    minutes INTEGER NOT NULL,
    speed TEXT,
    price INTEGER,
    batch TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    used_at REAL,
    used_by_mac TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- 'white' (always-on free) | 'black' (banned)
    label TEXT,
    speed TEXT,                  -- optional QoS profile for whitelisted devices
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS trials (
    mac TEXT PRIMARY KEY,
    last_at REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wallets (
    mac TEXT PRIMARY KEY,
    seconds INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pppoe_accounts (
    user TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    plan_days INTEGER,
    expires_at REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    speed TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_created  ON sales(created_at);
CREATE INDEX IF NOT EXISTS idx_sales_mac      ON sales(mac);
CREATE INDEX IF NOT EXISTS idx_sessions_exp   ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_vouchers_batch ON vouchers(batch);
"""


class Db:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # migration: remember each session's purchased speed profile so the
            # reconcile loop can re-apply the right cap after an IP change/restart
            cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)")]
            if "speed" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN speed TEXT")
            # migration: remember the speed profile banked time was bought at,
            # so redeeming wallet credit doesn't silently downgrade the client
            wcols = [r[1] for r in c.execute("PRAGMA table_info(wallets)")]
            if "speed" not in wcols:
                c.execute("ALTER TABLE wallets ADD COLUMN speed TEXT")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        # WAL lets the reconcile/scheduler threads read while the web threads
        # write without blocking; busy_timeout avoids "database is locked".
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    # -- settings (override config.json defaults, editable from admin) --

    def get_setting(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # -- sessions --

    def get_session(self, mac):
        with self._conn() as c:
            return c.execute("SELECT * FROM sessions WHERE mac=?", (mac,)).fetchone()

    def set_session(self, mac, expires_at, paused_remaining=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions(mac,expires_at,paused_remaining) VALUES(?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET expires_at=excluded.expires_at, "
                "paused_remaining=excluded.paused_remaining",
                (mac, expires_at, paused_remaining),
            )

    def set_session_speed(self, mac, speed):
        with self._conn() as c:
            c.execute("UPDATE sessions SET speed=? WHERE mac=?", (speed, mac))

    def delete_session(self, mac):
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE mac=?", (mac,))

    def active_sessions(self):
        now = time.time()
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sessions WHERE expires_at > ? OR paused_remaining IS NOT NULL "
                "ORDER BY expires_at DESC",
                (now,),
            ).fetchall()

    # -- sales --

    def record_sale(self, mac, pesos, minutes):
        with self._conn() as c:
            c.execute(
                "INSERT INTO sales(mac,pesos,minutes,created_at) VALUES(?,?,?,?)",
                (mac, pesos, minutes, time.time()),
            )

    def sales_summary(self):
        now = time.time()
        day = 86400
        with self._conn() as c:
            def total(since):
                row = c.execute(
                    "SELECT COALESCE(SUM(pesos),0) t, COUNT(*) n FROM sales WHERE created_at>=?",
                    (since,),
                ).fetchone()
                return {"pesos": row["t"], "count": row["n"]}

            recent = [dict(r) for r in c.execute(
                "SELECT * FROM sales ORDER BY created_at DESC LIMIT 25"
            ).fetchall()]
        for r in recent:
            r["when"] = time.strftime("%b %d %H:%M", time.localtime(r["created_at"]))
        return {
            "today": total(now - now % day),
            "week": total(now - 7 * day),
            "all_time": total(0),
            "recent": recent,
        }

    def daily_sales(self, days=30):
        """[(YYYY-MM-DD, pesos, count)] for the last `days`, oldest first.
        Uses localtime day boundaries so it lines up with the sales_summary."""
        since = time.time() - days * 86400
        with self._conn() as c:
            rows = c.execute(
                "SELECT created_at, pesos FROM sales WHERE created_at>=?", (since,)
            ).fetchall()
        buckets = {}
        for r in rows:
            d = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            b = buckets.setdefault(d, {"pesos": 0, "count": 0})
            b["pesos"] += r["pesos"]
            b["count"] += 1
        out = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            b = buckets.get(d, {"pesos": 0, "count": 0})
            out.append((d, b["pesos"], b["count"]))
        return out

    def iter_sales(self):
        """Stream every sale (for CSV export), oldest first."""
        with self._conn() as c:
            for r in c.execute("SELECT * FROM sales ORDER BY created_at"):
                yield dict(r)

    # -- vouchers --

    def create_voucher(self, code, minutes, speed=None, price=None,
                       batch=None, expires_at=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO vouchers(code,minutes,speed,price,batch,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, int(minutes), speed, price, batch, time.time(), expires_at),
            )

    def get_voucher(self, code):
        with self._conn() as c:
            return c.execute("SELECT * FROM vouchers WHERE code=?", (code,)).fetchone()

    def redeem_voucher(self, code, mac):
        """Atomically claim an unused, unexpired voucher. Returns the row on
        success (with minutes/speed/price), or None if invalid/used/expired."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE vouchers SET used_at=?, used_by_mac=? "
                "WHERE code=? AND used_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>?)",
                (now, mac, code, now),
            )
            if cur.rowcount != 1:
                return None
            return c.execute("SELECT * FROM vouchers WHERE code=?", (code,)).fetchone()

    def revoke_voucher(self, code):
        """Mark an unused voucher as void (used, no mac)."""
        with self._conn() as c:
            c.execute(
                "UPDATE vouchers SET used_at=?, used_by_mac='(revoked)' "
                "WHERE code=? AND used_at IS NULL",
                (time.time(), code),
            )

    def list_vouchers(self, status="all", limit=500):
        where = {"unused": "used_at IS NULL", "used": "used_at IS NOT NULL"}.get(status)
        sql = "SELECT * FROM vouchers"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, (limit,)).fetchall()]

    def voucher_stats(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) total, "
                "COALESCE(SUM(used_at IS NULL),0) unused, "
                "COALESCE(SUM(used_at IS NOT NULL),0) used FROM vouchers"
            ).fetchone()
        return {"total": row["total"], "unused": row["unused"], "used": row["used"]}

    # -- devices (whitelist / blacklist) --

    def set_device(self, mac, kind, label=None, speed=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO devices(mac,kind,label,speed,added_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET kind=excluded.kind, "
                "label=excluded.label, speed=excluded.speed",
                (mac, kind, label, speed, time.time()),
            )

    def delete_device(self, mac):
        with self._conn() as c:
            c.execute("DELETE FROM devices WHERE mac=?", (mac,))

    def list_devices(self, kind=None):
        sql = "SELECT * FROM devices"
        args = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        sql += " ORDER BY added_at DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # -- config backup / restore --

    def export_config(self, include_sales=False):
        """Portable snapshot for cloning machines (settings + vouchers + devices,
        optionally the sales ledger). SECRET_KEY is intentionally excluded."""
        with self._conn() as c:
            settings = {r["key"]: json.loads(r["value"])
                        for r in c.execute("SELECT key,value FROM settings")
                        if r["key"] != "SECRET_KEY"}
            vouchers = [dict(r) for r in c.execute("SELECT * FROM vouchers")]
            devices = [dict(r) for r in c.execute("SELECT * FROM devices")]
            pppoe = [dict(r) for r in c.execute("SELECT * FROM pppoe_accounts")]
            data = {"version": 1, "settings": settings, "vouchers": vouchers,
                    "devices": devices, "pppoe_accounts": pppoe}
            if include_sales:
                data["sales"] = [dict(r) for r in c.execute("SELECT * FROM sales")]
        return data

    def import_config(self, data):
        """Restore an export_config() snapshot (merge; codes/macs upserted)."""
        with self._conn() as c:
            for k, v in (data.get("settings") or {}).items():
                if k == "SECRET_KEY":
                    continue
                c.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
            for v in (data.get("vouchers") or []):
                c.execute(
                    "INSERT OR REPLACE INTO vouchers"
                    "(code,minutes,speed,price,batch,created_at,expires_at,used_at,used_by_mac) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (v.get("code"), v.get("minutes"), v.get("speed"), v.get("price"),
                     v.get("batch"), v.get("created_at") or time.time(),
                     v.get("expires_at"), v.get("used_at"), v.get("used_by_mac")),
                )
            for d in (data.get("devices") or []):
                c.execute(
                    "INSERT OR REPLACE INTO devices(mac,kind,label,speed,added_at) "
                    "VALUES(?,?,?,?,?)",
                    (d.get("mac"), d.get("kind"), d.get("label"),
                     d.get("speed"), d.get("added_at") or time.time()),
                )
            for p in (data.get("pppoe_accounts") or []):
                c.execute(
                    "INSERT OR REPLACE INTO pppoe_accounts"
                    "(user,password,plan_days,expires_at,enabled,speed,note,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (p.get("user"), p.get("password"), p.get("plan_days"),
                     p.get("expires_at"), p.get("enabled", 1), p.get("speed"),
                     p.get("note"), p.get("created_at") or time.time()),
                )

    # -- persistent Flask secret key (survives restarts) --

    def secret_key(self):
        import secrets
        key = self.get_setting("SECRET_KEY")
        if not key:
            key = secrets.token_hex(32)
            self.set_setting("SECRET_KEY", key)
        return key

    # -- admin password (PBKDF2, replaces the old plaintext compare) --

    @staticmethod
    def _hash_pw(password, salt=None, iters=200_000):
        import hashlib
        salt = salt or os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
        return f"pbkdf2${iters}${salt.hex()}${dk.hex()}"

    def set_admin_password(self, password):
        self.set_setting("admin_pw_hash", self._hash_pw(password))

    def verify_admin(self, password, legacy_plaintext=None):
        """Check the admin password. On first run (no hash yet) accept the
        legacy plaintext (config.json / old settings value) once and upgrade
        it to a hash. Constant-time compare."""
        import hashlib
        import hmac as _hmac
        stored = self.get_setting("admin_pw_hash")
        if not stored:
            if legacy_plaintext is not None and _hmac.compare_digest(password, legacy_plaintext):
                self.set_admin_password(password)
                return True
            return False
        try:
            _, iters, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
            return _hmac.compare_digest(dk.hex(), dk_hex)
        except Exception:
            return False

    # -- audit log --

    def log_audit(self, action, detail=None, ip=None):
        with self._conn() as c:
            c.execute("INSERT INTO audit(at,ip,action,detail) VALUES(?,?,?,?)",
                      (time.time(), ip, action, detail))

    def list_audit(self, limit=200):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def prune_audit(self, keep=5000):
        """Trim the audit log to the newest `keep` rows. The sales ledger is
        deliberately never pruned — it's the owner's revenue record — but the
        audit log is diagnostic and would otherwise grow for the life of the
        SD card. Returns how many rows were removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM audit WHERE id NOT IN "
                "(SELECT id FROM audit ORDER BY id DESC LIMIT ?)", (keep,))
            return cur.rowcount

    # -- free trial (once per period per MAC) --

    def get_trial(self, mac):
        with self._conn() as c:
            return c.execute("SELECT * FROM trials WHERE mac=?", (mac,)).fetchone()

    def mark_trial(self, mac):
        with self._conn() as c:
            c.execute(
                "INSERT INTO trials(mac,last_at,count) VALUES(?,?,1) "
                "ON CONFLICT(mac) DO UPDATE SET last_at=excluded.last_at, count=count+1",
                (mac, time.time()))

    # -- wallet / credit carry-over --

    def wallet_balance(self, mac):
        with self._conn() as c:
            r = c.execute("SELECT seconds FROM wallets WHERE mac=?", (mac,)).fetchone()
        return r["seconds"] if r else 0

    def wallet_speed(self, mac):
        """Speed profile the banked time was bought at (None -> default)."""
        with self._conn() as c:
            r = c.execute("SELECT speed FROM wallets WHERE mac=?", (mac,)).fetchone()
        return r["speed"] if r else None

    def wallet_add(self, mac, seconds, speed=None):
        seconds = int(seconds)
        if seconds <= 0:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO wallets(mac,seconds,speed,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET seconds=seconds+excluded.seconds, "
                "speed=COALESCE(excluded.speed, wallets.speed), "
                "updated_at=excluded.updated_at",
                (mac, seconds, speed, time.time()))

    def wallet_take(self, mac, seconds=None):
        """Deduct up to `seconds` (or the whole balance) from the wallet.
        Returns the number of seconds actually taken."""
        with self._conn() as c:
            r = c.execute("SELECT seconds FROM wallets WHERE mac=?", (mac,)).fetchone()
            bal = r["seconds"] if r else 0
            take = bal if seconds is None else min(bal, int(seconds))
            if take > 0:
                c.execute("UPDATE wallets SET seconds=seconds-?, updated_at=? WHERE mac=?",
                          (take, time.time(), mac))
        return take

    # -- pppoe accounts --

    def set_pppoe_account(self, user, password, plan_days=None, speed=None, note=None):
        exp = time.time() + int(plan_days) * 86400 if plan_days else None
        with self._conn() as c:
            c.execute(
                "INSERT INTO pppoe_accounts"
                "(user,password,plan_days,expires_at,enabled,speed,note,created_at) "
                "VALUES(?,?,?,?,1,?,?,?) "
                "ON CONFLICT(user) DO UPDATE SET password=excluded.password, "
                "plan_days=excluded.plan_days, expires_at=excluded.expires_at, "
                "speed=excluded.speed, note=excluded.note",
                (user, password, plan_days, exp, speed, note, time.time()))

    def delete_pppoe_account(self, user):
        with self._conn() as c:
            c.execute("DELETE FROM pppoe_accounts WHERE user=?", (user,))

    def set_pppoe_enabled(self, user, enabled):
        with self._conn() as c:
            c.execute("UPDATE pppoe_accounts SET enabled=? WHERE user=?",
                      (1 if enabled else 0, user))

    def list_pppoe_accounts(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM pppoe_accounts ORDER BY user").fetchall()]

    def expire_pppoe_accounts(self):
        """Disable accounts past their expiry. Returns how many were changed."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE pppoe_accounts SET enabled=0 WHERE enabled=1 "
                "AND expires_at IS NOT NULL AND expires_at<?", (time.time(),))
            return cur.rowcount
