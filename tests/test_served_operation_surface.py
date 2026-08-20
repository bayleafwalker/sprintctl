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
        "work.batch.apply",
        "work.event.add",
        "work.evidence.ingest",
        "work.handoff.record",
        "work.identity.current",
        "work.item.create",
        "work.item.dep.add",
        "work.item.dep.remove",
        "work.item.edit",
        "work.item.note",
        "work.item.ref.add",
        "work.item.ref.remove",
        "work.lifecycle.arbitrate",
        "work.maintain.check",
        "work.maintenance.prepare",
        "work.maintenance.recovery-record",
        "work.maintenance.resource.changes",
        "work.maintenance.resource.get",
        "work.maintenance.resource.prepare",
        "work.maintenance.transition",
        "work.project.batch",
        "work.project.context",
        "work.project.items",
        "work.project.next-work",
        "work.project.next-work-explain",
        "work.project.sprints",
        "work.read.context",
        "work.read.context-candidates",
        "work.read.decisions",
        "work.read.events",
        "work.read.handoff",
        "work.read.item",
        "work.read.item-projection",
        "work.read.items",
        "work.read.maintenance-capability",
        "work.read.next-work",
        "work.read.next-work-explain",
        "work.read.records",
        "work.read.reservation",
        "work.read.reservations",
        "work.read.sprint",
        "work.read.sprint-detail",
        "work.read.sprints",
        "work.reservation.reassign",
        "work.reservation.release",
        "work.reservation.reserve",
        "work.reservation.touch",
        "work.sprint.create",
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
