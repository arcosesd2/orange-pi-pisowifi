"""Health watchdog — keep the hotspot self-healing without a technician on site.

Daemon thread. Each interval it pings the WAN and checks that the configured
services are active; on failure it logs and restarts the failed unit, and after
a run of consecutive WAN failures it can reboot (opt-in, default off). The last
result is exposed on `.last` for the admin page. Stdlib only.

All behavior is settings-driven (watchdog_enabled/interval_s/wan_host/services/
reboot_after/reboot) so it can be tuned from the admin UI, and it stays a no-op
until explicitly enabled.
"""
import subprocess
import threading
import time


class Watchdog(threading.Thread):
    def __init__(self, get_settings, logger):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.log = logger
        self.last = {"at": None, "wan": None, "services": {},
                     "wan_fails": 0, "actions": []}
        self._wan_fails = 0

    def run(self):
        time.sleep(20)  # let the network come up first
        while True:
            cfg = self.get_settings()
            interval = max(30, int(cfg.get("watchdog_interval_s") or 60))
            if cfg.get("watchdog_enabled"):
                try:
                    self._check(cfg)
                except Exception as e:
                    self.log.warning("watchdog error: %s", e)
            time.sleep(interval)

    # ---- probes ----

    def _ping(self, host):
        return subprocess.run(
            ["ping", "-c", "1", "-W", "2", host], capture_output=True
        ).returncode == 0

    def _svc_active(self, name):
        return subprocess.run(
            ["systemctl", "is-active", name], capture_output=True, text=True
        ).stdout.strip() == "active"

    def _restart(self, name):
        subprocess.run(["systemctl", "restart", name], capture_output=True)

    # ---- one pass ----

    def _check(self, cfg):
        actions = []
        host = cfg.get("watchdog_wan_host") or "8.8.8.8"
        wan_ok = self._ping(host)
        if wan_ok:
            self._wan_fails = 0
        else:
            self._wan_fails += 1
            actions.append(f"WAN unreachable (x{self._wan_fails})")

        services = {}
        for svc in (cfg.get("watchdog_services") or ["dnsmasq"]):
            ok = self._svc_active(svc)
            services[svc] = ok
            if not ok:
                self.log.warning("watchdog: %s inactive -> restart", svc)
                self._restart(svc)
                actions.append(f"restarted {svc}")

        reboot_after = int(cfg.get("watchdog_reboot_after") or 0)
        if (reboot_after and self._wan_fails >= reboot_after
                and cfg.get("watchdog_reboot")):
            self.log.error("watchdog: WAN down x%d -> reboot", self._wan_fails)
            actions.append("reboot")
            self.last = {"at": time.time(), "wan": wan_ok, "services": services,
                         "wan_fails": self._wan_fails, "actions": actions}
            subprocess.run(["reboot"])
            return

        self.last = {"at": time.time(), "wan": wan_ok, "services": services,
                     "wan_fails": self._wan_fails, "actions": actions}
