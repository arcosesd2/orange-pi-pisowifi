"""Coin acceptor driver.

The acceptor (CH-926 class) emits N pulses per coin. Pulses arrive as edges on
`coin_gpio_pin` (through the optocoupler — see README wiring). A pulse train is
considered one coin once `pulse_end_gap_s` passes with no new pulse; the train
size is mapped to pesos via the `denominations` table ({"pulses": pesos}),
falling back to pulses * `pulse_value_pesos` for unmapped counts.

Only one client at a time may hold the "insert window"; while a window is
open the optional relay powers the acceptor. Pulses arriving with no window
open are still counted for diagnostics but never credited.

All hardware settings (pin, edge, debounce, relay, timing, denominations) are
runtime-reconfigurable via reconfigure() — the admin diagnostics page uses it.

Runs in mock mode automatically when OPi.GPIO is unavailable (development
on a PC) — the admin page then shows a "simulate coin" button.

NOTE: OPi.GPIO edge detection relies on the deprecated sysfs GPIO interface;
Armbian still ships it for H3. If a future kernel drops it, port `_setup_gpio`
to python3-libgpiod (gpiod.request_lines with edge detection) — the rest of
this module is hardware-agnostic.
"""
import collections
import datetime
import sys
import threading
import time

from diagnostics import HEADER

try:
    import orangepi.one
    from OPi import GPIO
    HAVE_GPIO = True
except ImportError:
    GPIO = None
    HAVE_GPIO = False

try:
    import gpiod
    from gpiod.line import Bias, Edge
    HAVE_GPIOD = True
except ImportError:
    gpiod = None
    HAVE_GPIOD = False

# cfg keys that require tearing down / re-arming the GPIO when changed
_GPIO_KEYS = ("coin_gpio_pin", "coin_edge", "coin_bounce_ms",
              "relay_gpio_pin", "relay_active_low")

# ---------------------------------------------------------------------------
# Coin pulse input
#
# OPi.GPIO's add_event_detect() arms the *deprecated sysfs* edge interface.
# On this board's kernel (6.18.44-current-sunxi) the kernel accepts the arming
# and then never delivers. Proven on hardware: five clean 50 ms pulses were
# captured on the coin line by level-polling that very pin, while the app held
# an open fd on the same pin with edge=falling -- and the app counted zero. No
# exception, no log line, no restart. Coins simply stopped existing, which is
# the worst failure this machine has: it takes money and gives nothing back.
#
# So the coin line never goes through add_event_detect() again. Two backends,
# in preference order:
#
#   gpiod - libgpiod v2 character device, interrupt driven. The supported
#           interface on this kernel, and the one sysfs was deprecated for.
#   poll  - level polling of the sysfs value file. Needs no packages, and is
#           exactly what did capture the pulses above.
#
# Both count every edge they see, both directions, so the diagnostics page can
# show the input is alive instead of leaving it to be inferred from money not
# arriving. An input that stops working must say so.
# ---------------------------------------------------------------------------

_POLL_S = 0.001          # 1 ms; a coin pulse on this acceptor is ~50 ms wide
_BANKS = "ABCDEFG"       # H3 gpiochip0 exposes PA..PG, 32 lines per bank
_CHIP = "/dev/gpiochip0"


def _line_offset(pin_name):
    """'PA12' -> line offset on gpiochip0 (which is also its sysfs number)."""
    return _BANKS.index(pin_name[1]) * 32 + int(pin_name[2:])


def _unexport(sysfs_n):
    """Drop a stale sysfs export of the coin line.

    A sysfs-exported line is held against the character device, so libgpiod
    gets EBUSY and the machine silently drops to the polling backend for the
    rest of its life. The export outlives the process that made it, so this
    happens on every restart after an unclean exit -- and on the very first
    restart after upgrading from the old add_event_detect() code, which is
    exactly when it matters. Only ever called for the pin this app owns.
    """
    import os
    if not os.path.exists(f"/sys/class/gpio/gpio{sysfs_n}"):
        return
    try:
        with open("/sys/class/gpio/unexport", "w") as f:
            f.write(str(sysfs_n))
        time.sleep(0.05)
    except OSError:
        pass


