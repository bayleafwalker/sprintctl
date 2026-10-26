# ruff: noqa: E402 - optional Vuoro imports follow explicit availability gates.
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest


httpx = pytest.importorskip(
    "httpx", reason="Vuoro protocol integration extra is absent"
)
pytest.importorskip("vuoro_client", reason="Vuoro client integration extra is absent")
pytest.importorskip("vuoro_service", reason="Vuoro service integration extra is absent")

from sprintctl import application, db
from sprintctl.application import WorkApplication
from sprintctl.vuoro_adapter import register_work_catalog
from vuoro_client import AsyncVuoroClient, Profile
from vuoro_client.errors import InvocationRejectedError
from vuoro_service.app import ServiceSettings, create_app
from vuoro_service.catalog import CatalogRegistry
from vuoro_service.identity import Identity, StaticBearerIdentityResolver
from tests.test_maintenance_capability import AT, CAPABILITY_ID, envelope


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Byte-exact pins on the published served catalog. Any change to them is a
# change to what clients see, so they must be updated in the same commit as
# the catalog change and never re-pinned to make a red test pass.
#
# Q4 basis successor: recomputed against canonical sprintctl da10ace4 with
# vuoro-adapter-kit 0.2.2 and frozen vuoro-service 0.1.94 source 7c0ddee248a8.
# Historical 46/49-operation pins predated a0 and were already stale there.
# Canonical da10 has 86/89 operations; this adds exactly one readonly basis.
# Subtraction retains every da10 operation byte, then the historical dd0 gate.
# Schema 25 adds the ten work.agent.*/work.message.* operations (97/100);
# removing them reproduces the 87/90-operation basis pins byte for byte.
# Schema 26 adds work.run.resolve-v2 and work.approval.{record,get}-v1
# (100/103); removing them reproduces the 97/100 pins byte for byte.
@pytest.mark.parametrize(
    ("remote_schema_version", "operation_count", "byte_count", "operations_sha", "revision"),
    [
        (
            6,
            100,
            241_556,
            "b07a6b35d49ccf559595b2520173e0b75b91582e57ee34e38bcc4408a2652b2f",
            "b07a6b35d49ccf559595b2520173e0b75b91582e57ee34e38bcc4408a2652b2f",
        ),
        (
            7,
            103,
            246_671,
            "20d35fb06b29872edd429ae225999bc0ce58383a0e747af6e2175a58e5d42bb3",
            "9c0e0ca5a2616561488e6682f0b8d89fd97ffa35ddc07e18e0769b45374d6689",
        ),
    ],
)
def test_adapter_kit_migration_preserves_catalog_bytes_and_registry_revision(
    remote_schema_version, operation_count, byte_count, operations_sha, revision, monkeypatch
):
    store = SimpleNamespace(
        repo_id="sprintctl", remote_schema_version=remote_schema_version
    )
    work = WorkApplication(
        repo_id="sprintctl",
        store=store,
        backend=object(),
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry()
    register_work_catalog(registry, work)
    operations = [
        operation.model_dump(mode="json")
        for operation in registry.catalog().operations
    ]
# The schema27 delegation trio, sprint-kind extension, and evidence-entry read
# are additive. New schema-28 operations are excluded by the schema filter in
# these historical schema-6/7 pins.
    # Preserve every historical descriptor byte and registry revision below;
    # never repin the old catalog to make the extension pass.
    extensions = {"work.run.delegate-v1", "work.run.delegation-get-v1",
                  "work.run.delegation-revoke-v1", "work.sprint.kind", "work.evidence.entry-v1",
                  "work.lease.parking-context-v1"}
    assert extensions <= {operation["name"] for operation in operations}
    import sprintctl.vuoro_adapter as adapter
    original_specs = adapter.catalog_operation_specs
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "catalog_operation_specs", lambda **kwargs: [
            spec for spec in original_specs(**kwargs) if spec["name"] not in extensions
        ])
        historical = CatalogRegistry()
        register_work_catalog(historical, work)
    operations = [operation.model_dump(mode="json") for operation in historical.catalog().operations]
    payload = json.dumps(operations, sort_keys=True, separators=(",", ":")).encode()

    assert len(operations) == operation_count
    assert len(payload) == byte_count
    assert hashlib.sha256(payload).hexdigest() == operations_sha
    assert historical.revision == revision
    schema26 = {"work.run.resolve-v2", "work.approval.record-v1", "work.approval.get-v1"}
    inbox_basis = [operation for operation in operations if operation["name"] not in schema26]
    inbox_payload = json.dumps(inbox_basis, sort_keys=True, separators=(",", ":")).encode()
    assert (len(inbox_basis), len(inbox_payload), hashlib.sha256(inbox_payload).hexdigest()) == {
        6: (97, 236630, "eafd1a4415fb2ef0540f7cd36b8e6d92c5dbb7d1b7eb5bb0aafbefc4cd071739"),
        7: (100, 241745, "e6c9ef82b65113eb2ea4d555e3f5ee36eab233a6bcc1f3e174cc18bfe925ba71"),
    }[remote_schema_version]
    from sprintctl.agent_inbox import OPERATIONS as agent_inbox_operations
    basis = [operation for operation in inbox_basis if operation["name"] not in agent_inbox_operations]
    basis_payload = json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    assert (len(basis), len(basis_payload), hashlib.sha256(basis_payload).hexdigest()) == {
        6: (87, 225459, "45465c368b31a6b7c790fdeaf8a0718da46fc48608ade628986ee4ec37db3c28"),
        7: (90, 230574, "60b52ef1fe022176add9b893a538c88aeb676562ec615e7ac02ee716702e8354"),
    }[remote_schema_version]
    predecessor = [operation for operation in basis
                   if operation["name"] != "work.effect.basis-v1"]
    predecessor_baseline = {
        6: (86, 221061, "e47eafd5ae592bf68ccc5185d5acc5412dd5d23b53af6d22b54580c88c2d08c1"),
        7: (89, 226176, "5549b7bd21af079d04687cb23860e60f1941604d7ba34bc58d806939759b2c25"),
    }[remote_schema_version]
    predecessor_payload = json.dumps(predecessor, sort_keys=True, separators=(",", ":")).encode()
    assert (len(predecessor), len(predecessor_payload), hashlib.sha256(predecessor_payload).hexdigest()) == predecessor_baseline
    legacy = [operation for operation in predecessor
              if operation["name"] != "work.effect.export-proposal-v3"]
    baseline = {
        6: (85, 214338, "29fd88288806a8087a8cb40b01963975a30a9be64780b84bd25dda816e92bba4"),
        7: (88, 219453, "1214611a57d0773b8b27490c4fd1d98c846188b2e3cac2209c0c9161fd440f2f"),
    }[remote_schema_version]
    legacy_payload = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    assert (len(legacy), len(legacy_payload), hashlib.sha256(legacy_payload).hexdigest()) == baseline


