from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from sprintctl import authority, db, outbox
from sprintctl.cli import cli


def _configure_repo(tmp_path) -> None:
    state = tmp_path / ".sprintctl"
    state.mkdir(exist_ok=True)
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    (tmp_path / "sprintctl.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": str(uuid4())}),
        encoding="utf-8",
    )


def test_authority_mode_defaults_off_and_shadow_submit_never_mutates(
    runner,
    conn,
    tmp_path,
):
    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority CLI", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Shadow item")

    initial = runner.invoke(cli, ["authority", "status", "--json"])
    assert initial.exit_code == 0, initial.output
    assert json.loads(initial.output)["mode"] == "off"

    enabled = runner.invoke(cli, ["authority", "mode", "--set", "shadow", "--json"])
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["mode"] == "shadow"

    submitted = runner.invoke(
        cli,
        [
            "authority",
            "submit",
            "--type",
            "item.transition",
            "--aggregate-id",
            str(item_id),
            "--payload",
            '{"to_status":"active"}',
            "--actor",
            "shadow-agent",
            "--json",
        ],
    )
    assert submitted.exit_code == 0, submitted.output
    result = json.loads(submitted.output)
    assert result["status"] == "pending-shadow"
    assert db.get_work_item(conn, item_id)["status"] == "pending"

    producer = outbox.open_outbox(tmp_path / ".sprintctl" / "authority-command-outbox.db")
    try:
        records = outbox.list_records(producer)
    finally:
        producer.close()
    assert [(record.record_class, record.event_type) for record in records] == [
        ("authority-command", "item.transition")
    ]


def test_authority_enforce_rejects_local_backend_before_mutation(runner, conn, tmp_path):
    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority local", status="active")
    assert runner.invoke(cli, ["authority", "mode", "--set", "enforce"]).exit_code == 0

    result = runner.invoke(
        cli,
        [
            "authority",
            "submit",
            "--type",
            "sprint.close",
            "--aggregate-id",
            str(sprint_id),
            "--actor",
            "operator",
        ],
    )

    assert result.exit_code != 0
    assert "requires a remote PostgreSQL backend" in result.output
    assert db.get_sprint(conn, sprint_id)["status"] == "active"


def test_authority_enforce_appends_then_returns_remote_decision(
    runner, conn, tmp_path, monkeypatch
):
    import sprintctl.cli as cli_module

    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority enforce", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Enforced item")
    item = db.get_work_item(conn, item_id)
    fake_store = SimpleNamespace(authority_repo_uuid=None)
    fake_module = SimpleNamespace(get_work_item=lambda _store, _id: item)
    calls = []

    def fake_get_store(obj):
        obj["backend_config"] = SimpleNamespace(mode="remote")
        return fake_store, fake_module

    def fake_arbitrate(store, record, *, credentials):
        calls.append((store, record, credentials))
        return authority.AuthorityDecision(
            request_event_id=record.event_id,
            decision_event_id=str(uuid4()),
            decision_ingest_offset=17,
            decision_type="item.transitioned",
            outcome="accepted",
            reason_code=None,
            reason_detail=None,
            effect={"item_id": item_id, "status": "active"},
        )

    monkeypatch.setattr(cli_module, "_get_store", fake_get_store)
    monkeypatch.setattr(cli_module._authority, "arbitrate_command", fake_arbitrate)
    assert runner.invoke(cli, ["authority", "mode", "--set", "enforce"]).exit_code == 0

    result = runner.invoke(
        cli,
        [
            "authority",
            "submit",
            "--type",
            "item.transition",
            "--aggregate-id",
            str(item_id),
            "--payload",
            '{"to_status":"active"}',
            "--actor",
            "operator",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "accepted"
    assert payload["decision_type"] == "item.transitioned"
    assert len(calls) == 1
    producer = outbox.open_outbox(tmp_path / ".sprintctl" / "authority-command-outbox.db")
    try:
        assert [record.event_id for record in outbox.list_records(producer)] == [
            payload["request_event_id"]
        ]
    finally:
        producer.close()


def test_shadow_claim_acquire_persists_private_recoverable_proof(runner, conn, tmp_path):
    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority proof", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Claim item")
    assert runner.invoke(cli, ["authority", "mode", "--set", "shadow"]).exit_code == 0

    submitted = runner.invoke(
        cli,
        [
            "authority",
            "submit",
            "--type",
            "claim.acquire",
            "--aggregate-id",
            str(item_id),
            "--payload",
            '{"agent":"worker","claim_type":"execute","exclusive":true,'
            '"ttl_seconds":300,"metadata":{}}',
            "--actor",
            "worker",
            "--json",
        ],
    )
    assert submitted.exit_code == 0, submitted.output
    event_id = json.loads(submitted.output)["request_event_id"]

    recovered = runner.invoke(cli, ["authority", "recover-proof", "--event-id", event_id])
    assert recovered.exit_code == 0, recovered.output
    assert recovered.output.strip()
    sidecar = tmp_path / ".sprintctl" / "authority-credentials" / f"{event_id}.json"
    assert sidecar.stat().st_mode & 0o777 == 0o600

    cleared = runner.invoke(cli, ["authority", "clear-proof", "--event-id", event_id])
    assert cleared.exit_code == 0, cleared.output
    assert not sidecar.exists()


def test_shadow_handoff_retains_old_proof_for_retry_and_recovers_only_new_proof(
    runner, conn, tmp_path
):
    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority handoff", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Handoff item")
    claim_id = db.create_claim(conn, item_id, "worker-a")
    old_proof = db.get_claim(conn, claim_id, include_secret=True)["claim_token"]
    new_proof = "pre-minted-rotated-proof"
    assert runner.invoke(cli, ["authority", "mode", "--set", "shadow"]).exit_code == 0

    submitted = runner.invoke(
        cli,
        [
            "authority",
            "submit",
            "--type",
            "claim.handoff",
            "--aggregate-id",
            str(claim_id),
            "--payload",
            '{"to_actor":"worker-b","mode":"rotate","ttl_seconds":300,"metadata":{}}',
            "--actor",
            "worker-a",
            "--claim-token",
            old_proof,
            "--proposed-claim-token",
            new_proof,
            "--json",
        ],
    )
    assert submitted.exit_code == 0, submitted.output
    event_id = json.loads(submitted.output)["request_event_id"]

    sidecar = tmp_path / ".sprintctl" / "authority-credentials" / f"{event_id}.json"
    raw = json.loads(sidecar.read_text())
    assert sorted(raw["credentials"].values()) == sorted([old_proof, new_proof])
    recovered = runner.invoke(cli, ["authority", "recover-proof", "--event-id", event_id])
    assert recovered.exit_code == 0, recovered.output
    assert recovered.output.strip() == new_proof

    producer = outbox.open_outbox(tmp_path / ".sprintctl" / "authority-command-outbox.db")
    try:
        durable_text = json.dumps(outbox.list_records(producer)[0].payload)
    finally:
        producer.close()
    assert old_proof not in durable_text
    assert new_proof not in durable_text
