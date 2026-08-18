"""Regression tests for the resource limits added after the v2.4 review:

  * SSE streams are capped so they can't starve waitress's worker threads
  * the DNS blocklist parse streams, skips junk and honours its cap
  * downloaded backups don't carry fleet secrets

Run on a dev PC (mock mode). Restores any setting it touches.

    python tests/test_api_limits.py
"""
import os
import sys

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

_prev = {k: main.db.get_setting(k) for k in ("sse_enabled", "sse_max_clients", "remote_key")}

try:
    c = main.app.test_client()

    # ---- blocklist parsing (pure, no network) ----
    src = [
        "# a comment",
        "",
        "0.0.0.0 ads.example.com",
        "127.0.0.1 localhost",              # skipped: not a real target
        "0.0.0.0 broadcasthost",            # skipped
        "0.0.0.0 tracker.example.net",
        "junkwithoutadot",                  # skipped: no dot
        b"0.0.0.0 bytes.example.org",       # bytes lines are fine too
    ]
    rules = list(main._blocklist_rules(src))
    check("only real domains kept", len(rules), 3)
    check("rule format", rules[0], "address=/ads.example.com/0.0.0.0\n")
    check("bytes line decoded", rules[2], "address=/bytes.example.org/0.0.0.0\n")
    check("cap is honoured", len(list(main._blocklist_rules(src, limit=2))), 2)
    check("it is a generator, not a list",
          type(main._blocklist_rules(src)).__name__, "generator")

    # ---- SSE cap ----
    main.db.set_setting("sse_enabled", True)
    main.db.set_setting("sse_max_clients", 2)

    check("stream refused when disabled after toggle off",
          (main.db.set_setting("sse_enabled", False),
           c.get("/api/stream").status_code)[1], 404)
    main.db.set_setting("sse_enabled", True)

    # a one-shot read must work and must NOT hold a slot
    main._sse_open = 0
    r = c.get("/api/stream?once=1")
    check("one-shot stream ok", r.status_code, 200)
    check("one-shot returns an event", r.get_data(as_text=True).startswith("data: "), True)
    check("one-shot holds no slot", main._sse_open, 0)

    # at the cap, further streams are refused rather than eating worker threads
    main._sse_open = 2
    r = c.get("/api/stream")
    check("refused at cap", r.status_code, 503)
    check("refusal explains itself", "polling" in r.get_json()["error"], True)

    # one-shot reads still work at the cap (they don't occupy a thread)
    check("one-shot still ok at cap", c.get("/api/stream?once=1").status_code, 200)
    main._sse_open = 0

    # ---- backup secrecy ----
    main.db.set_setting("remote_key", "super-secret-fleet-key")
    public = main.db.export_config()
    local = main.db.export_config(include_secrets=True)
    check("remote_key withheld from download", "remote_key" in public["settings"], False)
    check("remote_key kept for on-box backup", local["settings"].get("remote_key"),
          "super-secret-fleet-key")
    check("session key never exported", "SECRET_KEY" in public["settings"], False)
    check("session key never exported, even locally",
          "SECRET_KEY" in local["settings"], False)
    check("backup still usable (settings present)",
          "minutes_per_peso" in public["settings"] or len(public["settings"]) > 0, True)
finally:
    main._sse_open = 0
    for k, v in _prev.items():
        if v is None:
            with main.db._conn() as conn:
                conn.execute("DELETE FROM settings WHERE key=?", (k,))
        else:
            main.db.set_setting(k, v)

print("\n" + ("ALL PASSED" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