class _CoinInput:
    """Shared bookkeeping and debounce for both backends.

    `want_rising` selects which edge is credited as a pulse; the other edge is
    still counted, because knowing the line pulses the opposite way to the
    configuration is the difference between "no coin" and "wrong edge".
    """

    name = "?"

    def __init__(self, on_edge, want_rising, bounce_ms):
        self._on_edge = on_edge
        self._want_rising = bool(want_rising)
        self._bounce_s = max(0, int(bounce_ms)) / 1000.0
        self.rising = 0
        self.falling = 0
        self.counted = 0
        self.last_at = None
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def _emit(self, rising):
        if rising:
            self.rising += 1
        else:
            self.falling += 1
        if rising != self._want_rising:
            return
        now = time.monotonic()
        if self.last_at is not None and now - self.last_at < self._bounce_s:
            return                       # contact bounce, not a second coin
        self.last_at = now
        self.counted += 1
        self._on_edge()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def level(self):
        return None

    def _run(self):
        raise NotImplementedError


class _GpiodInput(_CoinInput):
    """libgpiod v2 edge events -- interrupt driven, no polling."""

    name = "gpiod"

    def __init__(self, on_edge, want_rising, pin_name, bounce_ms):
        super().__init__(on_edge, want_rising, bounce_ms)
        self._offset = _line_offset(pin_name)
        # Debounce is left to _emit rather than the kernel: the same rule then
        # applies to both backends, so behaviour does not change with the
        # backend that happens to be available.
        self._req = gpiod.request_lines(
            _CHIP,
            consumer="pisowifi-coin",
            config={self._offset: gpiod.LineSettings(
                edge_detection=Edge.BOTH, bias=Bias.AS_IS)},
        )

    def level(self):
        try:
            from gpiod.line import Value
            return 1 if self._req.get_value(self._offset) == Value.ACTIVE else 0
        except Exception:
            return None

    def _run(self):
        wait = datetime.timedelta(milliseconds=200)
        while not self._stop.is_set():
            try:
                if not self._req.wait_edge_events(wait):
                    continue
                for ev in self._req.read_edge_events():
                    self._emit(ev.event_type == gpiod.EdgeEvent.Type.RISING_EDGE)
            except Exception as e:                      # pragma: no cover
                self.error = f"{type(e).__name__}: {e}"
                print(f"coinslot: gpiod reader died: {self.error}",
                      file=sys.stderr, flush=True)
                return

    def close(self):
        super().close()
        try:
            self._req.release()
        except Exception:
            pass


class _PollInput(_CoinInput):
    """Level polling of the sysfs value file.

    Fallback for when libgpiod is not installed. The pin must already be
    exported as an input by the caller.
    """

    name = "poll"

    def __init__(self, on_edge, want_rising, sysfs_n, bounce_ms):
        super().__init__(on_edge, want_rising, bounce_ms)
        self._fh = open(f"/sys/class/gpio/gpio{sysfs_n}/value")
        self._last = self._read()

    def _read(self):
        self._fh.seek(0)
        return self._fh.read().strip()

    def level(self):
        try:
            return int(self._read())
        except Exception:
            return None

    def _run(self):
        while not self._stop.is_set():
            try:
                v = self._read()
            except Exception as e:                      # pragma: no cover
                self.error = f"{type(e).__name__}: {e}"
                print(f"coinslot: poll reader died: {self.error}",
                      file=sys.stderr, flush=True)
                return
            if v != self._last:
                self._last = v
                self._emit(v == "1")
            time.sleep(_POLL_S)

    def close(self):
        super().close()
        try:
            self._fh.close()
        except Exception:
            pass


