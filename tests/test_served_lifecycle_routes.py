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
