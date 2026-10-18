# Hybrid actor convention: sprintctl#2020 disposition

Observed 2026-10-18 against Sprintctl source
`16f121f1fc527f3238953b6f203b32530cdfb5bc` and the served item. Recommended
terminal decision: **withdraw** the obsolete hybrid-dispatch implementation
request. This evidence records a recommendation; it is not a terminal Decision.

The item asks Sprintctl to distinguish coordinator-held hybrid claims from
direct work and reconcile ActionQ actor prefixes. Its July finding already
records that served writes bind the authenticated identity, so an actor prefix
cannot select another authority principal. The historical serialization blocker
sprintctl#2088 is done; there is no active execution reservation from another
session on #2020 at this observation.

The accepted successor direction places execution, sandboxing and model choice
in the native harness. Agentops TS-2 explicitly excludes a hybrid revival,
including its concurrency tripwire. The October product-direction amendment
retains native execution and does not restore ActionQ's runner, queue or daemon.
The Vuoro disposition register marks `hybrid-dispatch` retired, superseded by
harness-native subagents, and gives “Not revived” as its waking condition:

- [Agentops target state and October amendment](https://github.com/bayleafwalker/agentops/blob/f205cdd67669242612f2c1d10294657a1c8def42/docs/plans/2026-09-17-target-state.md)
- [Vuoro hybrid-dispatch disposition](https://github.com/bayleafwalker/vuoro/blob/aa5af4de351f011c1d4f795eaa72b692c5dfcb47/docs/direction/disposition-register.yaml#L2177)

The July [authority recovery and actor convention design](../plans/2020-2026-authority-recovery-design.md)
contains `coordinator_actor`, `worker_actor` and `dispatch_id` requirements for
#2020. A source search found those names only in that design, with no matching
runtime implementation. This is an unimplemented historical request superseded
by accepted direction; withdrawal must not be described as successful delivery
of those fields. The separate #2026 recovery design is outside this disposition.

Current advisory reservation roles and authenticated served attribution remain
useful coordination metadata. Native-harness delegation observations can be
recorded as evidence under their own current contract; they do not require a
hybrid worker identity, actor-prefix authority, a revived dispatcher, or another
acceptance writer. This disposition changes no runtime, API, schema or grant.

Validation: inspected the served item's description, historical events,
dependencies and reservations; compared the pinned accepted direction; searched
`sprintctl/` and contributor guidance for the historical field names. No worker
dispatch, live claim, capability, migration or deployment was exercised.
Repository documentation, installation and lease-document checks: 22 passed.
Verification packet validation and whitespace checks passed. Independent
findings-first and optimistic review found no remaining issue.
