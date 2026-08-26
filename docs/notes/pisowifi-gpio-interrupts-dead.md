---
name: pisowifi-gpio-interrupts-dead
description: "On the Orange Pi One's Debian 13 kernel, OPi.GPIO add_event_detect arms cleanly and never fires - the coin slot silently counted nothing"
metadata: 
  node_type: memory
  type: project
  modified: 2026-08-26T01:10:00.000Z
  originSessionId: f9d04533-9903-4fd1-99c1-9635ef3b286c
---

**`OPi.GPIO.add_event_detect()` does not deliver on kernel
6.18.44-current-sunxi.** It arms the deprecated sysfs edge interface; the
kernel accepts the arming and never fires an interrupt. Silent - no exception,
no log line, no restart, no degraded flag.

This cost a long debugging session on 2026-08-26 because **every diagnostic
reported health** while the machine took coins and credited nothing: service
active with 0 restarts, pin exported `direction=in edge=falling`, an open fd on
the value file, `mock=False`, `gpio_error=None`.

**The test that finds it:** level-poll `/sys/class/gpio/gpioN/value` from a
separate process while the app runs. If the line transitions and the app's
counter does not, the delivery mechanism is dead, not the hardware. One coin
drop settles it.

Fixed in `86f2929` - the coin line reads through libgpiod v2 edge events
(`python3-libgpiod`), with level polling as fallback. **A sysfs export outlives
its process and holds the line against the character device** (`EBUSY`), so it
must be unexported first, or the machine silently falls back to polling forever.

Do not trust the older note that said Trixie was fine for GPIO - that was
written from "the pin arms successfully", which is exactly the misleading part.
Full write-up in the repo's `docs/PROJECT-LOG.md` and SKILL.md section 5.

See [[pisowifi-orange-pi-project]] and [[pisowifi-project-references]].
