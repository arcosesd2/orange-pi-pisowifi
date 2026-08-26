---
name: keep-pisowifi-notes-updated
description: "On the PisoWiFi project, keep the skill and project log updated as work progresses rather than only at the end"
metadata: 
  node_type: memory
  type: feedback
  modified: 2026-08-23T19:27:37.682Z
  originSessionId: f9d04533-9903-4fd1-99c1-9635ef3b286c
---

On 2026-08-23 the user asked me to take notes and summarise what I do and
analyse **as the work progresses**, into both memory and skill artifacts —
not only when asked at the end.

**Why:** the same mistakes were repeated several times in one session
(PowerShell quoting broke a remote command twice; a heredoc ate backslashes
after that exact class of bug had already been diagnosed). The user's concern
is repetition, not documentation for its own sake. Notes are worth writing
only if they change what happens next time.

**How to apply:** when something in this project fails in a way that was not
obvious beforehand, write it up at that moment:

- the rule into `.claude/skills/pisowifi/SKILL.md`, in the failing-form /
  working-form shape used there, **including the symptom** — nearly everything
  in this project fails silently, and the symptom is what saves the next hour
- the incident into `docs/PROJECT-LOG.md` with the evidence
- only promote a rule once it has actually bitten; speculation dilutes it

Related: [[pisowifi-project-references]], [[pisowifi-orange-pi-project]]
