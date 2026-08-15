# Archive

Historical build artifacts. Do not use as reference for current code.

| File | What it was |
|------|-------------|
| `sprintctl_starter.md` | Original spec prompt used to kick off the architecture design |
| `sprintctl_revised_architecture.md` | Pre-implementation revised architecture doc; describes design decisions behind what was eventually built |
| `session-phase1.md` | Build prompt for Phase 1 (core schema, CLI, rendering) |
| `session-phase1.5.md` | Build prompt for Phase 1.5 (transition enforcement in db.py, calc.py) |
| `session-phase2.md` | Pre-revision Phase 2 plan — describes a daemon-based architecture that was superseded before implementation |
| `session-phase3.md` | Pre-revision Phase 3 plan — describes knowledge promotion and API wrapper work moved to [kctl](https://github.com/bayleafwalker/kctl) |
| `cutover-dogfood.md` | Phase 28 operator procedure for the per-repo authority + projection cutover dogfood (#1163). The `sprintctl pilot` command surface it drives was retired, and `sprintctl/pilot.py` and `sprintctl/cutover.py` deleted, so the procedure is no longer executable |

The current source of truth for architecture and design decisions is the codebase itself and [README.md](../../README.md).
