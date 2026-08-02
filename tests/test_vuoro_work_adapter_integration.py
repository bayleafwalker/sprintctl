# ruff: noqa: E402 - optional Vuoro imports follow explicit availability gates.
from __future__ import annotations

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


@pytest.mark.anyio
async def test_preexisting_generic_client_discovers_cutover_evidence(monkeypatch):
    evidence = {
        "contract_version": "1",
        "config": {},
        "parity": None,
        "watermark": {},
        "stale_tools": {},
        "rollback_rehearsal": None,
        "promotable": False,
        "blockers": ["parity-not-evaluated"],
    }
    monkeypatch.setattr(
        application.cutover,
        "build_cutover_evidence",
        lambda **_kwargs: evidence,
    )
    work = WorkApplication(
        repo_id="sprintctl",
        store=None,
        backend=SimpleNamespace(),
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, credentials: None,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )
    registry = CatalogRegistry()
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
                    actor="served-test",
                    environment="vuoro-dev",
                    authorities=frozenset({"work:pilot-read"}),
                    repo_ids=frozenset({"sprintctl"}),
                )
            }
        ),
    )
    async with AsyncVuoroClient(
        Profile("dev", "http://test", "identity-ref", "vuoro-dev"),
        lambda _reference: "identity",
        transport=httpx.ASGITransport(app=app),
    ) as client:
        original = await client.catalog()
        assert original["operations"] == []

        register_work_catalog(registry, work)
        result = await client.invoke(
            "work.pilot.cutover-evidence",
                {"rehearse": False, "max_watermark_age_seconds": 60},
                request_id="old-client-new-work-operation",
                repo_id="sprintctl",
        )

    assert result == evidence


@pytest.mark.anyio
async def test_generic_client_invokes_click_free_claim_start(tmp_path):
    connection = db.get_connection(tmp_path / "served.db")
    db.init_db(connection)
    sprint_id = db.create_sprint(connection, "Served", status="active")
    track_id = db.get_or_create_track(connection, sprint_id, "work")
    item_id = db.create_work_item(
        connection, sprint_id, track_id, "Claim through catalog"
    )
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
                    actor="served-claimant",
                    environment="vuoro-dev",
                    authorities=frozenset({"work:claim"}),
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
            result = await client.invoke(
                "work.claim.start",
                {
                    "item_id": item_id,
                    "runtime_session_id": "served-session",
                    "instance_id": "served-instance",
                    "hostname": "served-host",
                    "pid": 4242,
                    },
                    request_id="generic-client-claim-start",
                    repo_id="sprintctl",
                )
    finally:
        connection.close()

    assert result["operation"] == "claim_start"
    assert result["claim_token"] == result["claim"]["claim_token"]
    assert result["claim"]["agent"] == "served-claimant"
    assert result["item_status_before"] == "pending"
    assert result["item_status_after"] == "active"
    assert result["status_transition_applied"] is True


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
    try:
        async with AsyncVuoroClient(
            Profile("dev", "http://test", "identity-ref", "vuoro-dev"),
            lambda _reference: "identity",
            transport=httpx.ASGITransport(app=app),
        ) as client:
            prepared = await client.invoke(
                "work.maintenance.prepare",
                {"capability_id": CAPABILITY_ID, "envelope": envelope()},
                request_id=prepare_id, idempotency_key=prepare_id,
                repo_id="sprintctl",
            )
            duplicate = await client.invoke(
                "work.maintenance.prepare",
                {"capability_id": CAPABILITY_ID, "envelope": envelope()},
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
