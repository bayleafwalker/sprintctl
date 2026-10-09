"""Offline invariants and fixed wire boundary for reserve intent."""
import fcntl
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sprintctl import outbox, reserve_intake as intake, served
from sprintctl.cli import cli
from tests.test_served_authority_sync import _configure_served_repo, _requires_312, _rollout_paths

BASIS = "item:11111111-1111-1111-1111-111111111111@description:v0@sha256:" + "a" * 64 + "@revise:0"


def payload(repo_id="repo"):
    binding = {"repo_id": repo_id, "run_id": "run_" + "A" * 26,
               "principal_id": "principal", "workspace_id": "workspace", "client_id": None, "grant_id": None}
    request = {"schema_version": intake.SCHEMA, "operation": intake.OPERATION,
               "idempotency_key": "reserve-key", "arguments": {"item_id": 1, "actor": "actor",
               "session_id": "session", "expected_revision": BASIS}}
    return request, binding


def capture(path, request, binding):
    return intake.capture(path, (json.dumps(request, indent=2) + "\n").encode(),
                          json.dumps(binding).encode(), repo_id=binding["repo_id"])


def test_raw_bytes_normalized_duplicate_and_append_only(tmp_path):
    path = tmp_path / "producer.db"
    request, binding = payload()
    first = capture(path, request, binding)
    raw = (json.dumps(request, indent=2) + "\n").encode()
    assert first["source_sha256"] == hashlib.sha256(raw).hexdigest()
    request["arguments"].update(role="execution", interrupt_existing=False,
        correlation_ref=None, acceptance_contract={"review_required": True})
    duplicate = capture(path, request, binding)
    assert duplicate["request_id"] == first["request_id"] and duplicate["duplicate"]
    assert duplicate["submitted_source_sha256"] != first["source_sha256"]
    conn = outbox.open_outbox(path)
    assert conn.execute("SELECT source FROM native_reserve_request").fetchone()[0] == raw
    assert conn.execute("SELECT count(*) FROM outbox_record").fetchone()[0] == 0
    intake._attempt(conn, first["request_id"], "interrupted-attempt", intake.RESOLVE, "started")
    for table in intake.TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
    conn.close()


@pytest.mark.parametrize("change", [
    {"item_id": True}, {"item_id": 0}, {"role": None}, {"interrupt_existing": 1},
    {"expected_revision": None}, {"expected_revision": BASIS.rsplit("@", 1)[0]},
    {"unexpected": 1}, {"acceptance_contract": None},
    {"acceptance_contract": {"effect_verification_required": "true"}},
    {"role": "observation", "acceptance_contract": {}},
])
def test_invalid_capture_never_creates_producer_state(tmp_path, change):
    request, binding = payload()
    request["arguments"].update(change)
    path = tmp_path / "producer.db"
    with pytest.raises(ValueError):
        capture(path, request, binding)
    assert not path.exists()


@pytest.mark.parametrize("source", [b'{"x":NaN}', b'{"x":1,"x":2}', b'{"access_token":"secret"}'])
def test_invalid_json_or_credential_shape_never_persisted(tmp_path, source):
    _, binding = payload()
    path = tmp_path / "producer.db"
    with pytest.raises(ValueError):
        intake.capture(path, source, json.dumps(binding).encode(), repo_id="repo")
    assert not path.exists()


@pytest.mark.parametrize("key", ["x", "a" * 7, "a" * 129, "space key", "slash/key", "unicode-é", None])
def test_invalid_owner_key_refuses_before_persistence(tmp_path, key):
    request, binding = payload()
    request["idempotency_key"] = key
    path = tmp_path / "producer.db"
    with pytest.raises(ValueError, match="owner key"):
        capture(path, request, binding)
    assert not path.exists()


@pytest.mark.parametrize("key", ["Aa0._:-x", "a" * 128])
def test_owner_key_boundaries_capture(tmp_path, key):
    request, binding = payload()
    request["idempotency_key"] = key
    assert not capture(tmp_path / "producer.db", request, binding)["duplicate"]


@pytest.mark.parametrize("change", ["content", "run", "grant"])
def test_existing_key_refuses_content_or_binding_replacement(tmp_path, change):
    request, binding = payload()
    path = tmp_path / "producer.db"
    first = capture(path, request, binding)
    if change == "content":
        request["arguments"]["session_id"] = "other"
    elif change == "run":
        binding["run_id"] = "run_" + "B" * 26
    else:
        binding["grant_id"] = "other-grant"
    with pytest.raises(ValueError, match="already captured"):
        capture(path, request, binding)
    assert intake.status(path)["pending_reserve_request_ids"] == [first["request_id"]]


