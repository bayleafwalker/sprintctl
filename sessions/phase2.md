## Task: Claims, staleness detection, daemon sweeper, carryover
## Spec reference: sprintctl_starter.md (agent reads as needed)

## This session builds:
- `sprintctl/claims.py` — Claim table, claim create/heartbeat/release/expire logic; claim types: inspect / execute / review / coordinate
- `sprintctl/policies.py` — central policy config (TTL, heartbeat interval, staleness threshold per claim type); no per-field overrides, profiles only
- `sprintctl/daemon.py` — sweeper loop: expire stale claims, mark items stale, emit system events
- `sprintctl/carryover.py` — sprint close: carry unfinished items to next sprint as PendingItems
- additions to `sprintctl/cli.py` — `claim`, `release`, `heartbeat`, `sprint close` subcommands
- additions to `sprintctl/render.py` — stale indicators, claim status per item
- `tests/test_claims.py`, `tests/test_daemon.py`

## Stop at:
- No knowledge promotion
- No API wrapper
- Policy profiles exist but only the default profile need work end-to-end

## Acceptance criteria:
- An expired claim (past TTL without heartbeat) is detected and released by the sweeper without manual intervention
- Exclusive claim types (execute, review) block a second claimant; non-exclusive types (inspect) do not
- `sprint close` moves all non-done items to a new PendingItem record and marks the sprint closed
- Stale items surface in `render` output with a staleness marker and age
