# Served maintenance diagnostic CLI

Item: sprintctl#2305. Source baseline:
`16f121f1fc527f3238953b6f203b32530cdfb5bc`.

The existing owner already returns an empty, healthy diagnostic when no active
sprint exists and a typed `sprint-not-found` refusal for an unknown explicit
sprint. The remaining defect was the CLI's unavailable disposition and absent
facade. Six adapter regressions now exercise the Click command through the
served facade into the existing owner with temporary SQLite data, without
network access or shared events. All five original regressions failed against
the baseline's `served-operation-unavailable` guard before implementation; the
sixth verifies that a timeout remains an error, never an empty diagnostic.

`maintain check [--sprint-id N] [--json]` now invokes the existing
`work.maintain.check` operation and doctor discovers that operation. JSON keeps
the owner's diagnostic; text handles both empty and selected sprints. The
server owns thresholds, so an explicit `--threshold` refuses before invocation.
The local command retains its existing threshold and rendering behavior.
`maintain sweep` and `maintain carryover` keep their existing served refusal.
There is no new catalog operation, schema migration, install or deployment.

Validation:

- 117 passed: new CLI regressions, route registry, local maintenance, facade
  shapes and failure-detail checks.
- 107 passed after adding the doctor probe and reconciling both guidance pages:
  facade, routing, six CLI regressions, doctor and documentation integrity.
- 31 passed: operation surface, result schema conformance, documentation and
  install workflow contracts.
- 3 passed: owner maintenance cases, including empty and missing sprints.

The counts overlap and are not additive. An earlier broad run had a failing
test double that passed a raw owner exception directly to the client; it was
corrected to translate the owner rejection into the installed client's typed
`InvocationRejectedError`, as the actual service boundary does. The final
focused runs above pass. This is adapter-boundary verification, not a claim
of live transport or PostgreSQL qualification.

The first Python 3.12 CI run found one additional stale availability fixture
that still classified `maintain check` as unavailable (2370 other tests passed).
That fixture now tests the still-unavailable sweep and carryover commands;
the six diagnostic regressions above cover the newly available check command.
The revised parity, CLI and documentation checks passed all 30 cases locally.

Independent optimistic review found the implementation correct and identified
one contradictory knowledge-review guide sentence. Both that guide and the
remote-mode guide now describe the existing read-only served operation and
server thresholds. The estate rewrite's future contract foundation remains
unselected; this corrects access to an existing interface under its current
contract and does not preempt sprintctl#2738.
