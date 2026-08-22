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
import sys
import threading
import time

try:
    import orangepi.one
    from OPi import GPIO
    HAVE_GPIO = True
except ImportError:
    GPIO = None
    HAVE_GPIO = False

# cfg keys that require tearing down / re-arming the GPIO when changed
_GPIO_KEYS = ("coin_gpio_pin", "coin_edge", "coin_bounce_ms",
              "relay_gpio_pin", "relay_active_low")


class CoinSlot:
    def __init__(self, cfg, on_coin, on_timeout):
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

        self.gpio_error = None
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
        GPIO.setup(pin, GPIO.IN)
        edge = GPIO.RISING if self.cfg.get("coin_edge") == "rising" else GPIO.FALLING
        GPIO.add_event_detect(
            pin, edge, callback=self._pulse_cb,
            bouncetime=int(self.cfg.get("coin_bounce_ms", 30)),
        )
        if self.cfg.get("relay_gpio_pin"):
            GPIO.setup(int(self.cfg["relay_gpio_pin"]), GPIO.OUT)

    def _teardown_gpio(self, old):
        try:
            GPIO.remove_event_detect(int(old["coin_gpio_pin"]))
        except Exception:
            pass
        time.sleep(0.1)  # OPi.GPIO's event thread touches sysfs on exit
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
        if self.mock:
            return None
        try:
            return int(GPIO.input(int(self.cfg["coin_gpio_pin"])))
        except Exception:
            return None

    def _pulse_cb(self, _channel):
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
            fire = timed_out = None
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
                if self._window_mac and time.monotonic() > self._window_deadline:
                    timed_out = (self._window_mac, self._window_pesos)
                    self._close_locked()
            if fire:
                self.on_coin(*fire)
            if timed_out:
                self.on_timeout(*timed_out)

    # ---- public API ----

    def open_window(self, mac):
        """Claim the slot for `mac`. Returns False if someone else holds it."""
        with self._lock:
            if self._window_mac and self._window_mac != mac:
                return False
            self._window_mac = mac
            self._window_deadline = time.monotonic() + float(self.cfg["insert_window_s"])
            if self._window_pesos == 0:
                self._train = 0
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
            return {
                "mock": self.mock,
                "level": self.coin_level(),
                "total_pulses": self.total_pulses,
                "train": self._train,
                "last_train": last,
                "pulses": pulses,
                "relay": self.relay_state,
                "window_mac": self._window_mac,
                "window_pesos": self._window_pesos,
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
