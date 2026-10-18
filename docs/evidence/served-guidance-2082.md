# Served and local guidance reconciliation: sprintctl#2082

Date: 2026-10-18. Source inspected:
`9d2e586712048b9ef8deb234e48cba0a648a3b37`.
The live item description defines this documentation-only scope; reservation
295 was recorded before edits. No runtime, authority, migration, recovery,
claim, compatibility, dependency or package semantics change.

## Corrections and inspected owners

- `sprintctl/backend.py`: explicit profile precedence, protected exact-worktree
  delegated handoff, marker/backend mismatch refusal and local recovery scope.
  `SPRINTCTL_DB` cannot override a served repository marker.
- `sprintctl/commands/work.py`: served item/sprint lifecycle calls carry their
  immutable basis and reject `--expected-revision`; direct local transitions
  require that option. An advisory reservation supplies no authority.
- `sprintctl/served_routes.py`: maintenance, backlog seeding and rendering are
  unavailable in served mode. Visible Click help is not served capability or
  caller authorization. A local snapshot cannot replace missing shared output.
- `sprintctl/maintain.py`: check is read-only; sweep changes stale item status,
  optionally closes an overdue sprint, and interrupts eligible stale
  reservations across the selected database without a sprint filter.
- Kctl source
  `dcd82ee520f2ec53377c54c29b0bb3de9d228666`: local extraction and review stay
  separate from work mutations. Approve overrides, reject, publication and
  explicit coordination publication use their actual existing commands. Kctl's
  five-type durable filter is distinguished from Sprintctl's four-type raw
  knowledge/backlog-seeding filter, which excludes `blocker-resolved`.
- Vuoro source
  `aa5af4de351f011c1d4f795eaa72b692c5dfcb47`: Workspace is the separate current
  served knowledge owner. Retained Kctl commands are not a bridge, grant,
  migration or accepted Workspace intake.

The former `docs/reference/cutover-dogfood.md` in the item's historical scope
now lives at `docs/archive/cutover-dogfood.md`. The archive receives an explicit
current-routing annotation; its original introduction and entire Phase 28 body
are preserved byte for byte. Plan files and ratification metadata are untouched.

## Verification

Existing checks, with the frozen development, served and remote extras:

```sh
uv run --frozen --extra dev --extra served --extra remote pytest -q \
  tests/test_docs_integrity.py tests/test_install_workflow_contract.py \
  tests/test_backend_mode.py tests/test_backend_served_mode.py \
  tests/test_served_routes.py tests/test_delegated_cwd_profile.py
```

Result: **95 passed in 1.00 s**, Python 3.13. The selected tests cover the
existing documentation/install contract, backend refusals and protected profile
resolution. No test oracle or product code was changed for this item.

Source entrypoint `--help` checks covered Sprintctl item/sprint status,
maintenance check/sweep, backlog seeding and event listing. Kctl review
list/show/approve/reject, extraction and publication were checked against the
Click tree and help. Kctl subcommand help initializes its local store; these
checks used only `/tmp/kctl-guidance-help.db`, not shared knowledge data.
Ten local Markdown links/anchors resolved. A direct comparison against the
inspected source confirmed the archived body and original introduction were
preserved and no `docs/plans` file changed.

The optimistic planner independently reviewed the actual diff against owner
source. Its maintenance reservation-sweep clarification was included. The Sprintctl owner also reviewed the cross-tool boundary without blockers;
that review did not rerun this suite. The final documentation/install subset
passed **18 tests in 0.03 s**. Native work refs retain the merged change and CI result separately.

## Limits

This establishes correspondence of the revised guidance with the inspected
implementation. It does not establish deployed feature availability, caller
permissions, a cutover, legacy migration or a successful knowledge submission.
No example maintenance, publication, seeding or close command was executed
against live work. The installed global CLI can lag source; its rejected scoped
reservation argument was a pre-I/O failure, and the current work reservation
used the explicit repository context. Source development does not upgrade a
host installation or deploy a service.

The current generated `agent-protocol` status snippet still describes direct
local CAS syntax. This documentation change makes mode differences explicit;
it does not silently repair that separate runtime-output issue.
