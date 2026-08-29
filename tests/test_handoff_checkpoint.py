"""A handed-off session's position has to survive the process that held it.

Before 2026-08-29 `record_handoff_generated` stored only that a handoff had happened.
The bundle's `git_context` was computed by the client, sent to the server, echoed back
in the response, and dropped. A fresh process could therefore recover the session
identity and the work authority from an open reservation and still have no idea which
revision the work was at -- which is what made the resumability outcome unprovable
rather than merely unproven.

`git_context` and `last_checkpoint` answer two different questions and both are needed:

    git_context      what the CALLER observes right now -- null for a resuming process,
                     which has no worktree yet, and never inspected by the server
    last_checkpoint  what the LAST session recorded -- the half that has to survive

Both directions of the rollout are additive. A bundle recorded before checkpoints were
persisted reads back as `last_checkpoint: None`, not as an error.
"""

from __future__ import annotations

import json

import pytest

from sprintctl import db, handoff

GIT_CONTEXT = {
    "branch": "main",
    "sha": "1b71bfe69c52281ef20cb8c889fbdb0f34ba34df",
    "worktree": "/projects/dev/agentops",
    "dirty_files": ["a.py", "b.py"],
}


def _bundle(conn, sprint, *, git_context):
    return handoff.build_handoff_bundle(
        conn, sprint, 50, backend=db, version="test", git_context=git_context
    )


def test_the_first_bundle_has_no_predecessor_to_read(conn, active_sprint):
    bundle = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    assert bundle["last_checkpoint"] is None
    assert bundle["git_context"] == GIT_CONTEXT


def test_a_recorded_checkpoint_is_served_to_the_next_process(conn, active_sprint):
    """The whole outcome, in one test: record with a worktree, read back without one."""
    recorded = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    handoff.record_handoff_generated(
        conn, active_sprint["id"], recorded, backend=db, actor="interrupted-session"
    )

    # The resuming process has no worktree, so it sends no git context at all.
    resumed = _bundle(conn, active_sprint, git_context=None)

    assert resumed["git_context"] is None, "the resumer observes nothing; that is honest"
    checkpoint = resumed["last_checkpoint"]
    assert checkpoint["sha"] == GIT_CONTEXT["sha"]
    assert checkpoint["branch"] == "main"
    assert checkpoint["worktree"] == "/projects/dev/agentops"
    assert checkpoint["recorded_at"] == recorded["generated_at"]


def test_the_checkpoint_reports_dirt_without_carrying_the_diff(conn, active_sprint):
    """Git owns the content. The bundle owes the resumer the fact that it exists."""
    recorded = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    handoff.record_handoff_generated(
        conn, active_sprint["id"], recorded, backend=db, actor="a"
    )
    checkpoint = _bundle(conn, active_sprint, git_context=None)["last_checkpoint"]

    assert checkpoint["dirty_file_count"] == 2
    assert "dirty_files" not in checkpoint


def test_the_newest_checkpoint_wins(conn, active_sprint):
    first = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    handoff.record_handoff_generated(conn, active_sprint["id"], first, backend=db, actor="a")
    later = {**GIT_CONTEXT, "sha": "0" * 40, "dirty_files": []}
    second = _bundle(conn, active_sprint, git_context=later)
    handoff.record_handoff_generated(conn, active_sprint["id"], second, backend=db, actor="a")

    checkpoint = _bundle(conn, active_sprint, git_context=None)["last_checkpoint"]
    assert checkpoint["sha"] == "0" * 40
    assert checkpoint["dirty_file_count"] == 0


def test_a_session_with_no_worktree_records_no_checkpoint(conn, active_sprint):
    """Recording `null` must not erase a real predecessor with an empty one."""
    real = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    handoff.record_handoff_generated(conn, active_sprint["id"], real, backend=db, actor="a")
    worktreeless = _bundle(conn, active_sprint, git_context=None)
    handoff.record_handoff_generated(
        conn, active_sprint["id"], worktreeless, backend=db, actor="b"
    )

    checkpoint = _bundle(conn, active_sprint, git_context=None)["last_checkpoint"]
    assert checkpoint is not None and checkpoint["sha"] == GIT_CONTEXT["sha"]


