# Authenticated cooperative attempt facts

sprintctl 0.17.0 / remote schema22 adds five owner operations under the existing
`work.effect.mark-applied` authority. Ordinary work scopes cannot call them.
Calls use a native `idempotency_key` inside their arguments; an outer envelope
key is prohibited, because a transport cache must never replay fresh dispatch
permission. The owner does not invoke, queue, schedule or retry provider work.

| Operation | Required arguments | Meaning |
| --- | --- | --- |
| `work.effect.attempt-open-v1` | `intent_id`, `revision`, `canonical_intent_digest`, `expected_revision`, `release_digest`, `target`, `idempotency_key` | Authorize one declared operation against the exact accepted intent/current full Release. |
| `work.effect.attempt-redeem-v1` | `attempt_id`, `authorization_digest`, `idempotency_key` | Consume the authorization before a cooperative provider invocation. |
| `work.effect.attempt-seal-unused-v1` | Same as redeem | Close an authorization that was never redeemed. |
| `work.effect.attempt-report-applied-v1` | Same as redeem, plus `commit_sha`, `pr_url` | Attribute the applier's report to an already redeemed PR authorization. |
| `work.effect.attempt-get-v1` | `attempt_id` | Read its current state and verified complete event chain in one read-only snapshot. |

All operations bind repository, workspace, principal, client and grant from
the authenticated context. An applier does not need a proposer run. The intent
must belong to its workspace. Another identity cannot adopt an authorization,
including through a previously committed idempotency key.

`expected_revision` is the full owner revision, including `@revise:<count>`;
plain description revisions are refused. Open and fresh redemption recompute
the stored intent digest and require the exact current Release and existing
protected verification guard. Sealing and reporting may record an old attempt
after its work basis changed, because they grant no new invocation permission.

The target is a closed union:

```json
{"operation":"push_branch","branch":"<configured-prefix>/<intent-id>","commit_sha":"<prepared-Git-object-id>"}
```

```json
{"operation":"open_pull_request","branch":"<configured-prefix>/<intent-id>","commit_sha":"<prepared-Git-object-id>","base_branch":"main"}
```

The owner derives repository, base commit and title/body hashes from stored
intent content. The prepared commit is the applier's declaration. The provider
and reconciler still check allowlists, protected refs, signatures and actual
remote heads. Push and PR creation require separate authorizations. Only one
authorization can exist per repository/intent revision/provider operation,
regardless of identity, target or idempotency key. Sealed authorizations cannot
be reopened; a new intent is required for another authorization.

Attempt state is `accepted → redeemed` or `accepted → sealed_unused`. Facts
are appended in the same transaction:

1. `attempt_authorization_accepted` records the immutable authorization.
2. `invocation_authorization_redeemed` or `attempt_closed_without_redemption`
   records its single consumption outcome.
3. A redeemed PR authorization may receive one `application_report_received`.

The native ledger stores historical receipts only. After commit, redemption
returns `delivery: "fresh"` and `dispatch_permitted: true` for the fresh
delivery. A same-key retry returns the identical historical receipt with
`delivery: "replay"` and `dispatch_permitted: false`, even if the original
reply was lost. A different-key retry of a consumed authorization refuses.
The reconciler must retain its own durable dispatch tracking and reconcile an
ambiguous redemption; it must never treat a replay as permission to invoke.

These facts have narrow meanings. Redemption does not prove a provider call,
success or failure. Sealing does not prove subject-wide non-invocation; legacy
or unrelated actors may still act. A report is an authenticated applier claim,
not an independent completion observation, and it does not transition the
legacy intent to `applied`. Independent provider evidence and a separately
qualified evaluator are still required. `work.evidence.evaluate-v1` retains
its existing unsupported execution-fact result; instrumentation and an
additive evaluator remain subsequent work.

The deployment-owned migrator installs schema22 atomically under its existing
global migration lock. It refuses foreign names. Immutable guards prevent
changing/removing authorizations or events; deferred consistency guards reject
commits with a state transition and missing fact. Runtime startup remains
read-only and admits exactly schema22. Publishing this source release does
not itself migrate any shared or Cloud deployment.
