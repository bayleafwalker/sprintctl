# Locked Decision writer extraction

Bounded child `sprintctl#2805` of unfinished repository refactor `sprintctl#2150`.
Native No doc5785, refinement5803 and accepted-source freeze5804 govern Release
`cdfc755f2c4f768563e2ca7831d33fc72e52fcf6f0cbfb50d6d600bf4182e389` on
accepted predecessor `9e55ebe01f5085c54c436a54180f4227173a415f`.

The private eight-operation participant in `workitemcore` owns admission,
successor/Release order, append-before-bind, legacy/terminal branch choice and
result timing. Backend callbacks retain original SQL, tenant predicates, cursors,
JSON encoding, native effects and dynamic helpers. All nine Decision callers and
two separate session-release bindings retain their existing owners. The primitive
starts, commits and rolls back no transaction and emits no event.

The insert handle stays opaque. SQLite retains lastrowid until its final
SELECT/decode after item effects. PostgreSQL decodes RETURNING before report/item
effects and returns that identical dictionary. The validated successor is captured
before Release resolution; other INSERT fields remain late reads. A deliberately
test-owned same-thread resolver mutation discriminates both lookup points. It is
not a concurrent-input or general input-immutability guarantee.

PG stamps awaiting reports before branch selection for every applicable kind.
Legacy closure preserves status and original close timestamp; revise performs no
ordinary terminal lease update. Ordinary terminal closure retains its native lease
update. `complete_lease` subsequently overwrites the generic reason with `settled`
and later inserts its new report, as before. Native datetime types remain explicit.

The oracle has 55 shared actual-backend cases plus 16 PG extensions. Frozen
accepted-source baseline passed 418 local/served tests and 672 PG tests, with 29
registered cleanup checks empty. The unchanged post-extraction selection passed
418 and 672, again with 29 empty checks. Independent oracle/source reviews are
clear; the reviewer independently ran all55 SQLite cases after extraction.
All467 adjacent existing top-level function/class ASTs, both helper signatures,
10 SQL literal values and six protected oracle files remain unchanged.

Actual Python faults after INSERT, decode and native effects preserve provisional
owner writes. SQL trigger faults retain native failure behavior. PG early decode
failure can leave an unbound terminal Decision that its deferred constraint
rejects at COMMIT with23514; failed COMMIT leaves IDLE. SQLite final decode occurs
after binding, so the corresponding caller may commit fully bound state. This
asymmetry is preserved rather than replaced by invented helper cleanup.

Seven finite historical wrong implementations are rejected by the actual oracle:
early SQLite read; PG copied result, late decode, omitted/duplicated report stamp,
and omitted/duplicated lease effect. Separate current saved-successor reread
mutants fail on both backends; positive controls pass. These are finite seeded
controls against original helper bodies, not an exhaustive equivalence proof or
plugins that can be applied unchanged to the extracted delegates.

Initial development measurements incorrectly assumed uniform decode-fault commit
behavior and counted repeated SQLite trace callbacks as owner statements. Those
captures remain historical. Final qualification uses native deferred-constraint
observations and transparent actual execute/decoder proxies. The publication
baseline explicitly projects the immutable native freeze by omitting duplicated
unrelated family histories; raw hashes and selected observations are preserved.

Full local regression passed3043 tests with4 skips and14 performance cases
excluded. Full PG passed1711 tests with88 registered cleanup checks empty.
Exact commands, runtime versions, raw log/XML hashes and candidate bytes appear
in the result packet. These full runs use the final reviewed source bytes.

Each before/full capture retains32 sequential direct-owner observations and four
reused two-key-actor races. Existing caller schedules are rerun evidence, not new
concurrency guarantees. See the [context](../../verification/contexts/locked-decision-writing.json),
[result](../../verification/results/locked-decision-writing-2805.json) and
[actual final observations](../../verification/traces/locked-decision-writing-histories-2805.json).
Temporary SQLite and runner-owned disposable PG are the only fault targets.
No public provider/API/schema/IDL, transaction-composition guarantee, physical
crash/durability, deployed compatibility or parent2150 acceptance is claimed.

The staged secret scanner found86 synthetic advisory claim/idempotency IDs in
the raw before/full captures. Exact file/rule/line fingerprints preserve those
bytes; six actual scanner controls confirm the reviewed matches are ignored while
the same pattern elsewhere and another credential field in each file are detected.
These exceptions are location-scoped; frozen artifact hashes and future review
protect the exact current contents. No hook or scanning rule is disabled.
