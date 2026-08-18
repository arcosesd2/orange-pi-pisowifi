"""Remote monitoring: push earnings/status heartbeats to your website.

Philippine home connections are almost always behind CGNAT, so the machine
can't accept inbound connections from the internet — instead it PUSHES a
signed JSON heartbeat to a URL you control (see server/ for a ready-made
receiver + phone dashboard). Works through any NAT/firewall since it's just
an outbound HTTPS POST.

Every payload is authenticated with HMAC-SHA256 over the raw body using the
shared `remote_key`, sent in the X-Piso-Signature header, so the receiver
can reject forged reports.

Stdlib only (urllib) — nothing extra to install on the board.
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.request


class RemoteReporter(threading.Thread):
    def __init__(self, get_settings, build_payload, logger):
        """get_settings() -> dict with remote_enabled/url/key/interval_s/device_id.
        build_payload() -> JSON-able dict of stats to report."""
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.build_payload = build_payload
        self.log = logger
        self.last = {"ok": None, "detail": "never sent", "at": None}
        self._wake = threading.Event()

    # ---- public ----

    def send_now(self):
        """Synchronous one-shot push (admin 'Send now' button)."""
        return self._push()

    def poke(self):
        """Re-read settings immediately (after admin saves them)."""
        self._wake.set()

    # ---- internals ----

    def run(self):
        time.sleep(10)  # let the app finish booting
        while True:
            cfg = self.get_settings()
            if cfg.get("remote_enabled") and cfg.get("remote_url"):
                self._push()
            interval = max(60, int(cfg.get("remote_interval_s") or 300))
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _push(self):
        cfg = self.get_settings()
        url = (cfg.get("remote_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self.last = {"ok": False, "detail": "no valid remote URL configured",
                         "at": time.time()}
            return self.last
        try:
            body = json.dumps(self.build_payload()).encode()
            sig = hmac.new((cfg.get("remote_key") or "").encode(),
                           body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Piso-Device": cfg.get("device_id") or "pisowifi",
                    "X-Piso-Signature": sig,
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.last = {"ok": True, "detail": f"HTTP {resp.status}",
                             "at": time.time()}
        except Exception as e:
            self.last = {"ok": False, "detail": str(e)[:200], "at": time.time()}
            self.log.warning("remote push failed: %s", self.last["detail"])
        return self.last
