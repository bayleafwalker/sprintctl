# Private Release policy extraction

Bounded child `sprintctl#2806` of unfinished backend refactor `sprintctl#2150`.
Native No doc5812 and oracle freeze5822 govern Release
`05fb546d4db82292afa789983d54e047bfd4779af730dd14ad8f5e03024bf338`
at accepted predecessor `1ec78fa9a2f1dbacc4d5ec832c14b961e09abc12`.

Private `releasecore` now owns basis count/revision derivation and query shape,
canonical freezing, current selection query and default/explicit membership.
The eight original backend helpers retain signatures, dynamic entrypoints,
execution, decoding, tenant parameters, optional PG row locks and transaction
ownership. Existing pure `releases.py`, native Decision/effect/reservation owners
and 533 adjacent function/class ASTs remain unchanged.

The insertion callback consumes a core-owned value supplier exactly once after
binding execute. Digest-document JSON occurs before binding; late item ID and
contract/ref JSON occur afterward. PostgreSQL binds the ref canonicalizer after
SELECT before fetch; SQLite binds it after fetched dict materialization. Real
transparent proxies discriminate these evaluation points without adding a public
arbitrary-proxy contract. Missing basis uses `is None`; current-row decoding
retains its original truthiness rule. Explicit falsey digests remain membership
requests, and historical same-item membership is distinct from later Decision
admission.

The unchanged oracle has46 shared cases and9 PG extensions. Final accepted-source
and extracted-source combined gates both passed326 local and958 PG cases, with
52 registered PG fixture cleanups empty. Five actual query captures preserve
normalized SQL tokens, parameter order/types and optional locking. ID/repository
normalization in that comparison excludes fixture-generated identities only.
Independent expected canonical bytes/digests, actual owner pending writes,
SAVEPOINT rollback, native SQL trigger failure states, READ ONLY reads and an
exact `pg_blocking_pids` holder/contender history supplement those structural
checks. No general linearizability claim follows.

Full local regression passed3089 tests with4 skips and14 performance cases
excluded in454.96s. Full PG passed1766 tests in101.47s, with89 registered
fixture cleanups empty. Independent source review is clear; the reviewer also
ran all46 SQLite cases. Exact-head CI and merge equality remain publication gates.

Thirty-one finite wrong implementations fail discriminating assertions: the
historical28 controls belong to their recorded45/54-case oracle hashes; the final
additional binding case has two positive and three wrong-control runs. Removing
only that added method reconstructs the prior exact recorded shared-file SHA,
and independent whole-module AST comparison proves all old fixtures/helpers/tests
unchanged. This is honestly labeled reconstruction, not a claimed earlier archive.
Initial setup failures from invalid NULL labels and strict empty-description
editing remain historical development evidence. Final stored fixtures are valid;
deliberate malformed row/value tests are identified as test-owned characterizations.

See the [context](../../verification/contexts/release-policy.json),
[baseline](../../verification/fixtures/release-policy-baseline-2806.json),
[observations](../../verification/traces/release-policy-histories-2806.json) and
[result](../../verification/results/release-policy-2806.json).
The postcampaign extractor now accepts explicit LABEL=XML inputs; actual CLI
replay reproduces the entire published observation object exactly. Its first
version needed a manual label/schema projection despite equal raw observations;
that publication gap was fixed without changing product or oracle bytes.
The original source plan and binding refinement are retained with exact hashes.
Only temporary SQLite and disposable PG scopes receive faults. No deployment,
physical durability, public API/schema/IDL, provider interchangeability or parent
completion is claimed. Dependency guard sharing and backend-peer imports remain
explicit separate work under2150.
