from __future__ import annotations

import json
from uuid import uuid4

from sprintctl import db, outbox
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


def test_authority_repository_uuid_can_be_distinct_from_dispatch_repo_id(
    runner,
    conn,
    tmp_path,
):
    authority_repo_uuid = str(uuid4())
    state = tmp_path / ".sprintctl"
    state.mkdir(exist_ok=True)
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": "example"}),
        encoding="utf-8",
    )
    (tmp_path / "example.dispatch.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repo_id": "example",
                "authority_repo_uuid": authority_repo_uuid,
            }
        ),
        encoding="utf-8",
    )

    sprint_id = db.create_sprint(conn, "Authority CLI", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Canonical manifest")
    assert runner.invoke(cli, ["authority", "mode", "--set", "shadow"]).exit_code == 0

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
            "shadow-agent",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output


def test_authority_repository_uuid_rejects_multiple_root_manifests(
    runner,
    conn,
    tmp_path,
):
    _configure_repo(tmp_path)
    (tmp_path / "other.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": str(uuid4())}),
        encoding="utf-8",
    )
    sprint_id = db.create_sprint(conn, "Authority CLI", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "authority")
    item_id = db.create_work_item(conn, sprint_id, track_id, "Ambiguous manifest")
    assert runner.invoke(cli, ["authority", "mode", "--set", "shadow"]).exit_code == 0

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
            "shadow-agent",
        ],
    )

    assert result.exit_code != 0
    assert "exactly one root *.dispatch.json" in str(result.exception)


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


def test_authority_submit_enforce_is_retired_before_store_or_mutation(
    runner, conn, tmp_path, monkeypatch
):
    import sprintctl.cli as cli_module

    _configure_repo(tmp_path)
    sprint_id = db.create_sprint(conn, "Authority local", status="active")
    assert runner.invoke(cli, ["authority", "mode", "--set", "enforce"]).exit_code == 0
    monkeypatch.setattr(
        cli_module,
        "_get_store",
        lambda _obj: (_ for _ in ()).throw(AssertionError("enforce opened a store")),
    )

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
    assert "authority submit enforce is retired" in result.output
    assert "direct PostgreSQL client" in result.output
    assert db.get_sprint(conn, sprint_id)["status"] == "active"
