# Readonly effect basis v1

`work.effect.basis-v1` accepts exactly `intent_id` and `intent_revision` and
requires `work.effect.get` plus `work:read`. Repository and workspace come from
the actual authenticated context. It runs only on the PostgreSQL owner, using
one sibling `REPEATABLE READ READ ONLY` snapshot. There are no schema changes,
writer grants, migrations, evidence reads, or effect mutations.

The closed `sprintctl-effect-basis/v1` response contains `repo_id`,
`workspace_id`, `intent`, `run`, `work`, and nullable `release`. Intent parameters,
legacy digest, frozen release digest and stored source-run binding use the same
bounded native fields as proposal export v3. Intent adds its actual `state`;
all four recognized states (proposed, accepted, rejected, applied) are data.
No public acceptor, acceptance, applied commit, PR, verification binding, or
public proof fields are exported.

Work has exactly `work_item_id`, `aggregate_uuid`, `status`, `edit_revision`,
`status_revision`, and `item_revision`. Status is the actual stored value, never
inferred by parsing the revision. The full `item_revision` includes the current
revise count. Release has the unchanged v3 Release fields and is validated
against **its own** full revision and work UUID. An edit without a new freeze
can leave an older valid selected Release: both actual revisions are returned.
Null means the native helper found no selected Release, such as after revise.
Current Release and intent's frozen release digest need not be equal.

The protected consumer compares current work edit/full revision, selected
Release, exact proposal parameters and namespace/source-run binding against
its immutable protected copy. These observations provide currentness
corroboration; they never grant acceptance or prove that an effect ran. Work
pending/active can pass this check under explicit consumer policy; blocked
defers effects; done refuses new execution unless the original protected journal
and independently verified forge tuple already establish completion. Unknown
states, malformed data, missing Release, or changed basis refuse; transport
unavailability defers. Public intent state does not approve, deny approval, or
establish protected completion. Existing protected completion remains authoritative
when public corroboration later changes or becomes unavailable.

The endpoint never reads current verification evidence. New, poisoned, or deleted
receipts cannot replace frozen accepted refs or invalidate the protected copy.
Consumers validate every receipt and check map in their own frozen copy.
Required verification and map-only false contracts are returned unchanged even
when no current receipt exists; this operation has no evidence-count gate.

Bounds remain 2 MiB canonical native output, existing 4 MiB raw transport,
depth 32, safe integers, and v3 field/contract/context bounds. Fixed errors are
`effect-basis-unavailable`, `effect-basis-stale`, `effect-basis-invalid`, and
`effect-basis-size-bound`, with existing invalid argument, authority, and backend
errors. SQLite is explicitly unsupported. Existing proposal export v2/v3 bytes
and descriptors remain unchanged. Source qualification uses disposable
PostgreSQL16 and the frozen service protocol catalog; publication alone does not
prove deployment, protected consumer execution, or production migration.
