"""Tests for the per-repo cutover dogfood evidence packet (item #1163).

Covers the pure/read-mostly module (``sprintctl/cutover.py``) directly with
``repo_root=tmp_path`` (matching the existing pilot/authority-config test
convention) and the ``sprintctl pilot cutover-evidence`` CLI surface with a
repo marker (matching ``tests/test_pilot_cli.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sprintctl import authority_config, cutover, pilot, projection, projection_reads
from sprintctl.cli import cli


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_healthy_pilot_projection(tmp_path) -> None:
    """Enable the pilot and advance its cached projection watermark to now."""
    pilot.set_shadow_pilot_enabled(True, repo_root=tmp_path)
    status = pilot.shadow_pilot_status(repo_root=tmp_path)
    conn = projection.open_cached_projection(status.paths.projection_path)
    try:
        projection.apply_ingested_records(
            conn,
            [
                projection.CachedIngestRecord(
                    ingest_offset=1, record={"event_id": "e-1", "kind": "remote.observation"}
                )
            ],
            advanced_at=_now_iso(),
        )
    finally:
        conn.close()


def _configure_repo_marker(tmp_path) -> None:
    state = tmp_path / ".sprintctl"
    state.mkdir(exist_ok=True)
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": tmp_path.name}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# config_snapshot
# ---------------------------------------------------------------------------

class TestConfigSnapshot:
    def test_defaults_are_all_disabled(self, tmp_path):
        snapshot = cutover.config_snapshot(repo_root=tmp_path)
        assert snapshot == {
            "pilot_enabled": False,
            "authority_command_mode": "off",
            "projection_reads_enabled": False,
        }

    def test_reflects_opted_in_state(self, tmp_path):
        pilot.set_shadow_pilot_enabled(True, repo_root=tmp_path)
        authority_config.set_authority_command_mode("shadow", repo_root=tmp_path)
        projection_reads.set_projection_reads_enabled(True, repo_root=tmp_path)

        snapshot = cutover.config_snapshot(repo_root=tmp_path)
        assert snapshot == {
            "pilot_enabled": True,
            "authority_command_mode": "shadow",
            "projection_reads_enabled": True,
        }

    def test_never_writes_any_file(self, tmp_path):
        cutover.config_snapshot(repo_root=tmp_path)
        assert not (tmp_path / ".sprintctl").exists()


# ---------------------------------------------------------------------------
# rehearse_rollback
# ---------------------------------------------------------------------------

class TestRehearseRollback:
    def test_round_trips_from_disabled_default(self, tmp_path):
        evidence = cutover.rehearse_rollback(repo_root=tmp_path)
        assert evidence["rollback_ok"] is True
        assert evidence["authority_command"] == {
            "before_mode": "off",
            "disabled_mode": "off",
            "restored_mode": "off",
            "round_trip_ok": True,
        }
        assert evidence["projection_reads"] == {
            "before_enabled": False,
            "disabled_enabled": False,
            "restored_enabled": False,
            "round_trip_ok": True,
        }
        # Restores to the exact same effective state it found.
        assert cutover.config_snapshot(repo_root=tmp_path)["authority_command_mode"] == "off"
        assert cutover.config_snapshot(repo_root=tmp_path)["projection_reads_enabled"] is False

    def test_round_trips_from_opted_in_state(self, tmp_path):
        authority_config.set_authority_command_mode("enforce", repo_root=tmp_path)
        projection_reads.set_projection_reads_enabled(True, repo_root=tmp_path)

        evidence = cutover.rehearse_rollback(repo_root=tmp_path)

        assert evidence["rollback_ok"] is True
        assert evidence["authority_command"]["before_mode"] == "enforce"
        assert evidence["authority_command"]["disabled_mode"] == "off"
        assert evidence["authority_command"]["restored_mode"] == "enforce"
        assert evidence["projection_reads"]["before_enabled"] is True
        assert evidence["projection_reads"]["disabled_enabled"] is False
        assert evidence["projection_reads"]["restored_enabled"] is True

        # The dogfood evidence run itself left the operator's chosen state intact.
        snapshot = cutover.config_snapshot(repo_root=tmp_path)
        assert snapshot["authority_command_mode"] == "enforce"
        assert snapshot["projection_reads_enabled"] is True

    def test_disables_mid_rehearsal_before_restoring(self, tmp_path, monkeypatch):
        authority_config.set_authority_command_mode("enforce", repo_root=tmp_path)

        observed_modes = []
        original = authority_config.authority_command_status

        def _spy(*, cwd=None, repo_root=None):
            status = original(cwd=cwd, repo_root=repo_root)
            observed_modes.append(status.mode.value)
            return status

        monkeypatch.setattr(authority_config, "authority_command_status", _spy)
        cutover.rehearse_rollback(repo_root=tmp_path)
        # authority_command_status is read for the initial "before" snapshot,
        # then again by set_authority_command_mode's own return value after
        # each write -- confirm the observed sequence actually disables
        # before restoring, never restoring first.
        assert observed_modes == ["enforce", "off", "enforce"]


# ---------------------------------------------------------------------------
# evaluate_watermark_lag
# ---------------------------------------------------------------------------

class TestEvaluateWatermarkLag:
    def test_missing_projection_reports_missing(self, tmp_path):
        evidence = cutover.evaluate_watermark_lag(repo_root=tmp_path)
        assert evidence["healthy"] is False
        assert evidence["fallback_reason"] == "missing"
        assert evidence["age_seconds"] is None

    def test_fresh_watermark_is_healthy(self, tmp_path):
        _make_healthy_pilot_projection(tmp_path)
        evidence = cutover.evaluate_watermark_lag(repo_root=tmp_path, max_age_seconds=3600)
        assert evidence["healthy"] is True
        assert evidence["fallback_reason"] is None
        assert evidence["watermark_offset"] == 1
        assert evidence["max_age_seconds"] == 3600

    def test_old_watermark_beyond_bound_is_reported_stale(self, tmp_path):
        pilot.set_shadow_pilot_enabled(True, repo_root=tmp_path)
        status = pilot.shadow_pilot_status(repo_root=tmp_path)
        conn = projection.open_cached_projection(status.paths.projection_path)
        try:
            projection.apply_ingested_records(
                conn,
                [
                    projection.CachedIngestRecord(
                        ingest_offset=1, record={"event_id": "e-1", "kind": "remote.observation"}
                    )
                ],
                advanced_at="2020-01-01T00:00:00Z",
            )
        finally:
            conn.close()

        evidence = cutover.evaluate_watermark_lag(repo_root=tmp_path, max_age_seconds=60)
        assert evidence["healthy"] is False
        assert evidence["fallback_reason"] == "stale"
        assert evidence["max_age_seconds"] == 60


# ---------------------------------------------------------------------------
# evaluate_stale_tool_incidents
# ---------------------------------------------------------------------------

class TestEvaluateStaleToolIncidents:
    def test_wraps_doctor_report_shape(self, tmp_path, monkeypatch):
        from sprintctl import doctor as _doctor

        fake_report = {
            "status": "warning",
            "findings": [
                {"code": "x", "severity": "warning", "message": "m", "guidance": []},
            ],
        }
        monkeypatch.setattr(_doctor, "collect_report", lambda *, cwd=None: fake_report)
        evidence = cutover.evaluate_stale_tool_incidents(cwd=tmp_path)
        assert evidence["status"] == "warning"
        assert evidence["incidents"] == []  # only "error" severity counts as an incident
        assert evidence["findings"] == fake_report["findings"]

    def test_error_findings_become_incidents(self, tmp_path, monkeypatch):
        from sprintctl import doctor as _doctor

        fake_report = {
            "status": "error",
            "findings": [
                {"code": "x", "severity": "error", "message": "m", "guidance": []},
                {"code": "y", "severity": "warning", "message": "n", "guidance": []},
            ],
        }
        monkeypatch.setattr(_doctor, "collect_report", lambda *, cwd=None: fake_report)
        evidence = cutover.evaluate_stale_tool_incidents(cwd=tmp_path)
        assert len(evidence["incidents"]) == 1
        assert evidence["incidents"][0]["code"] == "x"


# ---------------------------------------------------------------------------
# build_cutover_evidence / promotion gate
# ---------------------------------------------------------------------------

class TestBuildCutoverEvidence:
    def _no_incidents(self, monkeypatch):
        from sprintctl import doctor as _doctor

        monkeypatch.setattr(
            _doctor, "collect_report", lambda *, cwd=None: {"status": "ok", "findings": []}
        )

    def test_defaults_are_not_promotable_with_full_blocker_list(self, tmp_path, monkeypatch):
        self._no_incidents(monkeypatch)
        evidence = cutover.build_cutover_evidence(repo_root=tmp_path)
        assert evidence["contract_version"] == "1"
        assert evidence["promotable"] is False
        assert "pilot-not-enabled" in evidence["blockers"]
        assert "parity-not-evaluated" in evidence["blockers"]
        assert "watermark-missing" in evidence["blockers"]
        assert "rollback-rehearsal-failed" not in evidence["blockers"]
        assert evidence["rollback_rehearsal"]["rollback_ok"] is True

    def test_all_green_evidence_is_promotable(self, tmp_path, monkeypatch):
        self._no_incidents(monkeypatch)
        _make_healthy_pilot_projection(tmp_path)
        parity = {"is_equal": True, "counts": {"equal": 1, "mismatched": 0, "missing": 0, "unexpected": 0}}

        evidence = cutover.build_cutover_evidence(
            repo_root=tmp_path, parity=parity, max_watermark_age_seconds=3600
        )

        assert evidence["blockers"] == []
        assert evidence["promotable"] is True

    def test_diverged_parity_blocks_promotion(self, tmp_path, monkeypatch):
        self._no_incidents(monkeypatch)
        _make_healthy_pilot_projection(tmp_path)
        parity = {"is_equal": False, "counts": {"equal": 0, "mismatched": 1, "missing": 0, "unexpected": 0}}

        evidence = cutover.build_cutover_evidence(
            repo_root=tmp_path, parity=parity, max_watermark_age_seconds=3600
        )
        assert "parity-diverged" in evidence["blockers"]
        assert evidence["promotable"] is False

    def test_stale_tool_incidents_block_promotion(self, tmp_path, monkeypatch):
        from sprintctl import doctor as _doctor

        monkeypatch.setattr(
            _doctor,
            "collect_report",
            lambda *, cwd=None: {
                "status": "error",
                "findings": [{"code": "x", "severity": "error", "message": "m", "guidance": []}],
            },
        )
        _make_healthy_pilot_projection(tmp_path)
        parity = {"is_equal": True, "counts": {}}
        evidence = cutover.build_cutover_evidence(
            repo_root=tmp_path, parity=parity, max_watermark_age_seconds=3600
        )
        assert "stale-tool-incidents" in evidence["blockers"]
        assert evidence["promotable"] is False

    def test_skip_rehearsal_omits_rollback_and_never_blocks_on_it(self, tmp_path, monkeypatch):
        self._no_incidents(monkeypatch)
        evidence = cutover.build_cutover_evidence(repo_root=tmp_path, rehearse=False)
        assert evidence["rollback_rehearsal"] is None
        assert "rollback-rehearsal-failed" not in evidence["blockers"]

    def test_rehearsal_never_leaves_repository_in_different_config(self, tmp_path, monkeypatch):
        self._no_incidents(monkeypatch)
        pilot.set_shadow_pilot_enabled(True, repo_root=tmp_path)
        authority_config.set_authority_command_mode("shadow", repo_root=tmp_path)

        before = cutover.config_snapshot(repo_root=tmp_path)
        cutover.build_cutover_evidence(repo_root=tmp_path)
        after = cutover.config_snapshot(repo_root=tmp_path)

        assert before == after


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestCutoverEvidenceCLI:
    def test_json_shape_with_no_pilot_configured(self, runner, tmp_path):
        _configure_repo_marker(tmp_path)
        result = runner.invoke(cli, ["pilot", "cutover-evidence", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["contract_version"] == "1"
        assert data["promotable"] is False
        assert "pilot-not-enabled" in data["blockers"]
        assert data["parity"] is None  # pilot never enabled, so parity is skipped

    def test_skip_rollback_rehearsal_flag(self, runner, tmp_path):
        _configure_repo_marker(tmp_path)
        result = runner.invoke(
            cli, ["pilot", "cutover-evidence", "--skip-rollback-rehearsal", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["rollback_rehearsal"] is None

    def test_text_output_reports_promotable_and_blockers(self, runner, tmp_path):
        _configure_repo_marker(tmp_path)
        result = runner.invoke(cli, ["pilot", "cutover-evidence"])
        assert result.exit_code == 0, result.output
        assert "Promotable: False" in result.output
        assert "Blockers:" in result.output

    def test_parity_computed_when_pilot_enabled_and_sprint_available(
        self, runner, conn, active_sprint, tmp_path
    ):
        _configure_repo_marker(tmp_path)
        assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
        added = runner.invoke(
            cli,
            [
                "event", "add", "--sprint-id", str(active_sprint["id"]),
                "--type", "work.completed", "--actor", "agent-a",
                "--payload", '{"summary":"cutover dogfood evidence"}', "--json",
            ],
        )
        assert added.exit_code == 0, added.output

        result = runner.invoke(
            cli,
            [
                "pilot", "cutover-evidence",
                "--sprint-id", str(active_sprint["id"]),
                "--skip-rollback-rehearsal",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["parity"] is not None
        assert data["parity"]["is_equal"] is True

    def test_skip_parity_flag_omits_parity(self, runner, conn, active_sprint, tmp_path):
        _configure_repo_marker(tmp_path)
        assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
        result = runner.invoke(
            cli, ["pilot", "cutover-evidence", "--skip-parity", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["parity"] is None
