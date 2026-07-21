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
from vuoro_service.app import ServiceSettings, create_app
from vuoro_service.catalog import CatalogRegistry
from vuoro_service.identity import Identity, StaticBearerIdentityResolver


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
            )
    finally:
        connection.close()

    assert result["operation"] == "claim_start"
    assert result["claim_token"] == result["claim"]["claim_token"]
    assert result["claim"]["agent"] == "served-claimant"
    assert result["item_status_before"] == "pending"
    assert result["item_status_after"] == "active"
    assert result["status_transition_applied"] is True
