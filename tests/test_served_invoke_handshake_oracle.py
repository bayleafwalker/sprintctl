"""Oracle for #2822: handshake before authenticated served operation invocation.

EVIDENCE CLASS: LOCAL TRANSPORT REPLAY, NOT LIVE SERVICE EVIDENCE.
Every request in this module is answered by an in-process
``httpx.MockTransport``. No served backend, network endpoint, or real
credential is contacted; the profile, fixture credential and repository marker
are written to pytest ``tmp_path`` directories and the endpoint is
``https://fixture.invalid``.

What *is* real: the released ``vuoro_client.AsyncVuoroClient`` class (this
oracle is frozen against vuoro-client 0.1.1) and the real Click entrypoint
``sprintctl.cli.cli``. The only substitution is that construction of the real
client class is routed through a factory that injects the recording transport;
every instance is still exactly ``vuoro_client.AsyncVuoroClient``.

The recorder keeps method, path, operation name, argument *keys* and whether
an ``Authorization`` header was present. It never records a header value or a
credential value.

Contract under test (all against current ``sprintctl/served.py`` callers):

1. A matching handshake yields, per client, the request order
   handshake -> catalog -> invocation.
2. Wrong environment, incompatible protocol, malformed handshake body and an
   HTTP error on the handshake each yield no catalog and no invocation
   request, a non-zero exit / raised error, and a closed client.
3. The same real client class serves matching and mismatching environments.
4. One fresh client and one event loop per synchronous call; the client is
   closed on success and on every failure.
5. The attributed mutation path (session attribution + idempotency key) and
   the injected / pinned credential-resolver path also handshake before
   invoking. Credential resolution during the handshake is allowed when the
   profile carries a ``credential_ref``; resolver call counts are not asserted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

httpx = pytest.importorskip("httpx")
vuoro_client = pytest.importorskip("vuoro_client")

from vuoro_client import AsyncVuoroClient as _RealAsyncVuoroClient  # noqa: E402
from vuoro_client import Profile  # noqa: E402
from vuoro_client.errors import ClientIncompatibleError  # noqa: E402

from sprintctl import served  # noqa: E402
from sprintctl.backend import ServedProfile  # noqa: E402
from sprintctl.cli import cli  # noqa: E402

HANDSHAKE = "/api/meta/v1/handshake"
CATALOG = "/api/catalog/v1"
INVOKE = "/api/invoke/v1"
EXPECTED_ENVIRONMENT = "fixture"
ENDPOINT = "https://fixture.invalid"

# Permissive descriptors: the oracle is about request ordering, not schemas.
_OPERATIONS = (
    "work.message.read-v1",
    "work.read.item",
    "work.item.note",
    "work.reservation.reserve-v1",
    "work.evidence.append-v1",
)
_RESULTS = {
    "work.message.read-v1": {"messages": [], "next_after_seq": 0},
}


def _handshake_body(environment: str = EXPECTED_ENVIRONMENT, *, minimum: int = 1, maximum: int = 1):
    return {
        "client_protocol": {"minimum": minimum, "maximum": maximum},
        "service_release": {"distribution": "vuoro-service", "version": "fixture"},
        "environment": {"name": environment, "environment_class": "development"},
    }


def _handshake_response(scenario: str) -> httpx.Response:
    if scenario == "match":
        return httpx.Response(200, json=_handshake_body())
    if scenario == "wrong_environment":
        return httpx.Response(200, json=_handshake_body("wrong-fixture"))
    if scenario == "incompatible_protocol":
        return httpx.Response(200, json=_handshake_body(minimum=90, maximum=99))
    if scenario == "malformed_body":
        return httpx.Response(200, json={"unexpected": "shape"})
    if scenario == "http_error":
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "fixture"}})
    raise AssertionError(f"unknown handshake scenario {scenario!r}")


class _RecordingTransport(httpx.MockTransport):
    """One per client instance; records requests and whether it was closed."""

    def __init__(self, scenario: str, invoke_status: int = 200) -> None:
        self.events: list[dict] = []
        self.closed = False
        self._scenario = scenario
        self._invoke_status = invoke_status
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        event = {
            "method": request.method,
            "path": request.url.path,
            "authenticated": "authorization" in request.headers,  # presence only
        }
        if request.method == "POST":
            payload = json.loads(request.content)
            event["operation"] = payload["operation"]
            event["argument_keys"] = sorted(payload["arguments"])
            event["has_idempotency_key"] = bool(payload.get("idempotency_key"))
            event["session_id_present"] = "session_id" in payload["arguments"]
        self.events.append(event)
        if request.url.path == HANDSHAKE:
            return _handshake_response(self._scenario)
        if request.url.path == CATALOG:
            return httpx.Response(200, json={
                "revision": "fixture",
                "operations": [
                    {"name": name, "input_schema": {}, "result_schema": {}}
                    for name in _OPERATIONS
                ],
            })
        if request.url.path == INVOKE:
            if self._invoke_status != 200:
                return httpx.Response(self._invoke_status, json={
                    "status": "rejected",
                    "error": {"code": "fixture-rejected", "message": "fixture"}})
            operation = event["operation"]
            return httpx.Response(200, json={
                "status": "accepted",
                "result": _RESULTS.get(operation, {"operation": operation})})
        return httpx.Response(404, json={"error": {"code": "not-found", "message": "fixture"}})

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class _ClientLedger:
    def __init__(self, scenario: str, invoke_status: int = 200) -> None:
        self.scenario = scenario
        self.invoke_status = invoke_status
        self.clients: list[dict] = []

    def factory(self, profile, credential_resolver, **kwargs):
        transport = _RecordingTransport(self.scenario, self.invoke_status)
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = None
        client = _RealAsyncVuoroClient(profile, credential_resolver, transport=transport, **kwargs)
        self.clients.append({"client": client, "transport": transport, "loop": loop_id})
        return client

    def paths(self, index: int = -1) -> list[str]:
        return [event["path"] for event in self.clients[index]["transport"].events]

    def all_paths(self) -> list[str]:
        return [event["path"] for entry in self.clients for event in entry["transport"].events]


@pytest.fixture(autouse=True)
def _no_live_backend(monkeypatch):
    for name in ("SPRINTCTL_BACKEND", "SPRINTCTL_VUORO_PROFILE",
                 "CODEX_THREAD_ID", "SPRINTCTL_RUNTIME_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture_profile(tmp_path) -> dict:
    """Temporary profile + fixture credential + repository marker (cwd=tmp_path)."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    credential = secrets / "fixture-credential"
    credential.write_text("fixture-only-not-a-secret")
    credential.chmod(0o600)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "schema_version": "vuoro-client-profile/v1",
        "id": "fixture",
        "revision": 1,
        "source_environment_id": "workstation",
        "target": {"environment_id": EXPECTED_ENVIRONMENT,
                   "environment_class": "development",
                   "endpoint": ENDPOINT},
        "credential_ref": "file:" + str(credential),
        "required_authorities": ["work:read"],
        "production_endpoint_denied": True,
    }))
    marker = Path.cwd() / ".sprintctl"  # conftest chdirs into tmp_path
    marker.mkdir(exist_ok=True)
    (marker / "backend.json").write_text(json.dumps({"backend": "served", "repo_id": "agentops"}))
    served_profile = ServedProfile(
        name="fixture",
        endpoint=ENDPOINT,
        credential_ref="file:" + str(credential),
        expected_environment=EXPECTED_ENVIRONMENT,
        source_path=profile_path,
    )
    return {"path": profile_path, "served_profile": served_profile}


