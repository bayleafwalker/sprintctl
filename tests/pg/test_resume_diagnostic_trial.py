"""Three paired diagnostic fixtures; human recovery effort remains unmeasured."""

from datetime import datetime, timezone
import uuid

import pytest

from sprintctl import pg
from sprintctl.resume_diagnostics import diagnose_resume
from tests.pg._shared import PG_MARKS, _PG_URL, dict_row, psycopg
from tests.pg.test_effect_intent import PROPOSER, _app, _items, _run
from tests.pg.test_releases import _edit

pytestmark = PG_MARKS
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def snapshot_factory(store):
    store.connection_factory = lambda: psycopg.connect(_PG_URL, row_factory=dict_row)


@pytest.mark.parametrize("case", ["continuation-point", "comparison-basis", "reported-access"])
def test_paired_diagnostic_fixture_preserves_questions_and_unknowns(store, case):
    app = _app(store)
    item_id = _items(store)[0]
    run_id = _run(store, PROPOSER)
    ref_id = pg.add_ref(store, item_id, "doc", "docs/fixture-plan.md", "fixture")
    pg.reserve(store, item_id, actor="fixture", session_id=uuid.uuid4().hex, role="execution")

    def read(**extra):
        return app.invoke("work.run.continuation-delta-v1", {
            "run_id": run_id, "item_id": item_id, **extra}, PROPOSER)

    baseline = read()
    if case == "comparison-basis":
        _edit(pg, store, item_id, "Review replacement artifact before continuation.")
        pg.record_decision(store, item_id, "revise", actor="fixture-reviewer")
        pg.reserve(store, item_id, actor="fixture", session_id=uuid.uuid4().hex, role="execution")
    before_owner = read()
    before = diagnose_resume(before_owner, checkpoint_supplied=False, now=NOW)
    observations = None
    arguments = {}
    if case == "continuation-point":
        item = pg.get_work_item(store, item_id)
        pg.create_event(store, item["sprint_id"], "fixture", "lane.checkpoint", work_item_id=item_id,
            payload={"summary": "Continue artifact review", "git_sha": "a" * 40,
                     "detail": "next_action: inspect the replacement artifact"})
    elif case == "comparison-basis":
        arguments["checkpoint"] = baseline["delta"]["current"]
    after_owner = read(**arguments)
    if case == "reported-access":
        observations = {"schema": "sprintctl-resume-observations/v1", "repo_id": store.repo_id,
            "item_id": item_id, "snapshot_digest": after_owner["delta"]["snapshot_digest"],
            "checks": [{"target": {"kind": "reference", "reference_id": ref_id},
                "observed_at": NOW.isoformat(), "observer": "fixture-reader",
                "method": "fixture supplied report; no content access performed", "status": "inaccessible"}]}
    after = diagnose_resume(after_owner, checkpoint_supplied="checkpoint" in arguments,
                            observations=observations, now=NOW)
    before_codes = {row["reason_code"] for row in before["findings"]}
    after_codes = {row["reason_code"] for row in after["findings"]}
    if case == "continuation-point":
        # Frozen questions: current point present? declared revision? Git verified?
        assert before_owner["delta"]["source_checkpoint"] is None
        assert "source-checkpoint-absent" in before_codes - after_codes
        assert after_owner["delta"]["source_checkpoint"]["declared_git_sha"] == "a" * 40
        assert after_owner["delta"]["source_checkpoint"]["git_currentness"] == "unknown"
    elif case == "comparison-basis":
        # Frozen questions: baseline supplied? Release changed? Decision changed?
        assert "comparison-baseline-absent" in before_codes - after_codes
        assert set(before_owner["delta"]["comparison"].values()) == {"unknown"}
        for field in ("current_release_digest", "current_decision_digest"):
            assert after_owner["delta"]["comparison"][field] == "changed"
        assert "owner-record-changed" in after_codes
    else:
        # Frozen questions: access reported? owner content verified? global absence proven?
        assert "artifact-inaccessible-reported" in after_codes - before_codes
        assert after["findings"][0]["provenance"] == "caller-report"
        assert after_owner["delta"]["external_missing_content"] == "unknown"
    for report in (before, after):
        assert report["coverage"]["owner_read_count"] == 1
        assert report["coverage"]["artifact_checks_performed"] == 0
        assert report["recovery_effort"] is None
        assert report["owner_response"]["delta"]["source_bytes"] == "unknown"
