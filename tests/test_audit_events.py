"""Tests for Step 5: auditctl publisher integration in sprintctl CLI.

Verifies that lifecycle events emit the correct auditctl subprocess calls,
and that auditctl failures do not block the primary sprintctl operation.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sprintctl import db
from sprintctl.cli import cli, _emit_audit_event


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _captured_calls(monkeypatch):
    """Return a list that collects (args, kwargs) for each subprocess.run call."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    import sprintctl.cli as cli_module
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    return calls


def _audit_calls(calls):
    """Filter captured calls to only auditctl add invocations."""
    return [c for c in calls if c and c[0] == "auditctl" and len(c) > 1 and c[1] == "add"]


def test_fake_auditctl_binary_receives_stable_subprocess_contract(tmp_path, monkeypatch):
    capture = tmp_path / "captured.json"
    binary = tmp_path / "auditctl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['AUDITCTL_ARGV_CAPTURE'], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv[1:], handle)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("AUDITCTL_ARGV_CAPTURE", str(capture))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("USER", "publisher-user")

    def run_real_binary(args, **kwargs):
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, process.returncode, stdout=stdout, stderr=stderr)

    import sprintctl.cli as cli_module
    monkeypatch.setattr(cli_module.subprocess, "run", run_real_binary)

    _emit_audit_event(
        "sprint.closed",
        summary="Sprint 42 closed",
        refs=["sprint:42", "ka:7"],
        metadata={
            "sprint_id": 42,
            "event_type": "sprint-closed",
            "boundary_revision": "event:9",
        },
    )

    args = json.loads(capture.read_text(encoding="utf-8"))
    assert args[:2] == ["add", "--type"]
    assert args[args.index("--type") + 1] == "sprint.closed"
    assert args[args.index("--source") + 1] == "sprintctl"
    assert args[args.index("--actor") + 1] == "publisher-user"
    assert [args[index + 1] for index, value in enumerate(args) if value == "--ref"] == [
        "sprint:42",
        "ka:7",
    ]
    assert json.loads(args[args.index("--metadata") + 1]) == {
        "sprint_id": 42,
        "event_type": "sprint-closed",
        "boundary_revision": "event:9",
    }


# ---------------------------------------------------------------------------
# sprint.opened
# ---------------------------------------------------------------------------

class TestSprintOpenedAudit:
    def test_sprint_create_active_emits_opened(self, runner, db_path, monkeypatch):
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, ["sprint", "create", "--name", "S1", "--status", "active"])
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        assert "--type" in audit[0]
        assert audit[0][audit[0].index("--type") + 1] == "sprint.opened"
        assert "--source" in audit[0]
        assert audit[0][audit[0].index("--source") + 1] == "sprintctl"

    def test_sprint_create_planned_does_not_emit(self, runner, db_path, monkeypatch):
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, ["sprint", "create", "--name", "S1", "--status", "planned"])
        assert result.exit_code == 0, result.output
        assert not _audit_calls(calls)

    def test_sprint_status_to_active_emits_opened(self, runner, conn, active_sprint, db_path, monkeypatch):
        # Create a planned sprint to transition
        sid = db.create_sprint(conn, "Planned", "goal", None, None, "planned")
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(
            cli,
            [
                "sprint", "status", "--id", str(sid), "--status", "active",
                "--expected-revision", db.sprint_status_revision(db.get_sprint(conn, sid)),
            ],
        )
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        assert audit[0][audit[0].index("--type") + 1] == "sprint.opened"
        ref_idx = audit[0].index("--ref")
        assert audit[0][ref_idx + 1] == f"sprint:{sid}"


# ---------------------------------------------------------------------------
# sprint.closed
# ---------------------------------------------------------------------------

class TestSprintClosedAudit:
    def test_sprint_status_to_closed_emits_closed(self, runner, conn, active_sprint, db_path, monkeypatch):
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(
            cli,
            [
                "sprint", "status", "--id", str(active_sprint["id"]), "--status", "closed",
                "--expected-revision", db.sprint_status_revision(active_sprint),
            ],
        )
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        assert audit[0][audit[0].index("--type") + 1] == "sprint.closed"
        ref_idx = audit[0].index("--ref")
        assert audit[0][ref_idx + 1] == f"sprint:{active_sprint['id']}"

    def test_sprint_status_to_active_does_not_emit_closed(self, runner, conn, active_sprint, db_path, monkeypatch):
        sid = db.create_sprint(conn, "Planned", "goal", None, None, "planned")
        calls = _captured_calls(monkeypatch)
        runner.invoke(
            cli,
            [
                "sprint", "status", "--id", str(sid), "--status", "active",
                "--expected-revision", db.sprint_status_revision(db.get_sprint(conn, sid)),
            ],
        )
        audit = _audit_calls(calls)
        types = [c[c.index("--type") + 1] for c in audit]
        assert "sprint.closed" not in types


# ---------------------------------------------------------------------------
# sprint.taken_up
# ---------------------------------------------------------------------------

class TestSprintTakenUpAudit:
    def test_takeup_take_emits_taken_up(self, runner, conn, active_sprint, db_path, monkeypatch):
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-1",
        ])
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        cmd = audit[0]
        assert cmd[cmd.index("--type") + 1] == "sprint.taken_up"
        assert cmd[cmd.index("--source") + 1] == "sprintctl"
        ref_idx = cmd.index("--ref")
        assert cmd[ref_idx + 1] == f"sprint:{active_sprint['id']}"
        meta = json.loads(cmd[cmd.index("--metadata") + 1])
        assert meta["sprint_id"] == active_sprint["id"]
        assert meta["event_type"] == "sprint-taken-up"
        assert meta["actor"] == "agent-a"

    def test_takeup_take_conflict_does_not_emit(self, runner, conn, active_sprint, db_path, monkeypatch):
        args = [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-1",
        ]
        runner.invoke(cli, args)
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, args)
        assert result.exit_code == 2
        assert not _audit_calls(calls)


