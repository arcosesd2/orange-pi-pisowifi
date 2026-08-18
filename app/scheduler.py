"""Scheduled tasks — cron-like jobs without a technician on site.

Daemon thread (same shape as watchdog/RemoteReporter). Reads the `schedules`
setting: a list of jobs {type, time "HH:MM", days [0-6, Mon=0; empty=every day],
...args}. Supported job types:
  - reboot           : reboot the board
  - set_rate         : set minutes_per_peso to `value`
  - backup           : write a config backup JSON under <db dir>/backups/
  - blocklist_refresh: refresh the DNS ad/malware blocklist (main supplies this)
  - report           : send a remote heartbeat now (main supplies this)

Fires at most once per job per minute. Stdlib only.
"""
import json
import os
import subprocess
import threading
import time


class Scheduler(threading.Thread):
    def __init__(self, get_settings, db, logger, actions=None):
        super().__init__(daemon=True)
        self.get_settings = get_settings
        self.db = db
        self.log = logger
        self.actions = actions or {}
        self._last = {}   # job-key -> "YYYY-MM-DD HH:MM" it last fired

    def run(self):
        time.sleep(25)
        while True:
            try:
                self._tick()
            except Exception as e:
                self.log.warning("scheduler error: %s", e)
            time.sleep(20)

    def _tick(self, now=None):
        now = now or time.localtime()
        hm = time.strftime("%H:%M", now)
        stamp = time.strftime("%Y-%m-%d %H:%M", now)
        for i, job in enumerate(self.get_settings().get("schedules") or []):
            if not isinstance(job, dict) or job.get("time") != hm:
                continue
            days = job.get("days") or []
            if days and now.tm_wday not in days:
                continue
            key = f"{i}:{job.get('type')}"
            if self._last.get(key) == stamp:
                continue
            self._last[key] = stamp
            self.log.info("scheduler: running %s", job.get("type"))
            try:
                self._run_job(job)
            except Exception as e:
                self.log.warning("scheduler job %s failed: %s", job.get("type"), e)

    def _run_job(self, job):
        t = job.get("type")
        if t == "reboot":
            subprocess.run(["reboot"])
        elif t == "set_rate":
            self.db.set_setting("minutes_per_peso", int(job.get("value", 20)))
        elif t == "backup":
            d = os.path.join(os.path.dirname(self.db.path) or ".", "backups")
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, time.strftime("backup-%Y%m%d-%H%M.json"))
            with open(fn, "w") as f:
                json.dump(self.db.export_config(include_sales=True), f)
        elif t in self.actions:
            self.actions[t]()
        else:
            self.log.warning("scheduler: unknown job type %r", t)
