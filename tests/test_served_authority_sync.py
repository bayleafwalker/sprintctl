"""Tests for served-mode ``authority sync`` (#1195 Group C).

``vuoro_client`` is not installed in this environment (see the module
docstring in ``tests/test_served.py``), so these CLI-level tests monkeypatch
``sprintctl.cli._served.batch_apply`` directly rather than faking the
transport layer -- the same pattern ``tests/test_served_lifecycle_routes.py``
uses for the other served routes.
"""

from __future__ import annotations

import json
import sys
from uuid import uuid4

import pytest

import sprintctl.cli as cli_module
from sprintctl import outbox
from sprintctl.cli import cli

_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason=(
        "served mode requires Python 3.12+; this test exercises behavior only "
        "reachable past that version gate"
    ),
)


def _configure_served_repo(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    (tmp_path / "sprintctl.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": str(uuid4())}),
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "vuoro-client-profile/v1",
                "id": "workstation-vuoro-shared",
                "target": {
                    "environment_id": "vuoro-shared",
                    "environment_class": "production",
                    "endpoint": "https://vuoro-shared.example/",
                },
                "credential_ref": "file:~/.config/vuoro/credentials/workstation",
                "production_endpoint_denied": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile_path))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)


def _rollout_paths(tmp_path):
    return cli_module._authority_config.authority_command_paths(cwd=tmp_path)


def _open_producer(tmp_path):
    return outbox.open_outbox(_rollout_paths(tmp_path).outbox_path)


def _append_observation(tmp_path, *, event_type="note.recorded", actor="worker", payload=None):
    producer = _open_producer(tmp_path)
    try:
        return outbox.append_observation(
            producer,
            event_type=event_type,
            actor=actor,
            payload=payload or {"text": "hello"},
        )
    finally:
        producer.close()


def _mint_command(tmp_path, *, record_type, refs, payload, basis_revision="rev-1", actor="worker"):
    rollout_paths = _rollout_paths(tmp_path)
    return cli_module._mint_authority_command_record(
        record_type=record_type,
        actor=actor,
        refs=refs,
        payload=payload,
        basis_revision=basis_revision,
        outbox_path=rollout_paths.outbox_path,
    )


def _claim_refs(claim_id):
    return {
        "repo_id": str(uuid4()),
        "aggregate_type": "claim",
        "aggregate_id": claim_id,
        "claim_id": claim_id,
    }


def _item_refs(item_id):
    return {
        "repo_id": str(uuid4()),
        "aggregate_type": "item",
        "aggregate_uuid": str(uuid4()),
        "aggregate_id": item_id,
    }


def _sprint_refs(sprint_id):
    return {
        "repo_id": str(uuid4()),
        "aggregate_type": "sprint",
        "aggregate_uuid": str(uuid4()),
        "aggregate_id": sprint_id,
    }


def _store_sidecar(tmp_path, *, event_id, credentials, recovery_credential_ref=None):
    cli_module._authority_config.store_pending_authority_credentials(
        _rollout_paths(tmp_path),
        event_id=event_id,
        credentials=credentials,
        recovery_credential_ref=recovery_credential_ref,
    )


def _credential_dir(tmp_path):
    return tmp_path / ".sprintctl" / "authority-credentials"


def _ingest_result(record) -> dict:
    return {
        "kind": "record",
        "event_id": record.event_id,
        "event_type": record.event_type,
        "ingest_offset": 1,
        "duplicate": False,
    }


def _decision_result(record, *, outcome="accepted", reason_code=None, reason_detail=None, effect=None) -> dict:
    return {
        "kind": "decision",
        "event_id": record.event_id,
        "request_event_id": record.event_id,
        "decision_event_id": str(uuid4()),
        "decision_ingest_offset": 1,
        "decision_type": record.event_type,
        "outcome": outcome,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "effect": effect or {},
        "duplicate": False,
    }


