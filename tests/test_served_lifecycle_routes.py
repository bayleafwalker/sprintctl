"""Tests for the served-mode item/sprint status routes (#1195 Step 2, Group B)
and the shared authority-command record-construction helper (#1195 Step 1).

``vuoro_client`` is not installed in this environment (see the module
docstring in ``tests/test_served.py``), so these CLI-level tests monkeypatch
``sprintctl.cli._served``'s facade functions directly rather than faking the
transport layer -- the same pattern ``tests/test_authority_cli.py`` uses for
``cli_module._authority.arbitrate_command``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import sprintctl.cli as cli_module
from sprintctl import outbox
from sprintctl.cli import cli


def _configure_served_repo(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
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


def _outbox_records(tmp_path):
    producer = outbox.open_outbox(tmp_path / ".sprintctl" / "authority-command-outbox.db")
    try:
        return outbox.list_records(producer)
    finally:
        producer.close()


# ---------------------------------------------------------------------------
# item status
# ---------------------------------------------------------------------------


def test_served_item_status_active_to_done_appends_item_done_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 7, "aggregate_uuid": str(uuid4()), "status": "active"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, item_id: {"item": item}
    )

    captured = {}

    def fake_lifecycle_arbitrate(profile, *, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "reason_code": None,
            "reason_detail": None,
            "effect": {"item_id": 7, "previous_status": "active", "status": "done"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli,
        ["item", "status", "--id", "7", "--status", "done", "--actor", "worker", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"item_id": 7, "previous": "active", "status": "done"}

    assert captured["record"]["event_type"] == "item.done"
    assert captured["record"]["payload"]["payload"]["to_status"] == "done"
    assert captured["record"]["actor"] == "worker"
    assert captured["record"]["basis_revision"] == f"item:{item['aggregate_uuid']}@status:active"

    records = _outbox_records(tmp_path)
    assert [(r.record_class, r.event_type) for r in records] == [
        ("authority-command", "item.done")
    ]
    assert records[0].event_id == captured["record"]["event_id"]


def test_served_item_status_pending_to_active_uses_item_transition_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 3, "aggregate_uuid": str(uuid4()), "status": "pending"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, item_id: {"item": item}
    )
    captured = {}

    def fake_lifecycle_arbitrate(profile, *, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"item_id": 3, "previous_status": "pending", "status": "active"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli, ["item", "status", "--id", "3", "--status", "active", "--actor", "worker"]
    )
    assert result.exit_code == 0, result.output
    assert "pending -> active" in result.output
    assert captured["record"]["event_type"] == "item.transition"
    assert captured["record"]["payload"]["payload"] == {"to_status": "active"}


def test_served_item_status_rejects_claim_proof_arguments(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    # Neither _served.read_item nor _served.lifecycle_arbitrate should ever be
    # reached: fail-closed happens before any served call.
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli,
        [
            "item", "status", "--id", "1", "--status", "done", "--actor", "worker",
            "--claim-id", "9", "--claim-token", "secret-token",
        ],
    )
    assert result.exit_code != 0
    assert "cannot accept --claim-id/--claim-token" in result.output
    assert _outbox_records(tmp_path) == []


def test_served_item_status_surfaces_a_rejected_decision(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 4, "aggregate_uuid": str(uuid4()), "status": "done"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, item_id: {"item": item}
    )
    monkeypatch.setattr(
        cli_module._served,
        "lifecycle_arbitrate",
        lambda profile, *, record: {
            "outcome": "rejected",
            "reason_code": "invalid-transition",
            "reason_detail": "cannot transition done -> active",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["item", "status", "--id", "4", "--status", "active", "--actor", "worker"]
    )
    assert result.exit_code != 0
    assert "invalid-transition" in result.output


# ---------------------------------------------------------------------------
# sprint status
# ---------------------------------------------------------------------------


def test_served_sprint_status_activate_appends_sprint_activate_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    sprint = {"id": 11, "aggregate_uuid": str(uuid4()), "status": "planned"}
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda profile, **kwargs: {"sprints": [sprint]},
    )
    captured = {}

    def fake_lifecycle_arbitrate(profile, *, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"sprint_id": 11, "previous_status": "planned", "status": "active"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli,
        ["sprint", "status", "--id", "11", "--status", "active", "--actor", "operator", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"sprint_id": 11, "previous": "planned", "status": "active"}
    assert captured["record"]["event_type"] == "sprint.activate"
    assert captured["record"]["payload"]["payload"] == {}
    assert captured["record"]["basis_revision"] == f"sprint:{sprint['aggregate_uuid']}@status:planned"

    records = _outbox_records(tmp_path)
    assert [(r.record_class, r.event_type) for r in records] == [
        ("authority-command", "sprint.activate")
    ]


def test_served_sprint_status_close_surfaces_boundary_event(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    sprint = {"id": 12, "aggregate_uuid": str(uuid4()), "status": "active"}
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda profile, **kwargs: {"sprints": [sprint]},
    )
    monkeypatch.setattr(
        cli_module._served,
        "lifecycle_arbitrate",
        lambda profile, *, record: {
            "outcome": "accepted",
            "effect": {
                "sprint_id": 12,
                "previous_status": "active",
                "status": "closed",
                "boundary_event_id": 99,
                "boundary_revision": "event:99",
            },
        },
    )

    result = runner.invoke(
        cli,
        ["sprint", "status", "--id", "12", "--status", "closed", "--actor", "operator", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["boundary_event_id"] == 99
    assert payload["boundary_revision"] == "event:99"


def test_served_sprint_status_rejects_planned_target_with_no_served_call(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli, ["sprint", "status", "--id", "1", "--status", "planned", "--actor", "operator"]
    )
    assert result.exit_code != 0
    assert "no work.lifecycle.arbitrate mapping" in result.output
    assert _outbox_records(tmp_path) == []


def test_served_sprint_status_not_found(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_sprints", lambda profile, **kwargs: {"sprints": []}
    )

    result = runner.invoke(
        cli, ["sprint", "status", "--id", "42", "--status", "active", "--actor", "operator"]
    )
    assert result.exit_code != 0
    assert "Sprint #42 not found" in result.output


# ---------------------------------------------------------------------------
# Step 1 helper: _mint_authority_command_record / _served_record_argument
# ---------------------------------------------------------------------------


def test_mint_authority_command_record_appends_and_returns_a_durable_record(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli_module, "_detect_runtime_session_id", lambda explicit: "rs-fallback")
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="item.transition",
        actor="worker",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "item",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 5,
        },
        payload={"to_status": "active"},
        basis_revision=f"item:{aggregate_uuid}@status:pending",
        outbox_path=outbox_path,
    )
    assert durable.event_type == "item.transition"
    assert durable.record_class == "authority-command"
    assert durable.origin_seq == 1
    assert durable.runtime_session_id == "rs-fallback"
    assert durable.payload["payload"] == {"to_status": "active"}

    producer = outbox.open_outbox(outbox_path)
    try:
        records = outbox.list_records(producer)
    finally:
        producer.close()
    assert len(records) == 1
    assert records[0].event_id == durable.event_id


def test_mint_authority_command_record_independent_event_and_correlation_ids_when_omitted(
    tmp_path,
):
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="sprint.activate",
        actor="operator",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "sprint",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 1,
        },
        payload={},
        basis_revision=f"sprint:{aggregate_uuid}@status:planned",
        outbox_path=outbox_path,
    )
    # Matches authority_submit's pre-existing behavior when --event-id is
    # omitted: event_id and correlation_id are two independently generated
    # UUIDs, not the same value.
    assert durable.event_id != durable.correlation_id


def test_served_record_argument_matches_record_definition_field_set(tmp_path):
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="sprint.close",
        actor="operator",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "sprint",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 2,
        },
        payload={},
        basis_revision=f"sprint:{aggregate_uuid}@status:active",
        outbox_path=outbox_path,
    )
    record = cli_module._served_record_argument(durable)
    assert set(record) == {
        "origin_stream_id",
        "origin_seq",
        "event_id",
        "schema_version",
        "record_class",
        "event_type",
        "actor",
        "runtime_session_id",
        "occurred_at",
        "basis_revision",
        "correlation_id",
        "causation_id",
        "payload",
        "payload_sha256",
        "created_at",
    }
    assert record["event_id"] == durable.event_id
    assert record["event_type"] == "sprint.close"


# ---------------------------------------------------------------------------
# claim heartbeat / claim release (#1195 Group A, Build A2)
# ---------------------------------------------------------------------------


def _credential_dir(tmp_path):
    return tmp_path / ".sprintctl" / "authority-credentials"


def _stub_claim_context(monkeypatch, *, claim_id, actor, authority_repo_uuid, claim_revision):
    def fake_claim_context(profile, *, claim_id: int):
        return {
            "repo_id": "repo-x",
            "authority_repo_uuid": authority_repo_uuid,
            "actor": actor,
            "claim": {"claim_id": claim_id, "work_item_id": 3, "actor": actor},
            "claim_revision": claim_revision,
        }

    monkeypatch.setattr(cli_module._served, "claim_context", fake_claim_context)


def test_served_claim_heartbeat_mints_claim_renew_with_metadata_parity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    authority_repo_uuid = str(uuid4())
    _stub_claim_context(
        monkeypatch,
        claim_id=9,
        actor="worker-1",
        authority_repo_uuid=authority_repo_uuid,
        claim_revision="claim:9@sha256:" + "a" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = transient_credentials
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 9,
                "work_item_id": 3,
                "actor": "worker-1",
                "expires_at": "2026-07-23T01:05:00Z",
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "9", "--claim-token", "secret-proof",
            "--ttl", "600", "--branch", "feat/x", "--worktree", "/wt",
            "--commit-sha", "abc123", "--pr-ref", "org/repo#1",
            "--runtime-session-id", "rs-1", "--instance-id", "inst-1",
            "--hostname", "host-1", "--pid", "4242", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    record = captured["record"]
    assert record["event_type"] == "claim.renew"
    assert record["actor"] == "worker-1"
    assert record["basis_revision"] == "claim:9@sha256:" + "a" * 64

    payload = record["payload"]["payload"]
    assert payload["claim_id"] == 9
    assert payload["ttl_seconds"] == 600
    assert payload["metadata"] == {
        "runtime_session_id": "rs-1",
        "instance_id": "inst-1",
        "branch": "feat/x",
        "worktree_path": "/wt",
        "commit_sha": "abc123",
        "pr_ref": "org/repo#1",
        "hostname": "host-1",
        "pid": 4242,
    }
    ref = payload["credential_ref"]
    assert captured["transient_credentials"] == {ref: "secret-proof"}

    refs = record["payload"]["refs"]
    assert refs["repo_id"] == authority_repo_uuid
    assert refs["aggregate_type"] == "claim"
    assert refs["claim_id"] == 9

    payload_json = json.loads(result.output)
    assert payload_json["heartbeat_ttl_seconds"] == 600
    assert payload_json["expires_at"] == "2026-07-23T01:05:00Z"

    # Terminal accepted decision clears the retry sidecar.
    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_heartbeat_omits_metadata_when_all_fields_are_none(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=1,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:1@sha256:" + "b" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 1, "expires_at": "2026-07-23T01:05:00Z"},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)
    monkeypatch.setattr(cli_module, "_detect_runtime_session_id", lambda explicit: None)
    monkeypatch.setattr(cli_module, "_detect_hostname", lambda explicit: "detected-host")
    monkeypatch.setattr(cli_module, "_detect_instance_id", lambda explicit: "detected-instance")
    monkeypatch.setattr(cli_module, "_detect_pid", lambda explicit: 1)

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "1", "--claim-token", "secret"]
    )
    assert result.exit_code == 0, result.output
    payload = captured["record"]["payload"]["payload"]
    assert "runtime_session_id" not in payload.get("metadata", {})
    assert "branch" not in payload.get("metadata", {})


def test_served_claim_heartbeat_warns_before_expiry(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=2,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:2@sha256:" + "c" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, record, transient_credentials: {
            "outcome": "accepted",
            "effect": {"claim_id": 2, "expires_at": "2026-07-23T00:00:30Z"},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "2", "--claim-token", "secret",
            "--ttl", "30", "--warn-before-expiry", "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "heartbeat refreshed (ttl=30s, expires=2026-07-23T00:00:30Z)" in result.output
    assert "Warning: claim #2 expires in 30s" in result.output


def test_served_claim_heartbeat_ignores_mismatched_advisory_actor(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=3,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:3@sha256:" + "d" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        return {"outcome": "accepted", "effect": {"claim_id": 3, "expires_at": "x"}}

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "3", "--claim-token", "secret",
            "--actor", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


def test_served_claim_heartbeat_surfaces_a_rejected_decision_and_clears_sidecar(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=4,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:4@sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "invalid-claim-proof",
            "reason_detail": "claim proof is invalid",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "4", "--claim-token", "wrong-secret"]
    )
    assert result.exit_code != 0
    assert "invalid-claim-proof" in result.output
    # A rejected decision is terminal too: the sidecar is cleared, not kept.
    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_heartbeat_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=5,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:5@sha256:" + "f" * 64,
    )

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "5", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "connection reset" in result.output
    # Unknown/transport failure: the sidecar is retained for a retry.
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


def test_served_claim_release_clears_sidecar_on_accepted_decision(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=6,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:6@sha256:" + "1" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = transient_credentials
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 6, "released": True},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "release", "--id", "6", "--claim-token", "secret-proof"]
    )
    assert result.exit_code == 0, result.output
    assert "Claim #6 released." in result.output

    record = captured["record"]
    assert record["event_type"] == "claim.release"
    assert record["actor"] == "worker-1"
    payload = record["payload"]["payload"]
    assert set(payload) == {"claim_id", "credential_ref"}
    assert payload["claim_id"] == 6
    ref = payload["credential_ref"]
    assert captured["transient_credentials"] == {ref: "secret-proof"}

    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_release_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=7,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:7@sha256:" + "2" * 64,
    )

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        raise RuntimeError("timeout")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "release", "--id", "7", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "timeout" in result.output
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


def test_served_claim_release_surfaces_a_rejected_decision(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=8,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:8@sha256:" + "3" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "expired-grant",
            "reason_detail": "claim grant has expired",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["claim", "release", "--id", "8", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "expired-grant" in result.output


def test_served_claim_release_ignores_mismatched_advisory_actor(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=10,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:10@sha256:" + "4" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        return {"outcome": "accepted", "effect": {"claim_id": 10, "released": True}}

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "release", "--id", "10", "--claim-token", "secret",
            "--actor", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


# ---------------------------------------------------------------------------
# claim handoff (#1195 Group A, Build A3)
# ---------------------------------------------------------------------------


def _stub_read_item(monkeypatch, *, item):
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, item_id: {"item": item}
    )


def test_served_claim_handoff_rotate_mints_new_token_and_bumps_lease_epoch(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    authority_repo_uuid = str(uuid4())
    _stub_claim_context(
        monkeypatch,
        claim_id=20,
        actor="worker-1",
        authority_repo_uuid=authority_repo_uuid,
        claim_revision="claim:20@sha256:" + "5" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 3, "sprint_id": 55, "title": "Do the thing"})
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = dict(transient_credentials)
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 20,
                "work_item_id": 3,
                "actor": "recipient-actor",
                "claim_type": "work",
                "exclusive": True,
                "heartbeat": "2026-07-23T01:00:00Z",
                "expires_at": "2026-07-23T01:05:00Z",
                "status": "active",
                "lease_epoch": 2,
                "runtime_session_id": None,
                "instance_id": None,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "20", "--claim-token", "old-secret",
            "--actor", "recipient-actor", "--mode", "rotate", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    record = captured["record"]
    assert record["event_type"] == "claim.handoff"
    assert record["actor"] == "worker-1"  # authenticated actor, not the recipient
    assert record["basis_revision"] == "claim:20@sha256:" + "5" * 64

    payload = record["payload"]["payload"]
    assert payload["claim_id"] == 20
    assert payload["to_actor"] == "recipient-actor"
    assert payload["mode"] == "rotate"
    old_ref = payload["credential_ref"]
    proposed_ref = payload["proposed_credential_ref"]
    assert old_ref != proposed_ref

    creds = captured["transient_credentials"]
    assert set(creds) == {old_ref, proposed_ref}
    assert creds[old_ref] == "old-secret"
    new_token = creds[proposed_ref]
    assert new_token != "old-secret"

    bundle = json.loads(result.output)
    assert bundle["mode"] == "rotate"
    assert bundle["claim"]["claim_token"] == new_token
    assert bundle["claim"]["lease_epoch"] == 2
    assert bundle["item"]["id"] == 3
    assert bundle["sprint_id"] == 55
    assert bundle["performed_by"] == "worker-1"

    # Terminal accepted decision clears the retry sidecar.
    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_handoff_transfer_keeps_token_unchanged(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=21,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:21@sha256:" + "6" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 4, "sprint_id": 56})
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = dict(transient_credentials)
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 21,
                "work_item_id": 4,
                "actor": "recipient-actor",
                "lease_epoch": 1,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "21", "--claim-token", "shared-secret",
            "--actor", "recipient-actor", "--mode", "transfer", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = captured["record"]["payload"]["payload"]
    assert payload["mode"] == "transfer"
    assert "proposed_credential_ref" not in payload
    # Only one credential binding for transfer mode -- no new token minted.
    assert len(captured["transient_credentials"]) == 1

    bundle = json.loads(result.output)
    assert bundle["claim"]["claim_token"] == "shared-secret"


def test_served_claim_handoff_rejects_allow_legacy_adopt(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "claim_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "22", "--claim-token", "secret",
            "--actor", "recipient-actor", "--allow-legacy-adopt",
        ],
    )
    assert result.exit_code != 0
    assert "--allow-legacy-adopt is not supported in served mode" in result.output
    assert _outbox_records(tmp_path) == []


def test_served_claim_handoff_requires_claim_token(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "claim_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli,
        ["claim", "handoff", "--id", "23", "--actor", "recipient-actor"],
    )
    assert result.exit_code != 0
    assert "--claim-token is required in served mode" in result.output
    assert _outbox_records(tmp_path) == []


def test_served_claim_handoff_rejects_wrong_claim_token(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=24,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:24@sha256:" + "7" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "invalid-claim-proof",
            "reason_detail": "claim proof is invalid",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "24", "--claim-token", "wrong-secret",
            "--actor", "recipient-actor",
        ],
    )
    assert result.exit_code != 0
    assert "invalid-claim-proof" in result.output
    # A rejected decision is terminal too: the sidecar is cleared, not kept.
    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_handoff_surfaces_credential_conflict(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=25,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:25@sha256:" + "8" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "credential-conflict",
            "reason_detail": "proposed claim proof is already in use",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "25", "--claim-token", "secret",
            "--actor", "recipient-actor", "--mode", "rotate",
        ],
    )
    assert result.exit_code != 0
    assert "credential-conflict" in result.output
    assert list(_credential_dir(tmp_path).glob("*")) == []


def test_served_claim_handoff_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=26,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:26@sha256:" + "9" * 64,
    )

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "26", "--claim-token", "secret",
            "--actor", "recipient-actor",
        ],
    )
    assert result.exit_code != 0
    assert "connection reset" in result.output
    # Unknown/transport failure: the sidecar is retained for a retry.
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


def test_served_claim_handoff_ignores_mismatched_performed_by(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=27,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:27@sha256:" + "0" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 9, "sprint_id": None})
    captured = {}

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 27, "work_item_id": 9, "actor": "recipient-actor"},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "27", "--claim-token", "secret",
            "--actor", "recipient-actor", "--performed-by", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


def test_served_claim_handoff_degrades_bundle_when_item_fetch_fails(
    runner, tmp_path, monkeypatch
):
    """An already-accepted handoff must not be reported as a failure just
    because the post-acceptance item-detail fetch for the bundle breaks."""
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=31,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:31@sha256:" + "1" * 64,
    )

    def fake_read_item(profile, *, item_id):
        raise RuntimeError("transport blip")

    monkeypatch.setattr(cli_module._served, "read_item", fake_read_item)

    def fake_claim_arbitrate(profile, *, record, transient_credentials):
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 31,
                "work_item_id": 9,
                "actor": "recipient-actor",
                "claim_type": "work",
                "exclusive": True,
                "heartbeat": "2026-07-23T01:00:00Z",
                "expires_at": "2026-07-23T01:05:00Z",
                "status": "active",
                "lease_epoch": 2,
                "runtime_session_id": None,
                "instance_id": None,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "31", "--claim-token", "secret",
            "--actor", "recipient-actor", "--mode", "transfer", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "transport blip" in result.output
    assert "handoff succeeded" in result.output

    # stdout mixes the warning (stderr in real usage, merged here by the
    # runner) with the JSON bundle -- parse just the JSON line.
    json_line = [line for line in result.output.splitlines() if line.startswith("{")][0]
    bundle_start = result.output.index(json_line)
    bundle = json.loads(result.output[bundle_start:])
    assert bundle["item"] is None
    assert bundle["sprint_id"] is None
    assert bundle["claim"]["claim_token"] == "secret"

    # The handoff itself was accepted, so the retry sidecar is still cleared.
    assert list(_credential_dir(tmp_path).glob("*")) == []


# ---------------------------------------------------------------------------
# pilot cutover-evidence (#1211)
# ---------------------------------------------------------------------------


def _fake_cutover_payload(**overrides) -> dict:
    payload = {
        "contract_version": "1",
        "config": {
            "pilot_enabled": True,
            "authority_command_mode": "shadow",
            "projection_reads_enabled": False,
        },
        "parity": None,
        "watermark": {
            "healthy": True,
            "fallback_reason": None,
            "age_seconds": 5,
            "max_age_seconds": 300,
        },
        "stale_tools": {"status": "ok", "incidents": [], "findings": []},
        "rollback_rehearsal": {"rollback_ok": True},
        "promotable": False,
        "blockers": ["parity-not-evaluated"],
    }
    payload.update(overrides)
    return payload


def test_served_cutover_evidence_skip_parity_invokes_operation_with_none_parity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: (_ for _ in ()).throw(
            AssertionError("should not be called when --skip-parity is set")
        ),
    )
    captured = {}

    def fake_cutover_evidence(profile, *, parity, max_watermark_age_seconds, rehearse):
        captured["parity"] = parity
        captured["max_watermark_age_seconds"] = max_watermark_age_seconds
        captured["rehearse"] = rehearse
        return _fake_cutover_payload(parity=None, promotable=True, blockers=[])

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(
        cli, ["pilot", "cutover-evidence", "--skip-parity", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parity"] is None
    assert payload["promotable"] is True

    assert captured["parity"] is None
    assert captured["max_watermark_age_seconds"] == cli_module._cutover.DEFAULT_MAX_WATERMARK_AGE_SECONDS
    assert captured["rehearse"] is True


def test_served_cutover_evidence_pilot_disabled_passes_none_parity_without_error(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: SimpleNamespace(enabled=False),
    )
    captured = {}

    def fake_cutover_evidence(profile, *, parity, max_watermark_age_seconds, rehearse):
        captured["parity"] = parity
        return _fake_cutover_payload(parity=None)

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["parity"] is None


def test_served_cutover_evidence_fails_closed_when_pilot_enabled_and_parity_requested(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        cli_module._served,
        "cutover_evidence",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not be called: no served parity source exists")
        ),
    )

    result = runner.invoke(cli, ["pilot", "cutover-evidence"])
    assert result.exit_code != 0
    assert "cannot compute parity" in result.output
    assert "--skip-parity" in result.output


def test_served_cutover_evidence_passes_max_watermark_age_and_skip_rollback_rehearsal(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_cutover_evidence(profile, *, parity, max_watermark_age_seconds, rehearse):
        captured["max_watermark_age_seconds"] = max_watermark_age_seconds
        captured["rehearse"] = rehearse
        return _fake_cutover_payload(parity=None, rollback_rehearsal=None)

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(
        cli,
        [
            "pilot", "cutover-evidence", "--skip-parity",
            "--max-watermark-age-seconds", "60",
            "--skip-rollback-rehearsal", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_watermark_age_seconds"] == 60
    assert captured["rehearse"] is False
    assert json.loads(result.output)["rollback_rehearsal"] is None


def test_served_cutover_evidence_text_output_matches_local_shape(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "cutover_evidence",
        lambda *a, **k: _fake_cutover_payload(promotable=True, blockers=[]),
    )

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--skip-parity"])
    assert result.exit_code == 0, result.output
    assert "Cutover dogfood evidence (contract v1):" in result.output
    assert "Parity: not evaluated" in result.output
    assert "Promotable: True" in result.output


def test_served_cutover_evidence_surfaces_a_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)

    def fake_cutover_evidence(profile, *, parity, max_watermark_age_seconds, rehearse):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--skip-parity"])
    assert result.exit_code != 0
    assert "connection reset" in result.output
