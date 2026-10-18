# Backend-specific agent protocol guidance: sprintctl#2716

Date: 2026-10-18. The defect was observed in source
`9d2e586712048b9ef8deb234e48cba0a648a3b37` during the separate
[guidance reconciliation](served-guidance-2082.md): `agent-protocol` presented
`--expected-revision` as an unconditional status instruction, while served
lifecycle commands explicitly reject that direct-local option.

## Bounded repair

Protocol version 3 and the original `3_status_transition.command` string are
retained. `command_backend` labels that compatibility field as local;
`backend_commands` gives explicit local and served alternatives. Text output
renders both with their backend labels. The note states that direct local
status needs the current revision, served status uses the authenticated actor
and immutable owner basis, and a reservation confers no authority.

Only instruction generation and rendering change. Lifecycle validation,
authorization, backend selection, persistence, owner operations, package pins,
versions and deployment remain unchanged. Existing consumers of the local
compatibility field retain its bytes; consumers choosing a backend can use the
additive fields. No closed JSON schema is introduced or changed.

## Verification

Four new checks in `tests/test_agent_protocol.py` failed against the original
output before the implementation. After the repair:

- JSON retains the v3 local instruction and identifies the two backend forms.
- Text labels and prints those same forms with the identity/basis distinction.
- The emitted local command executes against temporary SQLite; reusing its
  old revision for another valid transition fails with a status revision
  mismatch and leaves the item unchanged.
- The emitted served command passes the real Click routing/option guards to
  a stubbed owner entrypoint without opening local state. The old direct-local
  command remains refused before that entrypoint. This test runs only on
  Python 3.12+, matching the existing served client requirement.

The protocol, documentation, CLI-structure and served-routing selection passed
**44 tests**. A final protocol-only check passed **4 tests**, including the
specific stale-revision error assertion. The optimistic planner reviewed the
actual diff and found no blockers.

The full Python 3.13 non-performance source suite passed **2,321 tests**, with
**1,100 skipped** and **14 deselected**, in 326.88 s. PostgreSQL cases were
not configured in that local run; the dedicated CI job exercises them. The
same focused selection on Python 3.11 passed **37 tests**, with **7 skipped**
under existing served-client version guards. The Python 3.11 environment was
separate from the full-suite environment.

Full-suite and CI outcomes are recorded on the native item's immutable Git/CI
refs before acceptance. This is bounded example evidence of the emitted
instructions and existing CLI boundary, not service deployment or owner
arbitration qualification. No live work item was mutated by the test commands.