def _install(monkeypatch, scenario: str, invoke_status: int = 200) -> _ClientLedger:
    ledger = _ClientLedger(scenario, invoke_status)
    monkeypatch.setattr(vuoro_client, "AsyncVuoroClient", ledger.factory)
    return ledger


def _invoke_cli(fixture_profile):
    return CliRunner().invoke(
        cli, ["message", "read", "--after-seq", "0", "--limit", "1"],
        env={"SPRINTCTL_BACKEND": "served",
             "SPRINTCTL_VUORO_PROFILE": str(fixture_profile["path"])},
    )


def _capture(call, *args, **kwargs):
    """Run one synchronous facade call; return the raised exception or None."""
    try:
        call(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the oracle inspects it
        return exc
    return None


def _assert_single_closed_real_client(ledger: _ClientLedger) -> None:
    assert len(ledger.clients) == 1
    entry = ledger.clients[0]
    assert type(entry["client"]) is _RealAsyncVuoroClient
    assert entry["loop"] is not None, "client must be built inside the call's event loop"
    assert entry["transport"].closed, "client must be closed"


# --- 1. matching handshake: handshake -> catalog -> invoke (Click entrypoint) ---

def test_cli_matching_handshake_precedes_catalog_and_invoke(monkeypatch, fixture_profile):
    ledger = _install(monkeypatch, "match")
    result = _invoke_cli(fixture_profile)
    assert ledger.paths() == [HANDSHAKE, CATALOG, INVOKE], ledger.paths()
    assert result.exit_code == 0, (result.output, result.exception)
    invoke = ledger.clients[0]["transport"].events[-1]
    assert invoke["operation"] == "work.message.read-v1"
    assert invoke["authenticated"] is True
    _assert_single_closed_real_client(ledger)


# --- 2. failing handshakes: no catalog, no invoke, non-zero, closed ---

_FAILURES = {
    "wrong_environment": "wrong-fixture",
    "incompatible_protocol": "protocol",
    "malformed_body": None,
    "http_error": None,
}


@pytest.mark.parametrize("scenario", sorted(_FAILURES))
def test_cli_failed_handshake_blocks_catalog_and_invoke(monkeypatch, fixture_profile, scenario):
    ledger = _install(monkeypatch, scenario)
    result = _invoke_cli(fixture_profile)
    assert ledger.all_paths() == [HANDSHAKE], ledger.all_paths()
    assert result.exit_code != 0, result.output
    detail = _FAILURES[scenario]
    if detail is not None:
        text = (result.output or "") + " " + str(result.exception or "")
        assert detail in text, text
    _assert_single_closed_real_client(ledger)


# --- 3. the same real client class distinguishes the environments ---

def test_real_client_class_handshake_control():
    """Control (independent of the fix): the released class itself enforces it."""
    profile = Profile(name="fixture", endpoint=ENDPOINT, credential_ref="unused",
                      expected_environment=EXPECTED_ENVIRONMENT)

    async def run(scenario):
        transport = _RecordingTransport(scenario)
        async with _RealAsyncVuoroClient(profile, lambda _ref: "fixture-only",
                                         transport=transport) as client:
            await client.handshake()
        return transport

    transport = asyncio.run(run("match"))
    assert [e["path"] for e in transport.events] == [HANDSHAKE] and transport.closed
    with pytest.raises(ClientIncompatibleError, match="wrong-fixture"):
        asyncio.run(run("wrong_environment"))


def test_cli_uses_one_real_client_class_for_both_environments(monkeypatch, fixture_profile):
    seen = []
    for scenario in ("match", "wrong_environment"):
        ledger = _install(monkeypatch, scenario)
        result = _invoke_cli(fixture_profile)
        seen.append((type(ledger.clients[0]["client"]), result.exit_code, ledger.all_paths()))
    assert {cls for cls, _code, _paths in seen} == {_RealAsyncVuoroClient}
    assert seen[0][1:] == (0, [HANDSHAKE, CATALOG, INVOKE]), seen[0]
    assert seen[1][1] != 0 and seen[1][2] == [HANDSHAKE], seen[1]


# --- 4. per-call client lifecycle ---

def test_fresh_client_and_loop_per_sync_call_closed_on_success_control(monkeypatch, fixture_profile):
    """Control (independent of the fix): lifecycle without ordering assertions."""
    ledger = _install(monkeypatch, "match")
    profile = fixture_profile["served_profile"]
    served.read_item(profile, repo_id="agentops", item_id=1)
    served.read_item(profile, repo_id="agentops", item_id=2)
    assert len(ledger.clients) == 2
    assert ledger.clients[0]["client"] is not ledger.clients[1]["client"]
    assert all(entry["loop"] is not None for entry in ledger.clients)
    assert all(entry["transport"].closed for entry in ledger.clients)


def test_each_sync_call_handshakes_its_own_fresh_client(monkeypatch, fixture_profile):
    ledger = _install(monkeypatch, "match")
    profile = fixture_profile["served_profile"]
    served.read_item(profile, repo_id="agentops", item_id=1)
    served.read_item(profile, repo_id="agentops", item_id=2)
    assert [ledger.paths(i) for i in range(len(ledger.clients))] == [
        [HANDSHAKE, CATALOG, INVOKE], [HANDSHAKE, CATALOG, INVOKE]]
    assert all(entry["transport"].closed for entry in ledger.clients)


@pytest.mark.parametrize("scenario", sorted(_FAILURES))
def test_facade_failed_handshake_raises_and_closes(monkeypatch, fixture_profile, scenario):
    ledger = _install(monkeypatch, scenario)
    raised = _capture(served.read_item, fixture_profile["served_profile"],
                      repo_id="agentops", item_id=1)
    assert ledger.all_paths() == [HANDSHAKE], ledger.all_paths()
    assert raised is not None, "a failed handshake must surface as an error"
    _assert_single_closed_real_client(ledger)


def test_client_closed_when_invocation_is_rejected(monkeypatch, fixture_profile):
    ledger = _install(monkeypatch, "match", invoke_status=500)
    with pytest.raises(vuoro_client.errors.InvocationRejectedError):
        served.read_item(fixture_profile["served_profile"], repo_id="agentops", item_id=1)
    assert ledger.paths() == [HANDSHAKE, CATALOG, INVOKE], ledger.paths()
    _assert_single_closed_real_client(ledger)


# --- 5. attributed mutation path and injected credential resolver path ---

def test_attributed_mutation_handshakes_before_invoke(monkeypatch, fixture_profile):
    monkeypatch.setenv("SPRINTCTL_RUNTIME_SESSION_ID", "fixture-session")
    ledger = _install(monkeypatch, "match")
    served.item_note(fixture_profile["served_profile"], repo_id="agentops", item_id=7,
                     note_type="progress", summary="fixture")
    assert ledger.paths() == [HANDSHAKE, CATALOG, INVOKE], ledger.paths()
    invoke = ledger.clients[0]["transport"].events[-1]
    assert invoke["operation"] == "work.item.note"
    assert invoke["session_id_present"] is True
    _assert_single_closed_real_client(ledger)


def test_attributed_mutation_blocked_by_wrong_environment(monkeypatch, fixture_profile):
    monkeypatch.setenv("SPRINTCTL_RUNTIME_SESSION_ID", "fixture-session")
    ledger = _install(monkeypatch, "wrong_environment")
    raised = _capture(served.item_note, fixture_profile["served_profile"], repo_id="agentops",
                      item_id=7, note_type="progress", summary="fixture")
    assert ledger.all_paths() == [HANDSHAKE], ledger.all_paths()
    assert isinstance(raised, ClientIncompatibleError), raised
    _assert_single_closed_real_client(ledger)


def test_pinned_resolver_idempotent_mutation_handshakes_before_invoke(monkeypatch, fixture_profile):
    ledger = _install(monkeypatch, "match")
    invoke = served.native_reserve_invoker(fixture_profile["served_profile"], repo_id="agentops")
    invoke("work.reservation.reserve-v1", {"item_id": 7}, "fixture-envelope-key")
    assert ledger.paths() == [HANDSHAKE, CATALOG, INVOKE], ledger.paths()
    event = ledger.clients[0]["transport"].events[-1]
    assert event["operation"] == "work.reservation.reserve-v1"
    assert event["has_idempotency_key"] is True
    _assert_single_closed_real_client(ledger)


def test_pinned_resolver_path_blocked_by_wrong_environment(monkeypatch, fixture_profile):
    ledger = _install(monkeypatch, "wrong_environment")
    invoke = served.native_evidence_invoker(fixture_profile["served_profile"], repo_id="agentops")
    raised = _capture(invoke, "work.evidence.append-v1", {"item_id": 7})
    assert ledger.all_paths() == [HANDSHAKE], ledger.all_paths()
    assert isinstance(raised, ClientIncompatibleError), raised
    _assert_single_closed_real_client(ledger)


def test_custom_credential_resolver_handshakes_before_invoke(monkeypatch, fixture_profile):
    """An explicitly injected resolver (the ``_credential_resolver`` seam)."""
    ledger = _install(monkeypatch, "match")
    resolver_refs = []

    def custom_resolver(ref):
        resolver_refs.append(ref == fixture_profile["served_profile"].credential_ref)
        return "fixture-only-not-a-secret"

    asyncio.run(served._invoke_operation(
        fixture_profile["served_profile"], "work.read.item", {"item_id": 1},
        _credential_resolver=custom_resolver, repo_id="agentops"))
    assert ledger.paths() == [HANDSHAKE, CATALOG, INVOKE], ledger.paths()
    assert resolver_refs and all(resolver_refs)
    _assert_single_closed_real_client(ledger)