# ---------------------------------------------------------------------------
# Observation-only batch flush
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_flushes_observation_only_batch(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    record = _append_observation(tmp_path)

    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        captured["records"] = records
        captured["idempotency_key"] = idempotency_key
        captured["transient_credentials"] = transient_credentials
        return {"repo_id": "repo-x", "results": [_ingest_result(record)]}

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_observation_count"] == 1
    assert payload["decisions"] == []
    assert payload["pending_command_event_ids"] == []
    assert payload["unsupported_command_event_ids"] == []

    assert len(captured["records"]) == 1
    assert captured["records"][0]["event_id"] == record.event_id
    assert captured["transient_credentials"] == {}
    expected_key = cli_module._application.batch_idempotency_key([record])
    assert captured["idempotency_key"] == expected_key


@_requires_312
def test_served_authority_reconcile_uses_served_decision_as_local_receipt(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    command = _mint_command(
        tmp_path,
        record_type="item.done",
        refs=_item_refs(4),
        payload={"to_status": "done"},
    )

    monkeypatch.setattr(
        cli_module._served, "read_records",
        lambda *args, **kwargs: {"records": [{"ingest_offset": 12, "record": cli_module._served_record_argument(command)}]},
    )
    monkeypatch.setattr(
        cli_module._served, "read_decisions",
        lambda *args, **kwargs: {"decisions": [_decision_result(command, outcome="rejected")]},
    )

    result = runner.invoke(cli, ["authority", "reconcile", "--apply", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["served_authoritative"] is True
    assert payload["applied_confirmed"] == 1
    assert cli_module._authority_config.is_terminal_authority_decision(
        _rollout_paths(tmp_path), event_id=command.event_id
    )


@_requires_312
def test_served_authority_reconcile_quarantines_only_old_absent_records(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    first = _mint_command(
        tmp_path, record_type="item.done", refs=_item_refs(4), payload={"to_status": "done"}
    )
    second = _mint_command(
        tmp_path, record_type="item.done", refs=_item_refs(5), payload={"to_status": "done"}
    )
    remote = cli_module._served_record_argument(second)
    monkeypatch.setattr(
        cli_module._served, "read_records",
        lambda *args, **kwargs: {"records": [{"ingest_offset": 12, "record": remote}]},
    )
    monkeypatch.setattr(
        cli_module._served, "read_decisions",
        lambda *args, **kwargs: {"decisions": [_decision_result(second)]},
    )

    result = runner.invoke(cli, ["authority", "reconcile", "--apply", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied_absent"] == 1
    assert payload["applied_confirmed"] == 1
    assert cli_module._authority_config.is_terminal_authority_decision(
        _rollout_paths(tmp_path), event_id=first.event_id
    )


# ---------------------------------------------------------------------------
# Mixed observation + command batch
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_mixed_batch_with_available_credential(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    observation = _append_observation(tmp_path)
    secret = "claim-release-secret"
    ref = cli_module._authority.credential_ref(secret)
    command = _mint_command(
        tmp_path,
        record_type="claim.release",
        refs=_claim_refs(5),
        payload={"claim_id": 5, "credential_ref": ref},
    )
    _store_sidecar(tmp_path, event_id=command.event_id, credentials={ref: secret})

    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        captured["records"] = records
        captured["transient_credentials"] = transient_credentials
        return {
            "repo_id": "repo-x",
            "results": [
                _ingest_result(observation),
                _decision_result(command, effect={"claim_id": 5, "released": True}),
            ],
        }

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_observation_count"] == 1
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["outcome"] == "accepted"
    assert payload["pending_command_event_ids"] == []
    assert payload["unsupported_command_event_ids"] == []

    assert len(captured["records"]) == 2
    assert captured["transient_credentials"] == {ref: secret}

    # Accepted claim.release is not a keep_for_recovery type: sidecar cleared.
    assert list(_credential_dir(tmp_path).glob("*")) == []


# ---------------------------------------------------------------------------
# Stop-at-first-gap semantics
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_stops_at_first_credential_gap(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    observation_before = _append_observation(tmp_path, payload={"text": "before gap"})
    secret = "missing-secret"
    ref = cli_module._authority.credential_ref(secret)
    blocked_command = _mint_command(
        tmp_path,
        record_type="claim.release",
        refs=_claim_refs(6),
        payload={"claim_id": 6, "credential_ref": ref},
    )
    # No sidecar stored for blocked_command -- this is the gap.
    second_command = _mint_command(
        tmp_path,
        record_type="item.transition",
        refs=_item_refs(9),
        payload={"to_status": "active"},
    )
    # An observation appended after the gap must also not be uploaded.
    _append_observation(tmp_path, payload={"text": "after gap"})

    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        captured["records"] = records
        return {"repo_id": "repo-x", "results": [_ingest_result(observation_before)]}

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_observation_count"] == 1
    assert payload["decisions"] == []
    assert set(payload["pending_command_event_ids"]) == {
        blocked_command.event_id,
        second_command.event_id,
    }
    assert payload["unsupported_command_event_ids"] == []

    # Only the pre-gap observation was ever sent to the server.
    assert len(captured["records"]) == 1
    assert captured["records"][0]["event_id"] == observation_before.event_id


# ---------------------------------------------------------------------------
# capability-receipt.accept routed to "unsupported"
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_routes_capability_receipt_accept_to_unsupported(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    unsupported = _mint_command(
        tmp_path,
        record_type="capability-receipt.accept",
        refs=_sprint_refs(2),
        payload={
            "pointer": {
                "project": "sprintctl",
                "receipt_id": "sprintctl.2026-07-14.boundary",
                "receipt_path": (
                    "/projects/dev/_artifacts/sprintctl/capability/receipts/"
                    "sprintctl.2026-07-14.boundary.json"
                ),
                "receipt_sha256": "c" * 64,
            }
        },
    )
    # A regular command after the unsupported record must still be sent --
    # unlike a credential gap, an unsupported record does not block anything.
    followup = _mint_command(
        tmp_path,
        record_type="item.transition",
        refs=_item_refs(3),
        payload={"to_status": "active"},
    )

    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        captured["records"] = records
        return {
            "repo_id": "repo-x",
            "results": [_decision_result(followup)],
        }

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["unsupported_command_event_ids"] == [unsupported.event_id]
    assert payload["pending_command_event_ids"] == []
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["event_id"] == followup.event_id

    # The unsupported record was never sent to the server.
    assert [r["event_id"] for r in captured["records"]] == [followup.event_id]


@_requires_312
def test_served_authority_sync_text_output_reports_unsupported(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    unsupported = _mint_command(
        tmp_path,
        record_type="capability-receipt.accept",
        refs=_sprint_refs(2),
        payload={
            "pointer": {
                "project": "sprintctl",
                "receipt_id": "sprintctl.2026-07-14.boundary",
                "receipt_path": (
                    "/projects/dev/_artifacts/sprintctl/capability/receipts/"
                    "sprintctl.2026-07-14.boundary.json"
                ),
                "receipt_sha256": "c" * 64,
            }
        },
    )
    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not be called: nothing left to send")
        ),
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output
    assert "1 unsupported" in result.output
    assert "capability-receipt.accept is not supported over the served batch operation" in result.output
    assert unsupported.event_id in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


# ---------------------------------------------------------------------------
# keep_for_recovery sidecar retention
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_keeps_sidecar_for_accepted_claim_acquire(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    secret = "claim-acquire-secret"
    ref = cli_module._authority.credential_ref(secret)
    command = _mint_command(
        tmp_path,
        record_type="claim.acquire",
        refs=_item_refs(4),
        payload={
            "agent": "worker",
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 600,
            "credential_ref": ref,
            "metadata": {},
        },
    )
    _store_sidecar(tmp_path, event_id=command.event_id, credentials={ref: secret})

    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: {"repo_id": "repo-x", "results": [_decision_result(command)]},
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output

    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


@_requires_312
def test_served_authority_sync_keeps_sidecar_for_accepted_claim_handoff_rotate(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    old_secret = "old-secret"
    new_secret = "new-secret"
    old_ref = cli_module._authority.credential_ref(old_secret)
    new_ref = cli_module._authority.credential_ref(new_secret)
    command = _mint_command(
        tmp_path,
        record_type="claim.handoff",
        refs=_claim_refs(7),
        payload={
            "claim_id": 7,
            "to_actor": "recipient",
            "mode": "rotate",
            "ttl_seconds": 600,
            "credential_ref": old_ref,
            "proposed_credential_ref": new_ref,
            "metadata": {},
        },
    )
    _store_sidecar(
        tmp_path,
        event_id=command.event_id,
        credentials={old_ref: old_secret, new_ref: new_secret},
        recovery_credential_ref=new_ref,
    )

    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: {"repo_id": "repo-x", "results": [_decision_result(command)]},
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output

    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


@_requires_312
def test_served_authority_sync_clears_sidecar_for_accepted_claim_handoff_transfer(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    secret = "shared-secret"
    ref = cli_module._authority.credential_ref(secret)
    command = _mint_command(
        tmp_path,
        record_type="claim.handoff",
        refs=_claim_refs(8),
        payload={
            "claim_id": 8,
            "to_actor": "recipient",
            "mode": "transfer",
            "ttl_seconds": 600,
            "credential_ref": ref,
            "metadata": {},
        },
    )
    _store_sidecar(tmp_path, event_id=command.event_id, credentials={ref: secret})

    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: {"repo_id": "repo-x", "results": [_decision_result(command)]},
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output

    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_authority_sync_clears_sidecar_on_rejected_decision(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    secret = "claim-acquire-secret"
    ref = cli_module._authority.credential_ref(secret)
    command = _mint_command(
        tmp_path,
        record_type="claim.acquire",
        refs=_item_refs(4),
        payload={
            "agent": "worker",
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 600,
            "credential_ref": ref,
            "metadata": {},
        },
    )
    _store_sidecar(tmp_path, event_id=command.event_id, credentials={ref: secret})

    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: {
            "repo_id": "repo-x",
            "results": [
                _decision_result(
                    command,
                    outcome="rejected",
                    reason_code="invalid-claim-proof",
                    reason_detail="claim proof is invalid",
                )
            ],
        },
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output
    # Even a would-be-recovery-eligible type is cleared once rejected.
    assert list(_credential_dir(tmp_path).glob("*")) == []
    assert cli_module._authority_config.is_terminal_authority_decision(
        _rollout_paths(tmp_path), event_id=command.event_id
    )


@_requires_312
def test_served_authority_sync_skips_a_terminal_rejection_and_replays_followup(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    rejected = _mint_command(
        tmp_path,
        record_type="item.transition",
        refs=_item_refs(4),
        payload={"to_status": "active"},
        actor="wrong-actor",
    )
    followup = _mint_command(
        tmp_path,
        record_type="item.transition",
        refs=_item_refs(5),
        payload={"to_status": "active"},
    )
    cli_module._authority_config.mark_terminal_authority_decision(
        _rollout_paths(tmp_path), event_id=rejected.event_id, outcome="rejected"
    )
    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        captured["event_ids"] = [record["event_id"] for record in records]
        return {"repo_id": "repo-x", "results": [_decision_result(followup)]}

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output
    assert captured["event_ids"] == [followup.event_id]
    assert cli_module._authority_config.is_terminal_authority_decision(
        _rollout_paths(tmp_path), event_id=followup.event_id
    )


# ---------------------------------------------------------------------------
# Batch-size chunking
# ---------------------------------------------------------------------------


@_requires_312
def test_served_authority_sync_splits_batches_by_batch_size_with_separate_keys(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    records = [
        _append_observation(tmp_path, payload={"n": i}) for i in range(3)
    ]

    calls = []

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key, transient_credentials=None):
        calls.append((list(records), idempotency_key))
        return {
            "repo_id": "repo-x",
            "results": [{"kind": "record", "event_id": r["event_id"], "event_type": r["event_type"], "ingest_offset": 1, "duplicate": False} for r in records],
        }

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)

    result = runner.invoke(cli, ["authority", "sync", "--batch-size", "2", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_observation_count"] == 3

    assert len(calls) == 2
    first_records, first_key = calls[0]
    second_records, second_key = calls[1]
    assert len(first_records) == 2
    assert len(second_records) == 1
    assert first_key != second_key
    assert first_key == cli_module._application.batch_idempotency_key(records[:2])
    assert second_key == cli_module._application.batch_idempotency_key(records[2:])


# ---------------------------------------------------------------------------
# served mode does not require enforce mode
# ---------------------------------------------------------------------------


@_requires_312
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_served_authority_sync_does_not_require_enforce_mode(
    runner, tmp_path, monkeypatch, mode
):
    _configure_served_repo(tmp_path, monkeypatch)
    if mode != "off":
        cli_module._authority_config.set_authority_command_mode(mode, cwd=tmp_path)

    monkeypatch.setattr(
        cli_module._served,
        "batch_apply",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not be called: empty outbox")
        ),
    )

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "uploaded_observation_count": 0,
        "decisions": [],
        "pending_command_event_ids": [],
        "unsupported_command_event_ids": [],
    }


def test_local_authority_sync_still_requires_enforce_mode(runner, tmp_path, monkeypatch):
    """Confirms the served gate above does not weaken the local/remote path."""
    monkeypatch.delenv("SPRINTCTL_BACKEND", raising=False)
    (tmp_path / ".git").mkdir()
    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code != 0
    assert "authority sync requires enforce mode" in result.output
