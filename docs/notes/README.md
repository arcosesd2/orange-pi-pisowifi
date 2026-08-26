# Working notes

These are Claude Code's persistent memory files for this project, copied out of
`~/.claude/` so they travel with the repository.

They are **not** documentation of how the software works — that lives in the
top-level [`README.md`](../../README.md). These are the things that are not
derivable from the code: which hardware quirks bit us, which approaches were
tried and abandoned, and what state the live board is in.

| File | What it records |
|---|---|
| [`pisowifi-orange-pi-project.md`](pisowifi-orange-pi-project.md) | The board, its network, and the facts about this deployment that the repo cannot tell you |
| [`pisowifi-project-references.md`](pisowifi-project-references.md) | Where the rules and the log live, and when to read them |
| [`pisowifi-gpio-interrupts-dead.md`](pisowifi-gpio-interrupts-dead.md) | `OPi.GPIO.add_event_detect()` arms cleanly and never fires on this kernel — the worst bug this project has had |
| [`pisowifi-anti-tethering.md`](pisowifi-anti-tethering.md) | Anti-tethering works, and still ships off; why, and how to retry it safely |
| [`keep-pisowifi-notes-updated.md`](keep-pisowifi-notes-updated.md) | Write incidents down as they happen, not at the end |

`[[double-bracket]]` links are cross-references between these files.

Two related documents are worth reading alongside them:

- [`docs/PROJECT-LOG.md`](../PROJECT-LOG.md) — the dated, evidence-bearing
  account of each working session.
- [`.claude/skills/pisowifi/SKILL.md`](../../.claude/skills/pisowifi/SKILL.md) —
  the rules distilled out of those incidents. Every entry is a defect that
  reached hardware.

## A note on what is in here

These notes describe a machine that handles cash on a private LAN, and they are
candid about what is not yet secured. `docs/PROJECT-LOG.md` in particular lists
the weaknesses that are still open on the live board. That is deliberate — the
point of the log is to be honest — but it is also why this repository is
private. Think twice before making it public.
