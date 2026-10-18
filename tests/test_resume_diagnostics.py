"""Continuation gaps and reported checks never become owner assurance."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from sprintctl.cli import cli
from sprintctl.resume_diagnostics import diagnose_resume, render_resume_diagnostic
from tests.test_run_brief import owner_response, calls

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def observation(response, *, status="inaccessible", target=None, at=None):
    return {"schema": "sprintctl-resume-observations/v1", "repo_id": response["repo_id"],
        "item_id": response["delta"]["work"]["item_id"],
        "snapshot_digest": response["delta"]["snapshot_digest"], "checks": [{
            "target": target or {"kind": "source-bytes"},
            "observed_at": (at or NOW - timedelta(minutes=30)).isoformat(),
            "observer": "reader", "method": "manual content inspection", "status": status}]}


def test_gaps_changed_records_unknowns_and_scope_are_explicit(owner_response):
    before = deepcopy(owner_response)
    report = diagnose_resume(owner_response, checkpoint_supplied=False, now=NOW)
    codes = {row["reason_code"] for row in report["findings"]}
    assert {"comparison-baseline-absent", "source-checkpoint-absent", "owner-record-changed",
            "owner-record-comparison-unknown", "source-content-unchecked",
            "reference-correspondence-unchecked", "external-content-unchecked", "outcome-not-assessed"} <= codes
    assert report["recovery_effort"] is None
    assert report["ranking_basis"] == "declared-checklist-order"
    assert report["authorizes_action"] is False
    assert report["owner_response"] == owner_response == before
    text = render_resume_diagnostic(report)
    assert "not measured recovery burden" in text
    assert "historical checkpoints may still exist" in text


@pytest.mark.parametrize("status", ["available", "inaccessible", "unchecked"])
def test_caller_reports_never_upgrade_owner_unknowns(owner_response, status):
    report = diagnose_resume(owner_response, checkpoint_supplied=True,
                            observations=observation(owner_response, status=status), now=NOW)
    assert report["owner_response"]["delta"]["source_bytes"] == "unknown"
    assert report["observations"][0]["evidence_class"] == "report"
    assert report["coverage"]["artifact_checks_performed"] == 0
    first = report["findings"][0]
    if status == "inaccessible":
        assert first["reason_code"] == "artifact-inaccessible-reported"
        assert first["provenance"] == "caller-report"
        assert "globally missing" in first["next_check"]
    else:
        assert not any(row["reason_code"] == "artifact-inaccessible-reported" for row in report["findings"])


@pytest.mark.parametrize("offset,reason", [(timedelta(hours=-2), "stale"), (timedelta(seconds=1), "future")])
def test_stale_and_future_reports_are_retained_but_not_used(owner_response, offset, reason):
    report = diagnose_resume(owner_response, checkpoint_supplied=True,
        observations=observation(owner_response, at=NOW + offset), now=NOW)
    assert report["observations"][0]["exclusion_reason"] == reason
    assert report["coverage"]["eligible_caller_observations"] == 0
    assert not any(row["reason_code"] == "artifact-inaccessible-reported" for row in report["findings"])


def test_exact_one_hour_boundary_is_eligible_and_unchanged_refs_do_not_verify_bytes(owner_response):
    owner_response["delta"]["comparison"]["current_reference_set_digest"] = "unchanged"
    report = diagnose_resume(owner_response, checkpoint_supplied=True,
        observations=observation(owner_response, at=NOW - timedelta(hours=1)), now=NOW)
    assert report["observations"][0]["eligible"] is True
    assert any(row["reason_code"] == "source-content-unchecked" for row in report["findings"])


@pytest.mark.parametrize("field,value", [("repo_id", "other"), ("item_id", 9),
    ("item_id", True), ("snapshot_digest", "sha256:" + "0" * 64)])
def test_wrong_scope_or_snapshot_is_rejected(owner_response, field, value):
    observed = observation(owner_response)
    observed[field] = value
    with pytest.raises(ValueError, match="repository, item and owner snapshot"):
        diagnose_resume(owner_response, checkpoint_supplied=True, observations=observed, now=NOW)


@pytest.mark.parametrize("target", [{"kind": "reference", "reference_id": 99},
    {"kind": "reference", "reference_id": True}, {"kind": "source-bytes", "url": "https://example.test"}])
def test_checks_are_bound_to_source_or_visible_reference_ids(owner_response, target):
    with pytest.raises(ValueError, match="visible on this page"):
        diagnose_resume(owner_response, checkpoint_supplied=True,
            observations=observation(owner_response, target=target), now=NOW)


def test_visible_reference_report_and_duplicate_target_rejection(owner_response):
    observed = observation(owner_response, target={"kind": "reference", "reference_id": 4})
    report = diagnose_resume(owner_response, checkpoint_supplied=True, observations=observed, now=NOW)
    assert report["observations"][0]["subject"] == "reference:4"
    observed["checks"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        diagnose_resume(owner_response, checkpoint_supplied=True, observations=observed, now=NOW)


def test_incomplete_owner_pages_are_not_hidden(owner_response):
    owner_response["delta"]["next_after_chain_seq"] = 0
    owner_response["delta"]["decision_page_complete"] = False
    report = diagnose_resume(owner_response, checkpoint_supplied=True, now=NOW)
    assert any(row["reason_code"] == "owner-context-incomplete" for row in report["findings"])
    assert report["coverage"]["decision_page_complete"] is False


@pytest.mark.parametrize("stamp", ["2026-10-18", "bad", None, "2026-10-18T10:00:00+02:00"])
def test_report_time_must_be_utc(owner_response, stamp):
    observed = observation(owner_response)
    observed["checks"][0]["observed_at"] = stamp
    with pytest.raises(ValueError, match="UTC"):
        diagnose_resume(owner_response, checkpoint_supplied=True, observations=observed, now=NOW)


def test_untrusted_observer_and_method_are_escaped(owner_response):
    observed = observation(owner_response)
    observed["checks"][0]["observer"] = "\x1b[2J\noperator"
    report = diagnose_resume(owner_response, checkpoint_supplied=True, observations=observed, now=NOW)
    assert "\x1b" not in render_resume_diagnostic(report)
    assert "\\u001b" in render_resume_diagnostic(report)


def test_cli_performs_one_read_and_preserves_full_owner_payload(runner, calls, owner_response):
    result = runner.invoke(cli, ["run", "diagnose", "--run-id", "run_successor", "--item-id", "7", "--format", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["owner_response"] == owner_response
    assert calls == [("work.run.continuation-delta-v1", {"run_id": "run_successor", "item_id": 7, "limit": 32})]


@pytest.mark.parametrize("content", [b"[]", b"bad json", b" " * 65537])
def test_invalid_or_oversized_observation_file_fails_before_any_read(runner, calls, tmp_path, content):
    path = tmp_path / "observations.json"
    path.write_bytes(content)
    result = runner.invoke(cli, ["run", "diagnose", "--run-id", "run_successor", "--item-id", "7",
                                 "--observations", str(path)])
    assert result.exit_code != 0
    assert calls == []
