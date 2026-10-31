"""Guards on the published served operation surface.

The byte-exact catalog pins in ``test_vuoro_work_adapter_integration`` are
the stronger check, but that module skips itself unless httpx, vuoro_client
*and* vuoro_service are all installed, and vuoro_service has no published
wheel -- so on an ordinary checkout it never runs. That is how the catalog
pins silently drifted for two days across the claim -> reservation cutover.

This module pins the same surface at the level that always imports:
``WORK_OPERATION_CONTRACTS`` is a plain tuple in ``sprintctl.vuoro_adapter``
with no Vuoro dependency. It cannot catch a schema-shape change inside one
operation, but it does catch an operation being added, removed, or renamed,
which is what actually drifted.

Adding or removing an operation is a change to what every client sees.
Update this list in the same commit as the change, never to make a red test
pass, and update the byte pins in test_vuoro_work_adapter_integration too.
"""

from __future__ import annotations

from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


PUBLISHED_OPERATIONS = (
        "work.agent.describe-v1",
        "work.agent.list-v1",
        "work.agent.observe-v1",
        "work.agent.register-v1",
        "work.agent.retire-v1",
        "work.approval.get-v1",
        "work.approval.record-v1",
        "work.batch.apply",
        "work.completion.assess-v1",
        "work.decision.record",
        "work.effect.accept-v1",
        "work.effect.acceptance-snapshot-v1",
        "work.effect.attempt-get-v1",
        "work.effect.attempt-open-v1",
        "work.effect.attempt-redeem-v1",
        "work.effect.attempt-report-applied-v1",
        "work.effect.attempt-seal-unused-v1",
        "work.effect.basis-v1",
        "work.effect.export-proposal-v2",
        "work.effect.export-proposal-v3",
        "work.effect.get-v1",
        "work.effect.list-accepted-v1",
        "work.effect.list-proposed-v1",
        "work.effect.mark-applied-v1",
        "work.effect.preview-v1",
        "work.effect.propose-bound-v1",
        "work.effect.propose-v1",
        "work.effect.reject-v1",
        "work.event.add",
        "work.evidence.append-v1",
        "work.evidence.entry-v1",
        "work.evidence.evaluate-v1",
        "work.evidence.ingest",
        "work.evidence.tail-v1",
        "work.handoff.record",
        "work.identity.current",
        "work.item.create",
        "work.item.dep.add",
        "work.item.dep.remove",
        "work.item.edit",
        "work.item.note",
        "work.item.priority",
        "work.item.ref.add",
        "work.item.ref.remove",
        "work.lease.acquire-v1",
        "work.lease.complete-v1",
        "work.lease.heartbeat-v1",
        "work.lease.parking-context-v1",
        "work.lease.read-v1",
        "work.lease.report-outcome-v1",
        "work.lifecycle.arbitrate",
        "work.maintain.check",
        "work.maintenance.prepare",
        "work.maintenance.recovery-record",
        "work.maintenance.resource.changes",
        "work.maintenance.resource.get",
        "work.maintenance.resource.prepare",
        "work.maintenance.transition",
        "work.message.cancel-v1",
        "work.message.read-v1",
        "work.message.receipt-v1",
        "work.message.send-v1",
        "work.message.status-v1",
        "work.project.batch",
        "work.project.context",
        "work.project.items",
        "work.project.next-work",
        "work.project.next-work-explain",
        "work.project.sprints",
        "work.public.item-v1",
        "work.public.list-v1",
        "work.read.context",
        "work.read.context-candidates",
        "work.read.decisions",
        "work.read.events",
        "work.read.handoff",
        "work.read.item",
        "work.read.item-decisions",
        "work.read.item-projection",
        "work.read.items",
        "work.read.maintenance-capability",
        "work.read.next-work",
        "work.read.next-work-explain",
        "work.read.records",
        "work.read.release",
        "work.read.reservation",
        "work.read.reservations",
        "work.read.sprint",
        "work.read.sprint-detail",
        "work.read.sprints",
        "work.read.unbound",
        "work.request.attest-v1",
        "work.request.continuation-receipt-v1",
        "work.request.continuation-v1",
        "work.request.decline-v1",
        "work.request.dependent-add-v1",
        "work.request.dependent-withdraw-v1",
        "work.request.events-v1",
        "work.request.get-v1",
        "work.request.list-v1",
        "work.request.progress-v1",
        "work.request.publish-v1",
        "work.request.report-performed-v1",
        "work.request.revise-v1",
        "work.request.supersede-v1",
        "work.reservation.reassign",
        "work.reservation.release",
        "work.reservation.reserve",
        "work.reservation.reserve-v1",
        "work.reservation.touch",
        "work.run.continuation-delta-v1",
        "work.run.delegate-v1",
        "work.run.delegation-get-v1",
        "work.run.delegation-revoke-v1",
        "work.run.invocation-receipt-get-v1",
        "work.run.predecessor-context-v1",
        "work.run.register-correlated-v1",
        "work.run.register-v1",
        "work.run.resolve-v1",
        "work.run.resolve-v2",
        "work.session-note.write-v1",
        "work.sprint.create",
        "work.sprint.kind",
        "work.validate.item-status-mutation",
)


def test_published_operation_names_are_pinned():
    assert sorted(c.name for c in WORK_OPERATION_CONTRACTS) == list(PUBLISHED_OPERATIONS)


def test_no_retired_surface_reappears():
    """claim and pilot operations were withdrawn; they must not come back."""
    names = {c.name for c in WORK_OPERATION_CONTRACTS}
    assert not [n for n in names if n.startswith("work.claim.")]
    assert not [n for n in names if ".pilot." in n]
    assert not [n for n in names if n in {"work.read.claims", "work.read.claim"}]


def test_every_contract_name_is_unique():
    names = [c.name for c in WORK_OPERATION_CONTRACTS]
    assert len(names) == len(set(names))
