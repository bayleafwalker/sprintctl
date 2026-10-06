"""Offline capture and process-level producer invariants, without credentials."""

import fcntl
import hashlib
import json

import pytest

from sprintctl import evidence_intake as intake
from sprintctl import outbox, served
from sprintctl.cli import cli
from tests.test_served_authority_sync import (
    _configure_served_repo,
    _requires_312,
    _rollout_paths,
)


def payload(repo_id="repo"):
    binding = {
        "repo_id": repo_id,
        "run_id": "run_" + "A" * 26,
        "principal_id": "principal",
        "workspace_id": "workspace",
        "client_id": None,
        "grant_id": None,
    }
    arguments = {
        "run_id": binding["run_id"],
        "item_id": "item",
        "kind": "test",
        "ref": "ref",
        "digest": "original-authored-digest",
        "collector": "producer",
        "validity": {},
        "chain_seq": 0,
        "chain_prev_digest": None,
        "idempotency_key": "key",
    }
    return json.dumps(arguments, indent=2).encode(), json.dumps(binding).encode()


def test_original_byte_digests_and_capture_duplicates(tmp_path):
    path = tmp_path / "producer.db"
    source, binding = payload()
    first = intake.capture(path, source, binding, repo_id="repo")
    assert first["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert (
        intake.capture(path, source, binding, repo_id="repo")["request_id"]
        == first["request_id"]
    )
    alternate = intake.capture(path, source + b"\n", binding, repo_id="repo")
    assert alternate["duplicate"]
    assert alternate["source_sha256"] == first["source_sha256"]
    assert alternate["submitted_source_sha256"] != first["source_sha256"]
    conn = outbox.open_outbox(path)
    assert conn.execute("SELECT count(*) FROM outbox_record").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "source", [b'{"run_id": 1, "run_id": 2}', b'{"token": "secret"}', b'{"x": NaN}']
)
def test_invalid_or_credential_shaped_input_never_creates_database(tmp_path, source):
    path = tmp_path / "producer.db"
    _, binding = payload()
    with pytest.raises(ValueError):
        intake.capture(path, source, binding, repo_id="repo")
    assert not path.exists()