def test_status_is_read_only_and_partial_schema_refuses(tmp_path):
    path = tmp_path / "producer.db"
    assert not intake.status(path)["pending_reserve_request_ids"] and not path.exists()
    request, binding = payload()
    first = capture(path, request, binding)
    with path.with_name(path.name + ".native-reserve.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert intake.status(path)["pending_reserve_request_ids"] == [first["request_id"]]
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE native_reserve_attempt")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="incomplete"):
        intake.status(path)
    with pytest.raises(ValueError, match="incomplete"):
        capture(path, request, binding)


def test_busy_capture_refuses_before_state(tmp_path):
    path = tmp_path / "producer.db"
    request, binding = payload()
    with path.with_name(path.name + ".native-reserve.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="busy"):
            capture(path, request, binding)
    assert not path.exists()


def test_damaged_request_refuses_status_capture_and_sync_before_invocation(tmp_path):
    path = tmp_path / "producer.db"
    request, binding = payload()
    capture(path, request, binding)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER native_reserve_request_update")
    conn.execute("UPDATE native_reserve_request SET source_sha256='damaged'")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="integrity"):
        intake.status(path)
    with pytest.raises(ValueError, match="integrity"):
        capture(path, request, binding)
    with pytest.raises(ValueError, match="integrity"):
        intake.synchronize(path, repo_id="repo", rejection_type=RuntimeError,
            invoke=lambda *a: pytest.fail("damaged request reached native owner"))


@_requires_312
def test_cli_offline_capture_and_batch_sync_do_not_invoke_reserve(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    original_client = served._client
    request, binding = payload(tmp_path.name)
    source, bfile = tmp_path / "request.json", tmp_path / "binding.json"
    source.write_text(json.dumps(request)); bfile.write_text(json.dumps(binding))
    monkeypatch.setattr(served, "_client", lambda *a: pytest.fail("offline capture asked for identity or transport"))
    result = runner.invoke(cli, ["authority", "reserve-queue", "--request", str(source), "--run-binding", str(bfile)])
    assert result.exit_code == 0, result.output
    identity = json.loads(result.output)["request_id"]
    assert intake.status(_rollout_paths(tmp_path).outbox_path)["pending_reserve_request_ids"] == [identity]
    monkeypatch.setattr(served, "_client", original_client)
    monkeypatch.setattr(served, "native_reserve_invoker", lambda *a, **k: pytest.fail("batch silently invoked reserve"))
    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pending_reserve_request_ids"] == [identity]


def test_transport_fixed_allowlist_and_key_placement(monkeypatch):
    calls = []
    async def invoke(profile, operation, arguments, **kwargs):
        kwargs.pop("_credential_resolver")
        calls.append((operation, arguments, kwargs)); return {}
    monkeypatch.setattr(served, "_invoke_operation", invoke)
    call = served.native_reserve_invoker(SimpleNamespace(credential_ref="test-reference"), repo_id="repo")
    for operation, key in [(intake.RESOLVE, None), (intake.OPERATION, "captured"), (intake.RELEASE, None)]:
        call(operation, {"item_id": 1}, key)
    assert calls[1][2] == {"repo_id": "repo", "idempotency_key": "captured"}
    assert all("idempotency_key" not in args for _, args, _ in calls)
    for operation, key in [("work.effect.propose-v1", None), (intake.OPERATION, None), (intake.RELEASE, "key")]:
        with pytest.raises(ValueError):
            call(operation, {}, key)
    assert len(calls) == 3


def test_reserve_transport_pins_one_credential_snapshot_for_pass(monkeypatch):
    resolved, tokens, clients = [], [], []
    def resolve(ref):
        resolved.append(ref)
        return "identity-A" if len(resolved) == 1 else "identity-B"
    class Client:
        def __init__(self, resolver): self.resolver = resolver
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def invoke(self, operation, arguments, **kwargs):
            tokens.append(self.resolver("test-reference")); return {}
    def client(profile, *, credential_resolver):
        obj = Client(credential_resolver); clients.append(obj); return obj
    monkeypatch.setattr(served, "resolve_file_credential", resolve)
    monkeypatch.setattr(served, "_client", client)
    call = served.native_reserve_invoker(SimpleNamespace(credential_ref="test-reference"), repo_id="repo")
    assert not resolved
    for operation, key in [(intake.RESOLVE, None), (intake.OPERATION, "captured"), (intake.RELEASE, None)]:
        call(operation, {}, key)
    assert resolved == ["test-reference"] and tokens == ["identity-A"] * 3
    assert len({id(c) for c in clients}) == 3
