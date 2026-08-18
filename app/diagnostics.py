"""GPIO diagnostics: per-pin live testing + all-pin noise scan for the admin
Diagnostics page.

Knows the Orange Pi One 26-pin header layout (physical/BOARD numbering, the
same numbering used everywhere else in this project). Any non-reserved GPIO
pin can be:

  * watched  — set as input (optional pull), live level shown, and an edge
               counter counts transitions between polls;
  * driven   — set as output high/low (e.g. to click a spare relay);
  * released — returned to a safe input state.

There is also a noise scan: one toggle listens on EVERY free GPIO pin at once
and records per-pin activity, used to find which pin a signal (coin pulse,
button, chattering wire) actually arrives on.

H3 hardware fact that shapes all of this: only port-A (PA*) pins have
external-interrupt lines — PC*/PD14 pins cannot do kernel edge detection at
all (OPi.GPIO's event thread just crashes on them). So edge counting uses
real interrupts on PA pins and falls back to a ~100 Hz polling sampler on the
others: fine for coin pulses (30–100 ms) and wiring checks, only sub-10 ms
glitches can slip through. The coin acceptor itself must be on a PA pin —
`EINT_PINS` is exported so the app can enforce that.

Pins in use by the coin slot / relay are reported as reserved — they are
monitored through the CoinSlot itself, not here, so the two never fight
over the same line.

NOTE: the sysfs backend used by OPi.GPIO cannot program internal pull
resistors on the H3; we try anyway and report `pull_applied` honestly so
the UI can tell the user an external resistor is needed.

Mock mode (no OPi.GPIO, i.e. developing on a PC): watched inputs read as
their pull state, outputs read back what you drove, and the noise scan
produces demo activity on pins 13 and 16.
"""
import collections
import random
import threading
import time

try:
    import orangepi.one
    from OPi import GPIO
    HAVE_GPIO = True
except ImportError:
    GPIO = None
    HAVE_GPIO = False

# Orange Pi One 26-pin header: physical pin -> (name, kind, has_interrupt)
# has_interrupt: H3 routes external interrupts only to PA pins (PA_EINT).
HEADER = {
    1: ("3.3V", "power", False),  2: ("5V", "power", False),
    3: ("PA12", "gpio", True),    4: ("5V", "power", False),
    5: ("PA11", "gpio", True),    6: ("GND", "ground", False),
    7: ("PA6", "gpio", True),     8: ("PA13", "gpio", True),
    9: ("GND", "ground", False), 10: ("PA14", "gpio", True),
    11: ("PA1", "gpio", True),   12: ("PD14", "gpio", False),
    13: ("PA0", "gpio", True),   14: ("GND", "ground", False),
    15: ("PA3", "gpio", True),   16: ("PC4", "gpio", False),
    17: ("3.3V", "power", False), 18: ("PC7", "gpio", False),
    19: ("PC0", "gpio", False),  20: ("GND", "ground", False),
    21: ("PC1", "gpio", False),  22: ("PA2", "gpio", True),
    23: ("PC2", "gpio", False),  24: ("PC3", "gpio", False),
    25: ("GND", "ground", False), 26: ("PA21", "gpio", True),
}

# Pins that can host the coin acceptor (interrupt-capable).
EINT_PINS = sorted(p for p, (_, kind, eint) in HEADER.items()
                   if kind == "gpio" and eint)

_PULLS = {"up": "PUD_UP", "down": "PUD_DOWN", "none": "PUD_OFF"}
_SAMPLE_S = 0.01  # polling-sampler period (~100 Hz)


