---
name: pisowifi-project-references
description: Where the PisoWiFi rules and history live - the pisowifi skill and docs/PROJECT-LOG.md in the repo
metadata: 
  node_type: memory
  type: reference
  modified: 2026-08-23T19:27:26.729Z
  originSessionId: f9d04533-9903-4fd1-99c1-9635ef3b286c
---

Two living documents in the PisoWiFi repo, both created 2026-08-23 and both
meant to be **kept updated as work continues** (see
[[keep-pisowifi-notes-updated]]):

- **`.claude/skills/pisowifi/SKILL.md`** — a project skill, so it auto-loads.
  Rules distilled from bugs that actually shipped, in failing-form /
  working-form shape. Covers: PowerShell→ssh→sh text mangling (ASCII-only
  `.ps1`, `$ErrorActionPreference`, quoting, the `sed` that corrupted 7 of 10
  modules), Jinja `| tojson` for JSON in `<script>`, nftables 1.1 quoting and
  rule ordering, Armbian first-login behaviour, and verification discipline.
- **`docs/PROJECT-LOG.md`** — dated incident log with the evidence behind each
  rule, plus the open-items checklist.

Currently on branch `claude/pisowifi-existing-app-6c1b6a`, not `master`, so
they are only visible from that worktree until merged.

Related: [[pisowifi-orange-pi-project]]