# ---------------------------------------------------------------------------
# sprint.released
# ---------------------------------------------------------------------------

class TestSprintReleasedAudit:
    def test_takeup_release_emits_released(self, runner, conn, active_sprint, db_path, monkeypatch):
        runner.invoke(cli, [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-1",
        ])
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-1",
        ])
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        cmd = audit[0]
        assert cmd[cmd.index("--type") + 1] == "sprint.released"
        assert cmd[cmd.index("--source") + 1] == "sprintctl"
        ref_idx = cmd.index("--ref")
        assert cmd[ref_idx + 1] == f"sprint:{active_sprint['id']}"
        meta = json.loads(cmd[cmd.index("--metadata") + 1])
        assert meta["sprint_id"] == active_sprint["id"]
        assert meta["event_type"] == "sprint-released"
        assert meta["actor"] == "agent-a"

    def test_takeup_release_without_prior_takeup_still_emits(self, runner, conn, active_sprint, db_path, monkeypatch):
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
        ])
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        assert audit[0][audit[0].index("--type") + 1] == "sprint.released"


# ---------------------------------------------------------------------------
# knowledge.landed
# ---------------------------------------------------------------------------

class TestKnowledgeLandedAudit:
    @pytest.mark.parametrize("note_type", [
        "decision", "pattern-noted", "lesson-learned", "risk-accepted",
    ])
    def test_knowledge_note_emits_landed(self, runner, conn, active_sprint, db_path, monkeypatch, note_type):
        item_id = _item(conn, active_sprint["id"])
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(item_id),
            "--type", note_type,
            "--summary", "Test knowledge",
            "--actor", "agent-a",
        ])
        assert result.exit_code == 0, result.output
        audit = _audit_calls(calls)
        assert len(audit) == 1
        cmd = audit[0]
        assert cmd[cmd.index("--type") + 1] == "knowledge.landed"
        assert cmd[cmd.index("--source") + 1] == "sprintctl"
        refs = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--ref"]
        assert any(r.startswith("sprint:") for r in refs)
        assert any(r.startswith("ka:") for r in refs)
        meta = json.loads(cmd[cmd.index("--metadata") + 1])
        assert meta["event_type"] == "knowledge-landed"
        assert meta["note_type"] == note_type
        assert "knowledge_event_id" in meta

    def test_non_knowledge_note_does_not_emit(self, runner, conn, active_sprint, db_path, monkeypatch):
        item_id = _item(conn, active_sprint["id"])
        calls = _captured_calls(monkeypatch)
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(item_id),
            "--type", "update",
            "--summary", "Just a status update",
            "--actor", "agent-a",
        ])
        assert result.exit_code == 0, result.output
        assert not _audit_calls(calls)

    def test_knowledge_note_refs_include_correct_sprint(self, runner, conn, active_sprint, db_path, monkeypatch):
        item_id = _item(conn, active_sprint["id"])
        calls = _captured_calls(monkeypatch)
        runner.invoke(cli, [
            "item", "note",
            "--id", str(item_id),
            "--type", "decision",
            "--summary", "Use event sourcing",
            "--actor", "agent-a",
        ])
        audit = _audit_calls(calls)
        refs = [audit[0][i + 1] for i, v in enumerate(audit[0]) if v == "--ref"]
        assert f"sprint:{active_sprint['id']}" in refs


# ---------------------------------------------------------------------------
# Failure degradation
# ---------------------------------------------------------------------------

class TestAuditFailureDegradation:
    def test_auditctl_failure_does_not_block_sprint_status(self, runner, conn, active_sprint, db_path, monkeypatch):
        import sprintctl.cli as cli_module
        sid = db.create_sprint(conn, "Planned", "goal", None, None, "planned")

        def fail_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"Error: db not found")

        monkeypatch.setattr(cli_module.subprocess, "run", fail_run)
        result = runner.invoke(
            cli,
            [
                "sprint", "status", "--id", str(sid), "--status", "active",
                "--expected-revision", db.sprint_status_revision(db.get_sprint(conn, sid)),
            ],
        )
        assert result.exit_code == 0
        assert "warning: auditctl emit failed" in result.output

    def test_auditctl_exception_does_not_block_takeup_take(self, runner, conn, active_sprint, db_path, monkeypatch):
        import sprintctl.cli as cli_module

        def raise_run(args, **kwargs):
            raise FileNotFoundError("auditctl not found")

        monkeypatch.setattr(cli_module.subprocess, "run", raise_run)
        result = runner.invoke(cli, [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
        ])
        assert result.exit_code == 0
        assert "warning: auditctl emit failed" in result.output

    def test_auditctl_exception_does_not_block_knowledge_note(self, runner, conn, active_sprint, db_path, monkeypatch):
        import sprintctl.cli as cli_module
        item_id = _item(conn, active_sprint["id"])

        def raise_run(args, **kwargs):
            raise FileNotFoundError("auditctl not found")

        monkeypatch.setattr(cli_module.subprocess, "run", raise_run)
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(item_id),
            "--type", "decision",
            "--summary", "Emit or not",
            "--actor", "agent-a",
        ])
        assert result.exit_code == 0
        assert "Recorded note" in result.output
        assert "warning: auditctl emit failed" in result.output