@pytest.mark.anyio
async def test_generic_client_discovers_and_replays_maintenance_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    connection = db.get_connection(tmp_path / "served-maintenance.db")
    db.init_db(connection)
    work = WorkApplication(
        repo_id="sprintctl", store=connection, backend=db,
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [], list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry()
    register_work_catalog(registry, work)
    app = create_app(
        settings=ServiceSettings(
            environment_name="vuoro-dev",
            environment_class="development",
            compatibility_state="compatible",
        ),
        registry=registry,
        identity_resolver=StaticBearerIdentityResolver(
            {
                "identity": Identity(
                    actor="operator",
                    environment="vuoro-dev",
                    authorities=frozenset({"work:maintenance", "work:maintenance-audit"}),
                    repo_ids=frozenset({"sprintctl"}),
                )
            }
        ),
    )
    prepare_id = "44444444-4444-4444-8444-444444444444"
    recovery_id = "55555555-5555-4555-8555-555555555555"
    frozen_envelope = envelope()
    try:
        async with AsyncVuoroClient(
            Profile("dev", "http://test", "identity-ref", "vuoro-dev"),
            lambda _reference: "identity",
            transport=httpx.ASGITransport(app=app),
        ) as client:
            prepared = await client.invoke(
                "work.maintenance.prepare",
                {"capability_id": CAPABILITY_ID, "envelope": frozen_envelope},
                request_id=prepare_id, idempotency_key=prepare_id,
                repo_id="sprintctl",
            )
            duplicate = await client.invoke(
                "work.maintenance.prepare",
                {"capability_id": CAPABILITY_ID, "envelope": frozen_envelope},
                request_id=prepare_id, idempotency_key=prepare_id,
                repo_id="sprintctl",
            )
            recovery = await client.invoke(
                "work.maintenance.recovery-record",
                {
                    "capability_id": CAPABILITY_ID,
                    "kind": "requested-command",
                    "payload_ref": "artifact:sha256:" + "8" * 64,
                },
                request_id=recovery_id, idempotency_key=recovery_id,
                repo_id="sprintctl",
            )
            status = await client.invoke(
                "work.read.maintenance-capability",
                {"capability_id": CAPABILITY_ID},
                request_id="read-maintenance",
                repo_id="sprintctl",
            )
    finally:
        connection.close()

    assert prepared["state"] == duplicate["state"] == "prepared"
    assert duplicate["duplicate"] is True
    assert recovery["authority"] == "none"
    assert status["capability"]["state"] == "prepared"
    assert "envelope_json" not in status["capability"]


@pytest.mark.anyio
async def test_generic_client_observes_additive_maintenance_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    connection = db.get_connection(tmp_path / "served-maintenance-resource.db")
    db.init_db(connection)
    work = WorkApplication(
        repo_id="sprintctl", store=connection, backend=db,
        ingest_records=lambda records: [], arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [], list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry(); register_work_catalog(registry, work)
    app = create_app(
        settings=ServiceSettings(environment_name="vuoro-dev", environment_class="development", compatibility_state="compatible"),
        registry=registry,
        identity_resolver=StaticBearerIdentityResolver({"identity": Identity(actor="operator", environment="vuoro-dev", authorities=frozenset({"work:maintenance"}), repo_ids=frozenset({"sprintctl"}))}),
    )
    request_id = "66666666-6666-4666-8666-666666666666"
    try:
        async with AsyncVuoroClient(Profile("dev", "http://test", "identity-ref", "vuoro-dev"), lambda _reference: "identity", transport=httpx.ASGITransport(app=app)) as client:
            reference = await client.invoke("work.maintenance.resource.prepare", {"capability_id": CAPABILITY_ID, "envelope": envelope()}, request_id=request_id, idempotency_key=request_id, repo_id="sprintctl")
            assert reference["resource_kind"] == "work.maintenance-capability"
            snapshot = await client.invoke("work.maintenance.resource.get", {"resource_ref": reference["reference"]}, repo_id="sprintctl")
            assert snapshot["state"]["state"] == "prepared" and snapshot["terminal"] is False
            empty = await client.invoke("work.maintenance.resource.changes", {"resource_ref": reference["reference"], "cursor": snapshot["cursor"], "wait_seconds": 0}, repo_id="sprintctl")
            assert empty["events"] == []
    finally:
        connection.close()


@pytest.mark.parametrize("legacy_version", [5, 16])
def test_resource_operations_are_not_published_before_sqlite_schema_17(tmp_path, legacy_version):
    connection = db.get_connection(tmp_path / f"legacy-{legacy_version}.db")
    db.init_db(connection)
    connection.execute("UPDATE schema_version SET version=?", (legacy_version,)); connection.commit()
    work = WorkApplication(
        repo_id="sprintctl", store=connection, backend=db,
        ingest_records=lambda records: [], arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [], list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry(); register_work_catalog(registry, work)
    catalog = registry.catalog()
    names = {operation.name for operation in catalog.operations}
    assert not {name for name in names if name.startswith("work.maintenance.resource.")}
    assert catalog.resource_kinds is None and catalog.observation_transports is None
    connection.close()


@pytest.mark.parametrize("remote_version, expected", [(5, False), (6, False), (7, True)])
def test_resource_operations_follow_remote_schema_capability(remote_version, expected):
    store = type("RemoteStore", (), {"repo_id": "sprintctl", "remote_schema_version": remote_version})()
    work = WorkApplication(
        repo_id="sprintctl", store=store, backend=object(),
        ingest_records=lambda records: [], arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [], list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry(); register_work_catalog(registry, work)
    names = {operation.name for operation in registry.catalog().operations}
    assert bool({name for name in names if name.startswith("work.maintenance.resource.")}) is expected

@pytest.mark.anyio
async def test_maintenance_catalog_denies_broad_work_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    connection = db.get_connection(tmp_path / "served-maintenance-denied.db")
    db.init_db(connection)
    work = WorkApplication(
        repo_id="sprintctl", store=connection, backend=db,
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [], list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry()
    register_work_catalog(registry, work)
    app = create_app(
        settings=ServiceSettings(
            environment_name="vuoro-dev", environment_class="development",
            compatibility_state="compatible",
        ),
        registry=registry,
        identity_resolver=StaticBearerIdentityResolver(
            {"identity": Identity(actor="operator", environment="vuoro-dev", authorities=frozenset({"work:read", "work:lifecycle"}), repo_ids=frozenset({"sprintctl"}))}
        ),
    )
    request_id = "66666666-6666-4666-8666-666666666666"
    try:
        async with AsyncVuoroClient(
            Profile("dev", "http://test", "identity-ref", "vuoro-dev"),
            lambda _reference: "identity", transport=httpx.ASGITransport(app=app),
        ) as client:
            with pytest.raises(InvocationRejectedError) as rejected:
                await client.invoke(
                    "work.maintenance.prepare",
                    {"capability_id": CAPABILITY_ID, "envelope": envelope()},
                    request_id=request_id, idempotency_key=request_id,
                    repo_id="sprintctl",
                )
    finally:
        connection.close()
    assert rejected.value.code == "authority-required"


@pytest.mark.anyio
async def test_generic_client_round_trips_create_edit_show_with_audit(tmp_path):
    connection = db.get_connection(tmp_path / "served-edit.db")
    db.init_db(connection)
    sprint_id = db.create_sprint(connection, "Served edit", status="active")
    work = WorkApplication(
        repo_id="sprintctl",
        store=connection,
        backend=db,
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry()
    register_work_catalog(registry, work)
    app = create_app(
        settings=ServiceSettings(
            environment_name="vuoro-dev",
            environment_class="development",
            compatibility_state="compatible",
        ),
        registry=registry,
        identity_resolver=StaticBearerIdentityResolver(
            {
                "identity": Identity(
                    actor="served-editor",
                    environment="vuoro-dev",
                    authorities=frozenset({"work:lifecycle", "work:read"}),
                    repo_ids=frozenset({"sprintctl"}),
                )
            }
        ),
    )
    try:
        async with AsyncVuoroClient(
            Profile("dev", "http://test", "identity-ref", "vuoro-dev"),
            lambda _reference: "identity",
            transport=httpx.ASGITransport(app=app),
        ) as client:
            created = await client.invoke(
                "work.item.create",
                {
                    "sprint_id": sprint_id,
                    "track_name": "served",
                    "title": "Editable through catalog",
                    "description": "Original scope",
                    "assignee": None,
                    "priority": None,
                },
                request_id="create-editable-item",
                repo_id="sprintctl",
            )
            item_id = created["item"]["id"]
            before = await client.invoke(
                "work.read.item",
                {"item_id": item_id},
                request_id="read-edit-revision",
                repo_id="sprintctl",
            )
            edited = await client.invoke(
                "work.item.edit",
                {
                    "item_id": item_id,
                    "description": "Corrected scope",
                    "expected_revision": before["item"]["edit_revision"],
                },
                request_id="edit-item",
                repo_id="sprintctl",
            )
            after = await client.invoke(
                "work.read.item",
                {"item_id": item_id},
                request_id="show-edited-item",
                repo_id="sprintctl",
            )
    finally:
        connection.close()

    assert edited["item"]["id"] == item_id
    assert edited["item_id"] == item_id
    assert edited["actor"] == "served-editor"
    assert edited["item"]["aggregate_uuid"] == before["item"]["aggregate_uuid"]
    assert after["item"]["description"] == "Corrected scope"
    assert after["item"]["edit_revision"] == edited["revision"]
    assert [event["id"] for event in before["events"]] == [
        event["id"]
        for event in after["events"]
        if event["id"] != edited["event_id"]
    ]
    audit = next(
        event for event in after["events"] if event["id"] == edited["event_id"]
    )
    assert audit["event_type"] == "item-edited"
    assert audit["actor"] == "served-editor"


@pytest.mark.anyio
@pytest.mark.parametrize('authorities,repo_id,allowed', [
    (frozenset({'work:read', 'work:sprint'}), 'sprintctl', True),
    (frozenset({'work:read'}), 'sprintctl', False),
    (frozenset({'work:sprint'}), 'other-repository', False),
])
async def test_served_sprint_kind_authority_and_repository_scope(tmp_path, authorities, repo_id, allowed):
    from sprintctl.sprintcore import sprint_kind_revision
    connection = db.get_connection(tmp_path / 'kind.db')
    db.init_db(connection)
    sid = db.create_sprint(connection, 'Kind authority')
    original = db.get_sprint(connection, sid)
    work = WorkApplication(repo_id='sprintctl', store=connection, backend=db,
                           ingest_records=lambda records: [], arbitrate_command=lambda record, credentials: None,
                           list_records=lambda after, limit: [], list_decisions=lambda after, limit: [])
    registry = CatalogRegistry()
    register_work_catalog(registry, work)
    app = create_app(settings=ServiceSettings(environment_name='vuoro-dev', environment_class='development', compatibility_state='compatible'), registry=registry,
                     identity_resolver=StaticBearerIdentityResolver({'identity': Identity(actor='operator', environment='vuoro-dev', authorities=authorities, repo_ids=frozenset({'sprintctl'}))}))
    try:
        async with AsyncVuoroClient(Profile('dev', 'http://test', 'identity-ref', 'vuoro-dev'), lambda _: 'identity', transport=httpx.ASGITransport(app=app)) as client:
            arguments = {'sprint_id': sid, 'kind': 'archive', 'expected_revision': sprint_kind_revision(original)}
            if allowed:
                result = await client.invoke('work.sprint.kind', arguments, repo_id=repo_id)
                assert result['sprint']['kind'] == 'archive'
                readback = await client.invoke('work.read.sprints', {'include_archive': True}, repo_id=repo_id)
                assert next(s for s in readback['sprints'] if s['id'] == sid)['kind'] == 'archive'
            else:
                with pytest.raises(InvocationRejectedError):
                    await client.invoke('work.sprint.kind', arguments, repo_id=repo_id)
                assert db.get_sprint(connection, sid)['kind'] == original['kind']
    finally:
        connection.close()