def test_busy_producer_stops_before_append(tmp_path):
    path = tmp_path / "producer.db"
    source, binding = payload()
    with path.with_name(path.name + ".native-evidence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="busy"):
            intake.capture(path, source, binding, repo_id="repo")
    assert not path.exists()


def test_started_without_completion_remains_pending_after_reopen(tmp_path):
    path = tmp_path / "producer.db"
    source, binding = payload()
    request = intake.capture(path, source, binding, repo_id="repo")
    conn = outbox.open_outbox(path)
    conn.execute(
        "INSERT INTO native_evidence_attempt(request_id,attempt_id,operation,phase) VALUES (?,?,?,'started')",
        (request["request_id"], "interrupted", intake.OPERATION),
    )
    conn.commit()
    conn.close()
    assert intake.status(path)["pending_evidence_request_ids"] == [
        request["request_id"]
    ]
    with pytest.raises(ValueError, match="key already captured"):
        intake.capture(
            path,
            source + b"\n",
            binding,
            repo_id="repo",
            supersedes=request["request_id"],
        )


@_requires_312
def test_offline_cli_never_constructs_transport_or_resolves_identity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline queue attempted authority or credential access")

    monkeypatch.setattr(served, "_client", forbidden)
    monkeypatch.setattr(served, "identity_current", forbidden)
    source, binding = payload(tmp_path.name)
    (tmp_path / "request.json").write_bytes(source)
    (tmp_path / "binding.json").write_bytes(binding)
    result = runner.invoke(
        cli,
        [
            "authority",
            "evidence-queue",
            "--request",
            str(tmp_path / "request.json"),
            "--run-binding",
            str(tmp_path / "binding.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    request_id = json.loads(result.output)["request_id"]
    result = runner.invoke(cli, ["authority", "evidence-status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pending_evidence_request_ids"] == [request_id]


@_requires_312
def test_batch_sync_reports_native_pending_without_absorbing_it(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    source, binding = payload(tmp_path.name)
    request = intake.capture(
        _rollout_paths(tmp_path).outbox_path, source, binding, repo_id=tmp_path.name
    )
    monkeypatch.setattr(
        served,
        "native_evidence_invoke",
        lambda *a, **k: pytest.fail(
            "batch sync must not silently invoke native intake"
        ),
    )
    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pending_evidence_request_ids"] == [
        request["request_id"]
    ]


def test_transport_refuses_unrelated_operation_before_client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        served, "_client", lambda *a: pytest.fail("unrelated native invocation")
    )
    with pytest.raises(ValueError, match="unsupported"):
        served.native_evidence_invoke(None, "work.effect.propose-v1", {}, repo_id="repo")


def test_run_resolution_chain_code_does_not_authorize_correction(tmp_path):
    from sprintctl.application import ApplicationRejection

    source, binding = payload()
    path = tmp_path / "producer.db"
    first = intake.capture(path, source, binding, repo_id="repo")

    def reject(op, args):
        assert op == "work.run.resolve-v1"
        raise ApplicationRejection(
            "evidence-chain-conflict", "synthetic wrong stage", 409
        )

    report = intake.synchronize(
        path, repo_id="repo", invoke=reject, rejection_type=ApplicationRejection
    )
    assert report["evidence_attempts"][0]["operation"] == "work.run.resolve-v1"
    state = intake.status(path)["evidence_request_states"][0]
    assert state["latest_attempt"] == {
        "phase": "rejected",
        "operation": "work.run.resolve-v1",
        "code": "evidence-chain-conflict",
        "http_status": 409,
    }
    assert state["source_sha256"] == hashlib.sha256(source).hexdigest()
    corrected = {**json.loads(source), "chain_seq": 1}
    with pytest.raises(ValueError, match="key already captured"):
        intake.capture(
            path,
            json.dumps(corrected).encode(),
            binding,
            repo_id="repo",
            supersedes=first["request_id"],
        )


@_requires_312
@pytest.mark.parametrize("failure", [None, "refusal", "reply-loss"])
def test_cli_native_sync_confirms_or_reports_pending(
    runner, tmp_path, monkeypatch, failure
):
    import sys
    from types import ModuleType

    _configure_served_repo(tmp_path, monkeypatch)
    source, binding_source = payload(tmp_path.name)
    arguments, binding = json.loads(source), json.loads(binding_source)
    request = intake.capture(
        _rollout_paths(tmp_path).outbox_path,
        source,
        binding_source,
        repo_id=tmp_path.name,
    )

    class Rejected(RuntimeError):
        code = "evidence-chain-conflict"
        status_code = 409

    package = ModuleType("vuoro_client")
    package.__path__ = []
    errors = ModuleType("vuoro_client.errors")
    errors.InvocationRejectedError = Rejected
    monkeypatch.setitem(sys.modules, "vuoro_client", package)
    monkeypatch.setitem(sys.modules, "vuoro_client.errors", errors)
    calls = []

    def invoke(profile, operation, values, *, repo_id):
        assert repo_id == tmp_path.name
        calls.append((operation, values))
        if operation == "work.run.resolve-v1":
            return binding
        if failure == "refusal":
            raise Rejected("untrusted transport message must not be persisted")
        if failure == "reply-loss":
            raise OSError("unknown")
        item = {
            key: value
            for key, value in values.items()
            if key not in {"run_id", "idempotency_key"}
        }
        return {
            "repo_id": tmp_path.name,
            "run_id": binding["run_id"],
            "item": {**item, "claims": [], "provenance": {}},
        }

    monkeypatch.setattr(served, "native_evidence_invoke", invoke)
    result = runner.invoke(cli, ["authority", "evidence-sync"])
    assert result.exit_code == (1 if failure else 0), result.output
    report = json.loads(result.output.split("\nError:")[0])
    assert [operation for operation, values in calls] == [
        "work.run.resolve-v1",
        intake.OPERATION,
    ]
    assert calls[1][1] == arguments
    assert report["pending_evidence_request_ids"] == (
        [request["request_id"]] if failure else []
    )
    assert report["confirmed_evidence_request_ids"] == (
        [] if failure else [request["request_id"]]
    )
    assert "untrusted transport message" not in result.output


def test_status_is_read_only_without_schema_or_carrier_lock(tmp_path):
    path = tmp_path / "producer.db"
    assert not intake.status(path)["pending_evidence_request_ids"]
    assert not path.exists()
    conn = outbox.open_outbox(path)
    original_tables = [
        row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")
    ]
    conn.close()
    assert not intake.status(path)["pending_evidence_request_ids"]
    conn = outbox.open_outbox(path)
    assert original_tables == [
        row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")
    ]
    conn.close()
    source, binding = payload()
    captured = intake.capture(path, source, binding, repo_id="repo")
    with path.with_name(path.name + ".native-evidence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert intake.status(path)["pending_evidence_request_ids"] == [
            captured["request_id"]
        ]


def test_native_owner_key_scope_refuses_other_run_or_grant(tmp_path):
    path = tmp_path / "producer.db"
    source, binding_source = payload()
    intake.capture(path, source, binding_source, repo_id="repo")
    arguments, binding = json.loads(source), json.loads(binding_source)
    binding["run_id"] = arguments["run_id"] = "run_" + "B" * 26
    with pytest.raises(ValueError, match="another run or grant"):
        intake.capture(
            path,
            json.dumps(arguments).encode(),
            json.dumps(binding).encode(),
            repo_id="repo",
        )
    binding["run_id"] = arguments["run_id"] = "run_" + "A" * 26
    binding["grant_id"] = "changed"
    with pytest.raises(ValueError, match="another run or grant"):
        intake.capture(
            path,
            json.dumps(arguments).encode(),
            json.dumps(binding).encode(),
            repo_id="repo",
        )


@_requires_312
def test_batch_report_survives_unavailable_native_status(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    from tests.test_served_authority_sync import _append_observation

    observation = _append_observation(tmp_path)
    effects = []

    def apply(profile, **kwargs):
        effects.append(kwargs["records"])
        return {"results": [{"kind": "record", "event_id": observation.event_id}]}

    monkeypatch.setattr(served, "batch_apply", apply)
    monkeypatch.setattr(
        intake,
        "status",
        lambda path: (_ for _ in ()).throw(ValueError("unknown native status")),
    )
    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert len(effects) == 1
    assert report["uploaded_observation_count"] == 1
    assert report["pending_evidence_request_ids"] is None
    assert (
        report["native_evidence_status_error"] == "native-evidence-status-unavailable"
    )


@_requires_312
def test_real_served_refusal_allows_cli_tail_correction_and_batch_reports_queue(
    runner, tmp_path, monkeypatch
):
    from vuoro_client import errors

    _configure_served_repo(tmp_path, monkeypatch)
    source, binding_source = payload(tmp_path.name)
    request_path, binding_path = tmp_path / "request.json", tmp_path / "binding.json"
    request_path.write_bytes(source)
    binding_path.write_bytes(binding_source)
    queued = runner.invoke(
        cli,
        [
            "authority",
            "evidence-queue",
            "--request",
            str(request_path),
            "--run-binding",
            str(binding_path),
        ],
    )
    assert queued.exit_code == 0, queued.output
    request_id = json.loads(queued.output)["request_id"]
    batch = runner.invoke(cli, ["authority", "sync", "--json"])
    assert batch.exit_code == 0, batch.output
    assert json.loads(batch.output)["pending_evidence_request_ids"] == [request_id]

    def invoke(profile, operation, arguments, *, repo_id):
        assert repo_id == tmp_path.name
        if operation == "work.run.resolve-v1":
            return json.loads(binding_source)
        raise errors.InvocationRejectedError(
            "evidence-chain-conflict", "owner refusal", status_code=409
        )

    monkeypatch.setattr(served, "native_evidence_invoke", invoke)
    refused = runner.invoke(cli, ["authority", "evidence-sync"])
    assert refused.exit_code == 1, refused.output
    state = intake.status(_rollout_paths(tmp_path).outbox_path)[
        "evidence_request_states"
    ][0]
    assert state["latest_attempt"] == {
        "phase": "rejected",
        "operation": intake.OPERATION,
        "code": "evidence-chain-conflict",
        "http_status": 409,
    }
    request_path.write_text(json.dumps({**json.loads(source), "chain_seq": 1}))
    corrected = runner.invoke(
        cli,
        [
            "authority",
            "evidence-queue",
            "--request",
            str(request_path),
            "--run-binding",
            str(binding_path),
            "--supersedes",
            request_id,
        ],
    )
    assert corrected.exit_code == 0, corrected.output


@_requires_312
def test_native_facade_preserves_real_served_refusal(monkeypatch):
    from vuoro_client.errors import InvocationRejectedError

    refusal = InvocationRejectedError(
        "evidence-chain-conflict", "owner", status_code=409
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def invoke(self, operation, arguments, **kwargs):
            assert operation == intake.OPERATION
            raise refusal

    monkeypatch.setattr(served, "_client", lambda profile: Client())
    with pytest.raises(InvocationRejectedError) as excinfo:
        served.native_evidence_invoke(None, intake.OPERATION, {}, repo_id="repo")
    assert excinfo.value is refusal
    assert excinfo.value.status_code == 409


def test_receipt_numeric_normalization_does_not_merge_booleans():
    assert intake._same_json({"value": [100.0, 1.5]}, {"value": [100, 1.5]})
    assert not intake._same_json({"value": [1]}, {"value": [True]})
    assert not intake._same_json({"value": [100.1]}, {"value": [100]})


@pytest.mark.parametrize("operation", ["work.run.resolve-v1", intake.OPERATION])
def test_native_transport_preserves_explicit_repository_scope(operation, monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def invoke(self, invoked, arguments, **kwargs):
            assert invoked == operation
            assert kwargs["repo_id"] == "agentops"
            assert arguments == {"run_id": "registered-run"}
            return {"scope_observed": kwargs["repo_id"]}

    monkeypatch.setattr(served, "_client", lambda profile: Client())
    result = served.native_evidence_invoke(
        None, operation, {"run_id": "registered-run"}, repo_id="agentops"
    )
    assert result == {"scope_observed": "agentops"}
