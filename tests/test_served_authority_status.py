"""Served-mode local diagnostics for ordered authority recovery."""

from __future__ import annotations

import json
import sys
from uuid import uuid4

import pytest

import sprintctl.cli as cli_module
from sprintctl import outbox
from sprintctl.cli import cli


_requires_312 = pytest.mark.skipif(sys.version_info < (3, 12), reason="served mode requires Python 3.12+")


def _configure_served_repo(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}), encoding="utf-8"
    )
    (tmp_path / "sprintctl.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": str(uuid4())}), encoding="utf-8"
    )
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({
            "schema_version": "vuoro-client-profile/v1",
            "id": "workstation-vuoro-shared",
            "target": {"environment_id": "vuoro-shared", "environment_class": "production", "endpoint": "https://vuoro-shared.example/"},
            "credential_ref": "file:~/.config/vuoro/credentials/workstation",
            "production_endpoint_denied": False,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)


@_requires_312
def test_served_authority_status_lists_only_unsettled_records(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    paths = cli_module._authority_config.authority_command_paths(cwd=tmp_path)
    settled = cli_module._mint_authority_command_record(
        record_type="item.transition",
        actor="worker",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "item",
            "aggregate_uuid": str(uuid4()),
            "aggregate_id": 1,
        },
        payload={"to_status": "active"},
        basis_revision="rev-1",
        outbox_path=paths.outbox_path,
    )
    producer = outbox.open_outbox(paths.outbox_path)
    try:
        pending = outbox.append_observation(producer, event_type="note.recorded", actor="worker", payload={"text": "pending"})
    finally:
        producer.close()
    cli_module._authority_config.mark_terminal_authority_decision(paths, event_id=settled.event_id, outcome="rejected")

    result = runner.invoke(cli, ["authority", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["outbox_records"] == 2
    assert payload["pending_records"] == [{
        "event_id": pending.event_id,
        "origin_stream_id": pending.origin_stream_id,
        "origin_seq": pending.origin_seq,
        "record_class": "observation",
        "event_type": "note.recorded",
    }]