class CoinSlot:
    def __init__(self, cfg, on_coin, on_timeout, on_pending=None, pending=(0, 0.0)):
        """on_coin(mac, pesos): fired per completed coin.
        on_timeout(mac, total_pesos): fired if the insert window expires —
        the caller must still credit the money (never swallow coins)."""
        self.cfg = dict(cfg)
        self.on_coin = on_coin
        self.on_timeout = on_timeout
        self.mock = not HAVE_GPIO

        self._lock = threading.Lock()
        self._window_mac = None
        self._window_deadline = 0.0
        self._window_pesos = 0
        self._train = 0           # pulses in the current (unfinished) train
        self._last_pulse = 0.0

        # diagnostics (survive across windows)
        self.total_pulses = 0
        self.pulse_log = collections.deque(maxlen=100)  # monotonic timestamps
        self.last_train = None    # {"pulses": n, "pesos": x, "ago": ts, "credited": bool}
        self.relay_state = False
        # Coins that arrived with no insert window open. Held rather than
        # destroyed; the next customer to open a window claims them, provided
        # they do so within `uncredited_hold_s`.
        # Restored across restarts: a coin held in the pot is real money, and a
        # reboot must not eat it. `pending` is (pesos, wall-clock seconds) as
        # last persisted; the age is carried in wall time because monotonic
        # clocks reset at boot.
        self.on_pending = on_pending
        self.pending_pesos = int(pending[0] or 0)
        self._pending_wall = float(pending[1] or 0.0)
        self.pending_at = time.monotonic() - max(
            0.0, time.time() - self._pending_wall) if self.pending_pesos else 0.0
        self.claimed_pesos = 0    # last amount handed to a window from the pot
        # Running tally since boot, for the admin dashboard. A number that
        # keeps climbing is the signal that the relay is not gating the
        # acceptor -- which is the only defence against a coin being credited
        # to the wrong customer.
        self.unclaimed_total = 0
        self.unclaimed_events = 0

        self.gpio_error = None
        self.coin_input = None
        if not self.mock:
            # The import guard above only catches ImportError. OPi.GPIO can
            # import perfectly well and still fail here against the kernel's
            # actual GPIO layout -- on 6.6+ the sysfs chip base moved (this H3
            # exposes gpiochip0 AND gpiochip352), so exporting a computed pin
            # number raises instead of returning. That must not be fatal: this
            # runs at import time, so an escaping exception takes down the whole
            # portal -- vouchers, live sessions, admin and all -- over a coin
            # slot. Degrade to mock and make the reason visible instead.
            try:
                self._setup_gpio()
            except Exception as e:
                self.mock = True
                self.gpio_error = f"{type(e).__name__}: {e}"
                print(f"coinslot: GPIO unavailable, running MOCKED -- coins will "
                      f"NOT be counted: {self.gpio_error}", file=sys.stderr, flush=True)
        self._relay(False)

        t = threading.Thread(target=self._watcher, daemon=True)
        t.start()

    # ---- hardware ----

    def _setup_gpio(self):
        GPIO.setwarnings(False)
        GPIO.setmode(orangepi.one.BOARD)
        pin = int(self.cfg["coin_gpio_pin"])
        name = (HEADER.get(pin) or (None, None, None))[0]
        if not name or not name.startswith("P"):
            raise ValueError(f"physical pin {pin} is not a GPIO")
        want_rising = self.cfg.get("coin_edge") == "rising"
        bounce = int(self.cfg.get("coin_bounce_ms", 30))

        # The relay is a plain output and OPi.GPIO drives it correctly; only
        # the coin line's *edge detection* was broken, so only it moves.
        if self.cfg.get("relay_gpio_pin"):
            GPIO.setup(int(self.cfg["relay_gpio_pin"]), GPIO.OUT)

        self.coin_input = None
        if HAVE_GPIOD:
            try:
                _unexport(_line_offset(name))
                self.coin_input = _GpiodInput(
                    self._pulse_cb, want_rising, name, bounce)
            except Exception as e:
                # Busy line, missing chip, permissions -- fall through to
                # polling rather than leave the machine unable to take money.
                print(f"coinslot: gpiod unavailable on {name} "
                      f"({type(e).__name__}: {e}); falling back to polling",
                      file=sys.stderr, flush=True)
        if self.coin_input is None:
            GPIO.setup(pin, GPIO.IN)          # exports the sysfs value file
            self.coin_input = _PollInput(
                self._pulse_cb, want_rising, _line_offset(name), bounce)
        self.coin_input.start()
        print(f"coinslot: coin input armed on physical pin {pin} ({name}) via "
              f"{self.coin_input.name}, counting "
              f"{'rising' if want_rising else 'falling'} edges",
              file=sys.stderr, flush=True)

    def _teardown_gpio(self, old):
        if self.coin_input:
            self.coin_input.close()
            self.coin_input = None
        time.sleep(0.1)  # let the reader thread let go of sysfs before cleanup
        for key in ("coin_gpio_pin", "relay_gpio_pin"):
            if old.get(key):
                try:
                    GPIO.cleanup(int(old[key]))
                except Exception:
                    pass

    def reconfigure(self, cfg):
        """Apply new hardware settings live. Re-arms GPIO only if pin-level
        settings changed; timing/denomination changes are picked up instantly."""
        with self._lock:
            old = self.cfg
            rearm = any(old.get(k) != cfg.get(k) for k in _GPIO_KEYS)
            self.cfg = dict(cfg)
            if rearm and not self.mock:
                self._teardown_gpio(old)
                # Same reasoning as at construction, plus one of its own: this
                # runs inside an admin request, so an escaping exception would
                # 500 the Hardware page and leave the slot half-armed.
                try:
                    self._setup_gpio()
                except Exception as e:
                    self.mock = True
                    self.gpio_error = f"{type(e).__name__}: {e}"
                    print(f"coinslot: re-arm failed, running MOCKED: "
                          f"{self.gpio_error}", file=sys.stderr, flush=True)
        if rearm:
            self._relay(self._window_mac is not None)

    def _relay(self, energize):
        pin = self.cfg.get("relay_gpio_pin")
        self.relay_state = bool(energize)
        if self.mock or not pin:
            return
        level = energize != self.cfg.get("relay_active_low", True)
        GPIO.output(int(pin), level)

    def relay_test(self, on):
        """Diagnostics: force the relay regardless of window state."""
        self._relay(bool(on))

    def coin_level(self):
        """Diagnostics: current logic level of the coin input pin (None in mock)."""
        if self.mock or not self.coin_input:
            return None
        return self.coin_input.level()

    def _pulse_cb(self):
        with self._lock:
            self._train += 1
            self.total_pulses += 1
            self._last_pulse = time.monotonic()
            self.pulse_log.append(self._last_pulse)

    # ---- pulse-train completion + window timeout ----

    def _pesos_for(self, pulses):
        denoms = self.cfg.get("denominations") or {}
        if str(pulses) in denoms:
            return int(denoms[str(pulses)])
        return pulses * int(self.cfg.get("pulse_value_pesos", 1))

    def _watcher(self):
        while True:
            time.sleep(0.05)
            fire = timed_out = uncredited = save_pot = None
            with self._lock:
                if (
                    self._train
                    and time.monotonic() - self._last_pulse > float(self.cfg["pulse_end_gap_s"])
                ):
                    pulses, self._train = self._train, 0
                    pesos = self._pesos_for(pulses)
                    credited = self._window_mac is not None
                    self.last_train = {
                        "pulses": pulses, "pesos": pesos,
                        "at": time.monotonic(), "credited": credited,
                    }
                    if credited:
                        self._window_pesos += pesos
                        # keep the window alive while coins keep coming
                        self._window_deadline = (
                            time.monotonic() + float(self.cfg["insert_window_s"])
                        )
                        fire = (self._window_mac, pesos)
                    else:
                        # A real coin just fell in with nobody's window open.
                        # This used to be thrown away silently: no session, no
                        # sale, no record beyond a diagnostics field, and the
                        # customer simply lost their money. The relay is meant
                        # to lock the acceptor at these moments, but it is
                        # optional and can fail, so never rely on it alone.
                        # Hold the value instead; the next customer to open a
                        # window claims it (see open_window).
                        self.pending_pesos += pesos
                        self.pending_at = time.monotonic()
                        self._pending_wall = time.time()
                        self.unclaimed_total += pesos
                        self.unclaimed_events += 1
                        uncredited = (pesos, self.pending_pesos)
                        save_pot = (self.pending_pesos, self._pending_wall)
                if self._window_mac and time.monotonic() > self._window_deadline:
                    timed_out = (self._window_mac, self._window_pesos)
                    self._close_locked()
            if fire:
                self.on_coin(*fire)
            if save_pot and self.on_pending:
                self.on_pending(*save_pot)      # survive a reboot
            if uncredited:
                print("coinslot: P%d inserted with no window open -- holding "
                      "P%d for the next customer" % uncredited,
                      file=sys.stderr, flush=True)
            if timed_out:
                self.on_timeout(*timed_out)

    # ---- public API ----

    def open_window(self, mac):
        """Claim the slot for `mac`. Returns False if someone else holds it."""
        claimed = False
        with self._lock:
            if self._window_mac and self._window_mac != mac:
                return False
            self._window_mac = mac
            self._window_deadline = time.monotonic() + float(self.cfg["insert_window_s"])
            if self._window_pesos == 0:
                self._train = 0
            # Hand over any coins that fell in while nobody had the slot open.
            # Bounded by uncredited_hold_s so a coin from hours ago is not
            # given away to an unrelated customer; anything older stays on the
            # books for the owner to see and settle by hand.
            hold = float(self.cfg.get("uncredited_hold_s", 300) or 0)
            fresh = time.monotonic() - self.pending_at <= hold
            if self.pending_pesos and hold > 0 and fresh:
                self._window_pesos += self.pending_pesos
                self.claimed_pesos = self.pending_pesos
                self.pending_pesos = 0
                self._pending_wall = 0.0
                claimed = True
        if claimed and self.on_pending:
            self.on_pending(0, 0.0)             # pot emptied; forget it
        self._relay(True)
        return True

    def close_window(self, mac):
        """Close `mac`'s window; returns the total pesos inserted in it."""
        with self._lock:
            if self._window_mac != mac:
                return 0
            total = self._window_pesos
            self._close_locked()
            return total

    def _close_locked(self):
        self._window_mac = None
        self._window_pesos = 0
        self._train = 0
        self._relay(False)

    def status(self, mac):
        with self._lock:
            return {
                "open": self._window_mac == mac,
                "busy": self._window_mac is not None and self._window_mac != mac,
                "pesos": self._window_pesos if self._window_mac == mac else 0,
                "seconds_left": max(0, int(self._window_deadline - time.monotonic()))
                if self._window_mac == mac else 0,
                # Coins waiting to be claimed by whoever opens the next window.
                # Surfaced so the portal can say "P5 waiting -- tap INSERT COIN"
                # rather than leaving the customer to wonder where it went.
                "pending": self.pending_pesos,
            }

    def diag(self):
        """Snapshot for the diagnostics page."""
        now = time.monotonic()
        with self._lock:
            log = list(self.pulse_log)
            pulses = [
                {
                    "ago_s": round(now - t, 2),
                    "gap_ms": int((t - log[i - 1]) * 1000) if i else None,
                }
                for i, t in enumerate(log[-20:])
            ]
            last = None
            if self.last_train:
                last = dict(self.last_train)
                last["ago_s"] = round(now - last.pop("at"), 1)
            ci = self.coin_input
            return {
                "mock": self.mock,
                "level": self.coin_level(),
                # Which backend is actually reading the line, and what it has
                # seen. `edges_other` counting up while `total_pulses` stays at
                # zero means the line pulses the opposite way to `coin_edge` --
                # the one symptom that otherwise looks identical to a dead
                # acceptor.
                "input_backend": ci.name if ci else None,
                "input_error": ci.error if ci else None,
                "edges_counted": ci.counted if ci else 0,
                "edges_rising": ci.rising if ci else 0,
                "edges_falling": ci.falling if ci else 0,
                "edges_other": (
                    (ci.rising if self.cfg.get("coin_edge") != "rising"
                     else ci.falling) if ci else 0),
                "total_pulses": self.total_pulses,
                "train": self._train,
                "last_train": last,
                "pulses": pulses,
                "relay": self.relay_state,
                "relay_pin": self.cfg.get("relay_gpio_pin") or 0,
                "window_mac": self._window_mac,
                "window_pesos": self._window_pesos,
                # Money that landed with nobody's window open. `pending` is
                # still claimable; `unclaimed_total` is the running tally since
                # boot and is the number the owner should watch -- a rising
                # figure means the relay is not doing its job.
                "pending_pesos": self.pending_pesos,
                "pending_age_s": round(now - self.pending_at, 1) if self.pending_pesos else None,
                "unclaimed_total": self.unclaimed_total,
                "unclaimed_events": self.unclaimed_events,
            }

    def inject(self, pulses):
        """Inject a fake pulse train (mock 'simulate coin' + diag testing).
        Goes through the exact same train-completion path as real pulses."""
        pulses = max(1, int(pulses))
        with self._lock:
            base = time.monotonic() - float(self.cfg["pulse_end_gap_s"]) - 0.1
            for i in range(pulses):
                self.pulse_log.append(base + i * 0.06)
            self._train += pulses
            self.total_pulses += pulses
            self._last_pulse = base + pulses * 0.06
        return True

    def simulate(self, pesos):
        """Mock-mode coin insert by peso value (admin test button)."""
        denoms = self.cfg.get("denominations") or {}
        for pulse_count, value in denoms.items():
            if int(value) == int(pesos):
                return self.inject(int(pulse_count))
        return self.inject(int(pesos / int(self.cfg.get("pulse_value_pesos", 1))))