class PinManager:
    def __init__(self, reserved_fn):
        """reserved_fn() -> {pin: "coin"|"relay"} — pins owned by the coin slot."""
        self.mock = not HAVE_GPIO
        self.reserved_fn = reserved_fn
        self._lock = threading.Lock()
        # pin -> {"mode": "watch"|"out", "pull": str, "pull_applied": bool,
        #         "level": int (outputs), "edges": int, "edge_detect": bool}
        self._pins = {}
        # all-pin noise scan: {"active", "started", "stopped", "pins": {pin: rec}}
        self._scan = None
        self._sampler = None  # polling thread for non-interrupt pins
        if not self.mock:
            GPIO.setwarnings(False)
            GPIO.setmode(orangepi.one.BOARD)

    def _check(self, pin):
        pin = int(pin)
        if HEADER.get(pin, ("", "", False))[1] != "gpio":
            raise ValueError(f"pin {pin} is not a GPIO pin")
        if pin in self.reserved_fn():
            raise ValueError(f"pin {pin} is reserved by the coin slot config")
        if self._scan and self._scan["active"]:
            raise ValueError("noise scan is running — stop it before using pins manually")
        return pin

    # ---- manual per-pin testing ----

    def watch(self, pin, pull="none"):
        pin = self._check(pin)
        if pull not in _PULLS:
            raise ValueError("pull must be up/down/none")
        eint = HEADER[pin][2]
        with self._lock:
            self._forget(pin)
            applied = False
            if not self.mock:
                try:
                    GPIO.setup(pin, GPIO.IN,
                               pull_up_down=getattr(GPIO, _PULLS[pull]))
                    applied = pull != "none"
                except Exception:
                    GPIO.setup(pin, GPIO.IN)  # sysfs can't do pulls
                entry = {"mode": "watch", "pull": pull, "pull_applied": applied,
                         "edges": 0, "edge_detect": eint}
                self._pins[pin] = entry
                if eint:
                    try:
                        GPIO.add_event_detect(
                            pin, GPIO.BOTH,
                            callback=lambda ch, p=pin: self._edge(p), bouncetime=5,
                        )
                    except Exception:
                        entry["edge_detect"] = False
                if not entry["edge_detect"]:
                    self._ensure_sampler()  # PC/PD pins: no IRQ line — poll
            else:
                self._pins[pin] = {"mode": "watch", "pull": pull,
                                   "pull_applied": True, "edges": 0,
                                   "edge_detect": True}

    def _edge(self, pin):
        with self._lock:
            if pin in self._pins:
                self._pins[pin]["edges"] += 1

    def drive(self, pin, level):
        pin = self._check(pin)
        level = 1 if int(level) else 0
        with self._lock:
            cur = self._pins.get(pin)
            if not cur or cur["mode"] != "out":
                self._forget(pin)
                if not self.mock:
                    GPIO.setup(pin, GPIO.OUT)
                self._pins[pin] = {"mode": "out", "level": level}
            else:
                cur["level"] = level
            if not self.mock:
                GPIO.output(pin, GPIO.HIGH if level else GPIO.LOW)

    def release(self, pin):
        pin = self._check(pin)
        with self._lock:
            self._forget(pin)

    def force_release(self, pin):
        """Release without reserved-pin checks — used when the hardware config
        hands a previously free (possibly watched) pin to the coin slot."""
        with self._lock:
            self._forget(int(pin))

    def _forget(self, pin):
        if pin not in self._pins:
            return
        if not self.mock:
            had_events = self._pins[pin].get("edge_detect")
            try:
                GPIO.remove_event_detect(pin)
            except Exception:
                pass
            if had_events:
                time.sleep(0.1)  # OPi.GPIO's event thread touches sysfs on exit
            try:
                GPIO.cleanup(pin)
            except Exception:
                pass
        del self._pins[pin]

    def reset_edges(self, pin):
        with self._lock:
            if int(pin) in self._pins:
                self._pins[int(pin)]["edges"] = 0

    def state(self):
        """Full header snapshot for the UI."""
        reserved = self.reserved_fn()
        out = []
        with self._lock:
            for pin, (name, kind, eint) in HEADER.items():
                row = {"pin": pin, "name": name, "kind": kind, "eint": eint}
                if pin in reserved:
                    row["reserved"] = reserved[pin]
                st = self._pins.get(pin)
                if st:
                    row.update(st)
                    if st["mode"] == "watch":
                        row["level"] = self._read(pin, st)
                out.append(row)
        return out

    def _read(self, pin, st):
        if self.mock:
            return 1 if st["pull"] == "up" else 0
        try:
            return int(GPIO.input(pin))
        except Exception:
            return None

    # ---- all-pin noise scan ----

    def scan_start(self):
        with self._lock:
            if self._scan and self._scan["active"]:
                return
            for pin in list(self._pins):   # release all manual watches/outputs
                self._forget(pin)
            reserved = self.reserved_fn()
            recs = {}
            for pin, (name, kind, eint) in HEADER.items():
                if kind != "gpio" or pin in reserved:
                    continue
                recs[pin] = {"edges": 0, "first": None, "last": None,
                             "recent": collections.deque(maxlen=2000),
                             "method": "irq" if (eint or self.mock) else "poll",
                             "error": None}
            self._scan = {"active": True, "started": time.monotonic(),
                          "stopped": None, "pins": recs}
            if not self.mock:
                need_sampler = False
                for pin, rec in recs.items():
                    try:
                        GPIO.setup(pin, GPIO.IN)
                        if rec["method"] == "irq":
                            GPIO.add_event_detect(
                                pin, GPIO.BOTH,
                                callback=lambda ch, p=pin: self._scan_edge(p),
                                bouncetime=2,
                            )
                        else:
                            need_sampler = True
                    except Exception as e:
                        rec["error"] = str(e)[:80]
                if need_sampler:
                    self._ensure_sampler()

    def scan_stop(self):
        with self._lock:
            s = self._scan
            if not s or not s["active"]:
                return
            s["active"] = False
            s["stopped"] = time.monotonic()
            if not self.mock:
                # two-phase: detach every IRQ handler first, give OPi.GPIO's
                # event threads a beat to exit (they write sysfs on the way
                # out), then unexport everything.
                for pin, rec in s["pins"].items():
                    if rec["method"] == "irq" and not rec["error"]:
                        try:
                            GPIO.remove_event_detect(pin)
                        except Exception:
                            pass
                time.sleep(0.3)
                for pin, rec in s["pins"].items():
                    if not rec["error"]:
                        try:
                            GPIO.cleanup(pin)
                        except Exception:
                            pass

    def _scan_edge(self, pin, now=None):
        now = now or time.monotonic()
        with self._lock:
            self._scan_edge_locked(pin, now)

    def _scan_edge_locked(self, pin, now):
        s = self._scan
        if not s or not s["active"]:
            return
        rec = s["pins"].get(pin)
        if not rec:
            return
        rec["edges"] += 1
        rec["first"] = rec["first"] or now
        rec["last"] = now
        rec["recent"].append(now)

    def scan_state(self):
        """Results table; kept after stop until the next scan starts."""
        with self._lock:
            s = self._scan
            if not s:
                return {"active": False}
            now = time.monotonic()
            if s["active"] and self.mock:
                # demo noise so the feature is testable on a dev PC
                for pin, n in ((13, random.randint(0, 3)),
                               (16, 1 if random.random() < 0.15 else 0)):
                    if pin in s["pins"]:
                        for _ in range(n):
                            self._scan_edge_locked(pin, now)
            rows = []
            for pin, rec in s["pins"].items():
                level = None
                if s["active"] and not self.mock and not rec["error"]:
                    try:
                        level = int(GPIO.input(pin))
                    except Exception:
                        pass
                rows.append({
                    "pin": pin, "name": HEADER[pin][0],
                    "edges": rec["edges"], "method": rec["method"],
                    "per_min": sum(1 for t in rec["recent"] if now - t <= 60),
                    "first_ago_s": round(now - rec["first"], 1) if rec["first"] else None,
                    "last_ago_s": round(now - rec["last"], 1) if rec["last"] else None,
                    "level": level, "error": rec["error"],
                })
            return {
                "active": s["active"],
                "duration_s": int((s["stopped"] or now) - s["started"]),
                "rows": rows,
            }

    # ---- polling sampler for pins without an interrupt line (PC*/PD14) ----

    def _ensure_sampler(self):
        # call with lock held; real hardware only
        if self._sampler is None or not self._sampler.is_alive():
            self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
            self._sampler.start()

    def _sample_targets(self):
        # call with lock held -> set of pins needing polled edge counting
        targets = set()
        for pin, st in self._pins.items():
            if st.get("mode") == "watch" and not st.get("edge_detect"):
                targets.add(pin)
        s = self._scan
        if s and s["active"]:
            for pin, rec in s["pins"].items():
                if rec["method"] == "poll" and not rec["error"]:
                    targets.add(pin)
        return targets

    def _sample_loop(self):
        last = {}
        while True:
            with self._lock:
                targets = self._sample_targets()
                if not targets:
                    self._sampler = None
                    return
            for pin in targets:
                try:
                    lvl = int(GPIO.input(pin))
                except Exception:
                    continue
                prev = last.get(pin)
                last[pin] = lvl
                if prev is None or lvl == prev:
                    continue
                now = time.monotonic()
                with self._lock:
                    st = self._pins.get(pin)
                    if st and st.get("mode") == "watch":
                        st["edges"] += 1
                    self._scan_edge_locked(pin, now)
            time.sleep(_SAMPLE_S)