def test_a_bundle_recorded_before_checkpoints_reads_back_as_absent(conn, active_sprint):
    """The other half of the additive rollout: an old payload is not an error."""
    db.create_event(
        conn,
        sprint_id=active_sprint["id"],
        actor="old-client",
        event_type="handoff-generated",
        source_type="system",
        payload={
            "summary": "Handoff bundle generated",
            "detail": "Generated a working-memory handoff bundle for the next session.",
            "bundle_version": "1",
            "events_limit": 50,
        },
    )
    bundle = _bundle(conn, active_sprint, git_context=None)
    assert bundle["last_checkpoint"] is None
    assert bundle["delta_since_last_handoff"]["previous_handoff_at"] is not None


@pytest.mark.parametrize(
    "git_context",
    [None, {}, {"branch": "main"}, {"sha": "", "branch": "main"}],
)
def test_a_position_without_a_revision_is_not_a_checkpoint(git_context):
    assert handoff.checkpoint_from(git_context, "2026-08-29T00:00:00Z") is None


def test_the_recorded_payload_is_json_round_trippable(conn, active_sprint):
    """The pg backend stores the payload as JSON; a value that will not serialise there
    would be a defect visible only in production."""
    bundle = _bundle(conn, active_sprint, git_context=GIT_CONTEXT)
    handoff.record_handoff_generated(conn, active_sprint["id"], bundle, backend=db, actor="a")
    event = next(
        e for e in reversed(db.list_events(conn, active_sprint["id"]))
        if e["event_type"] == "handoff-generated"
    )
    payload = e_payload = event["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert json.loads(json.dumps(payload))["checkpoint"]["sha"] == GIT_CONTEXT["sha"]
    assert e_payload is not None


def test_the_resumability_outcome_end_to_end(conn, active_sprint):
    """The outcome the acceptance scenario `resume-and-settle` scores, without a network.

    A session reserves an item, records a handoff at a known revision, and stops. A fresh
    process -- no worktree, no local cache, sending no git context -- must recover all
    four elements: session identity, work authority, checkpoint, exact revision.

    This is the served path, not the CLI path: it goes through the same work application
    the deployment runs, so a pass here is a prediction about the deployment rather than
    a statement about this repository's helpers.
    """
    from tests.test_work_application import _application, _context

    app = _application(store=conn, backend=db)
    ctx = _context(actor="workstation-vuoro")
    session_id = "interrupted-session-1"

    created = app.invoke(
        "work.item.create",
        {"sprint_id": active_sprint["id"], "title": "the work", "track_name": "t1"},
        ctx,
    )
    item_id = created["item"]["id"]

    # --- the session that gets interrupted ---
    app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "workstation-vuoro", "session_id": session_id,
         "role": "execution"},
        ctx,
    )
    bundle = app.invoke(
        "work.read.handoff",
        {"sprint_id": active_sprint["id"], "events_limit": 50, "git_context": GIT_CONTEXT},
        ctx,
    )
    app.invoke(
        "work.handoff.record", {"sprint_id": active_sprint["id"], "bundle": bundle}, ctx
    )

    # --- the process that resumes, carrying only its principal ---
    resumed = app.invoke(
        "work.read.handoff",
        {"sprint_id": active_sprint["id"], "events_limit": 50, "git_context": None},
        ctx,
    )
    reservations = app.invoke("work.read.reservations", {"active_only": True}, ctx)

    claims = reservations["reservations"] if isinstance(reservations, dict) else reservations
    claim = next(row for row in claims if row["work_item_id"] == item_id)

    assert claim["session_id"] == session_id, "session identity"
    assert claim["actor"] == "workstation-vuoro" and claim["work_item_id"] == item_id, "work authority"
    assert resumed["last_checkpoint"] is not None, "checkpoint"
    assert resumed["last_checkpoint"]["sha"] == GIT_CONTEXT["sha"], "exact revision"
