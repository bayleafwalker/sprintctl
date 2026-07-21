# ruff: noqa: E402 - optional Vuoro imports follow explicit availability gates.
from __future__ import annotations

from types import SimpleNamespace

import pytest


httpx = pytest.importorskip(
    "httpx", reason="Vuoro protocol integration extra is absent"
)
pytest.importorskip("vuoro_client", reason="Vuoro client integration extra is absent")
pytest.importorskip("vuoro_service", reason="Vuoro service integration extra is absent")

from sprintctl import application
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
