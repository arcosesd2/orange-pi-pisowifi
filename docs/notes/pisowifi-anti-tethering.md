---
name: pisowifi-anti-tethering
description: "Anti-tethering on the PisoWiFi is built but ships OFF - it was enabled on hardware 2026-08-26, worked, and was reverted at the owner's request"
metadata: 
  node_type: memory
  type: project
  modified: 2026-08-26T02:00:00.000Z
  originSessionId: f9d04533-9903-4fd1-99c1-9635ef3b286c
---

Anti-tethering exists in the code but **both toggles ship `false`**, and the
live board was deliberately returned to that state on 2026-08-26.

Two halves, and they are not equally safe:

- `anti_tether` - egress `ip ttl set 1` toward customers. **This one alone is
  enough**: the paying device is the last hop so it receives normally, but a
  tethered device behind it never gets replies. It cannot cut anyone off.
- `anti_tether_strict` - drops client packets whose TTL shows a hotspot
  forwarded them. Only adds anything against a phone that rewrites TTL, and it
  **fails closed**: an unusual stack, or an AP that routes instead of bridges,
  cuts off real customers.

**It did work.** On the test phone, its own traffic (portal polls and its own
internet) arrived at TTL 64, while traffic that had crossed one forwarding hop
arrived at TTL 63 with retransmits. That is the tethering signature.

It was reverted because the strict counter reached 979 packets within minutes
and it was not yet established whether that was a tethered device retrying or a
paying customer being harmed. **The owner asked for the revert and it was the
right call** - this machine takes cash, and an unproven rule in front of a
paying customer is not acceptable.

If retried: turn on the **egress half only** and leave strict off.

Also fixed while doing this: `detect.sh` substitutes template placeholders with
`str.replace()`, so a placeholder named in a comment gets substituted too. Never
write a `#@TOKEN@` in prose in `network/nftables.conf`. `test_reachability.py`
now guards it.

See [[pisowifi-orange-pi-project]] and [[pisowifi-gpio-interrupts-dead]].
