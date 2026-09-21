"""agentops#2476: replay of agentops#2431's interrupted-item history against
the `checkpointed_unacked` bucket (agentops#2474 / #2475).

This is evidence, not a new behaviour test: #2450's falsifier (evidence note
#3191 on agentops#2430) was discharged by an interim note scan, not by the
bucket. This fixture reconstructs #2431's real recorded history --
lane.checkpoint note #3134 (2026-09-19T17:07:01.250294Z) and the lane.dispatch
pickup note #3180 (2026-09-19T22:03:37.187863Z) that cites it -- against a
local fixture DB, and asserts the bucket both raises the pickup (state A,
before #3180) and retires it on the real acknowledgement (state B, after
#3180 is recorded). IDs in this fixture are local-sqlite autoincrement ids,
not the real agentops#2431 / #3134 / #3180 numbers; only the payload fields
that flow into the bucket's join and rendering are reproduced verbatim.

Per the item's blocker-note-3397 decision: this replay runs against a local
backend with a fixture DB reconstructing #2431's checkpoint fields, not the
served backend (the S6 note keys are not yet accepted there pending #2493-95).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from sprintctl import db
from sprintctl.cli import cli
from tests.test_work_application import _application, _context

# The real #3134 lane.checkpoint payload's detail field, reproduced verbatim
# (this is the "reproduce enough of it that the next_action survives" text
# from the item brief).
CHECKPOINT_DETAIL = (
    "Refine tick 2026-09-19 16:55 UTC. State found: worktree "
    "/projects/dev/_wt/sprintctl-2431-release-claim on branch "
    "lane/2431-item-release-to-pending had 9 modified files and 1 untracked "
    "(tests/pg/test_item_release.py), +337/-8, uncommitted, branch never "
    "pushed, no PR. The ledger row for session 1789834071 ends 'Waiting on "
    "the worker's rework commit'. Applied the interim rule from #2430: "
    "committed as-is ('wip(2431): worker checkpoint, unreviewed'), pushed; "
    "origin/lane/2431-item-release-to-pending = 89dc1d5. Validated: nothing "
    "(the refine tick does not run or review code). Rejected: unknown; the "
    "worker was mid-rework, so treat the diff as a first draft. next_action "
    "for the successor: fetch the branch, read the diff against the item's "
    "Scope (1)-(5) and Acceptance, finish the rework, PR, merge on green, "
    "accept the Decision."
)

# The real note #3134 / #3180 `created_at` values, verbatim, carry
# microsecond precision ("...250294Z" / "...187863Z") -- `rows.py:iso_timestamp`
# documents that the served (PostgreSQL) backend deliberately preserves
# fractional seconds on read. `application_common._parse_utc_timestamp`,
# which `_checkpointed_unacked_items` uses to order/age every event, parses
# with the strict format `"%Y-%m-%dT%H:%M:%SZ"` -- no `%f` -- so it raises an
# uncaught `ValueError` on exactly this real timestamp shape (see
# `test_bucket_crashes_on_a_real_microsecond_precision_timestamp` below).
# The state A/B replay below therefore uses the same instants truncated to
# whole seconds, which is what a produced note's timestamp collapses to only
# when its microsecond component happens to be `:00` -- not the general case.
# This is a real gap, not a fixture convenience; it is reported as such.
CHECKPOINT_CREATED_AT = "2026-09-19T17:07:01Z"
PICKUP_CREATED_AT = "2026-09-19T22:03:37Z"

# The real, verbatim `created_at` values from notes #3134 / #3180.
REAL_CHECKPOINT_CREATED_AT = "2026-09-19T17:07:01.250294Z"
REAL_PICKUP_CREATED_AT = "2026-09-19T22:03:37.187863Z"


def _backdate_event(conn, event_id, created_at):
    conn.execute("UPDATE event SET created_at = ? WHERE id = ?", (created_at, event_id))
    conn.commit()


def _seed_2431_checkpoint(conn, sprint_id, *, created_at=CHECKPOINT_CREATED_AT):
    """Build the #2431 item + lane.checkpoint note #3134, backdated."""
    track_id = db.get_or_create_track(conn, sprint_id, "served")
    item_id = db.create_work_item(
        conn, sprint_id, track_id,
        "2450: release an active or blocked item back to pending",
    )
    db.set_work_item_status(conn, item_id, "active", actor="lane-loop-refine")

    checkpoint_note_id = db.create_event(
        conn, sprint_id, "lane-loop-refine", "lane.checkpoint",
        work_item_id=item_id,
        payload={
            "summary": "Refine tick checkpoint: worker mid-rework, committed and pushed as-is.",
            "detail": CHECKPOINT_DETAIL,
            "tags": [
                "lane", "agent:lane-loop-refine", "host:devbox",
                "verdict:interrupted", "attempt:1", "tier:fast-build",
                "checkpoint",
            ],
            "git_branch": "lane/2431-item-release-to-pending",
            "git_sha": "89dc1d5",
            "git_worktree": "/projects/dev/_wt/sprintctl-2431-release-claim",
        },
    )
    _backdate_event(conn, checkpoint_note_id, created_at)
    return item_id, checkpoint_note_id


def _seed_2431_pickup(conn, sprint_id, item_id, checkpoint_note_id, *, created_at=PICKUP_CREATED_AT):
    """Add the lane.dispatch pickup note #3180, citing the checkpoint by id."""
    pickup_note_id = db.create_event(
        conn, sprint_id, "lane-loop-refine", "lane.dispatch",
        work_item_id=item_id,
        payload={
            "summary": "Picking up interrupted checkpoint.",
            "detail": (
                f"Resuming from lane.checkpoint note #{checkpoint_note_id}: "
                "fetch lane/2431-item-release-to-pending @ 89dc1d5, review "
                "against Scope/Acceptance, finish rework, PR, merge on green."
            ),
            "evidence_event_id": checkpoint_note_id,
        },
    )
    _backdate_event(conn, pickup_note_id, created_at)
    return pickup_note_id


class _FrozenDatetime(datetime):
    """A `datetime` subclass whose `.now()` always returns a fixed instant.

    Used to reconstruct the bucket's view of `now` as of the real pickup
    timestamp, so `age_hours` in this replay matches the real recorded gap
    between note #3134 and note #3180, not today's wall-clock date.
    """

    _frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen


def _freeze_now(monkeypatch, iso_text):
    frozen = type("_Frozen", (_FrozenDatetime,), {"_frozen": datetime.fromisoformat(iso_text.replace("Z", "+00:00"))})
    # The CLI's plain `next-work` path (sprintctl/commands/session.py) and the
    # served-shaped explain contract (sprintctl/work_application.py, reached
    # here directly against the local sqlite backend, not over the network)
    # each bind their own `datetime` name via separate imports -- both must
    # be frozen for `age_hours` to reproduce the recorded #3134->#3180 gap
    # instead of today's wall-clock date.
    monkeypatch.setattr("sprintctl.commands.session.datetime", frozen)
    monkeypatch.setattr("sprintctl.work_application.datetime", frozen)
    return frozen


class TestReplay2431CheckpointedUnackedBucket:
    """Replay of agentops#2431 (agentops#2476), `now` frozen to the real
    pickup timestamp so `age_hours` reproduces the recorded gap."""

    def test_state_a_before_pickup_surfaces_the_checkpoint(self, monkeypatch, runner, conn, active_sprint):
        item_id, checkpoint_note_id = _seed_2431_checkpoint(conn, active_sprint["id"])
        _freeze_now(monkeypatch, PICKUP_CREATED_AT)

        plain = runner.invoke(
            cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--include-checkpoints"]
        )
        assert plain.exit_code == 0, plain.output
        assert "Checkpointed unacked items (1):" in plain.output
        assert f"#{item_id}" in plain.output
        assert "lane/2431-item-release-to-pending" in plain.output
        assert "89dc1d5" in plain.output
        # Field-gap finding (agentops#2476): the bucket's `Next action:`
        # extractor (`_extract_next_action`, commands/session.py) looks for
        # the literal substring "next_action:". Note #3134's real detail
        # reads "next_action for the successor:" -- no colon directly after
        # "next_action" -- so the extractor does NOT find it and the plain
        # rendering falls through to its "no next_action" line, even though
        # a human reading the same detail immediately sees the next action.
        # The full detail (and so the next_action text) still reaches the
        # operator via the "Detail:" echo beneath it, just not the
        # dedicated "Next action:" line the interim note scan effectively
        # replaced.
        assert f"(none — no checkpoint detail names a next_action: #{item_id})" in plain.output
        assert (
            "next_action for the successor: fetch the branch, read the diff"
            in plain.output
        )

        # The local (non-served) CLI's `--explain --json` view does NOT carry
        # `checkpointed_unacked` (test_cli_output_format.py asserts this
        # explicitly -- it is a deliberate contract boundary, not an
        # oversight here). The served explain contract does; exercise it
        # directly against the same local sqlite backend, the way
        # test_work_application.py's TestCheckpointedUnackedBucket does.
        payload = _application(store=conn, backend=db).invoke(
            "work.read.next-work-explain", {"sprint_id": active_sprint["id"]}, _context()
        )
        assert payload["summary"]["checkpointed_unacked"] == 1
        [entry] = payload["checkpointed_unacked"]
        assert entry["item_id"] == item_id
        assert entry["branch"] == "lane/2431-item-release-to-pending"
        assert entry["sha"] == "89dc1d5"
        assert entry["checkpoint_note_id"] == checkpoint_note_id
        assert entry["reason_code"] == "checkpoint-unacked"
        # Recorded gap between note #3134 and note #3180 is 4h56m35.9s.
        assert entry["age_hours"] == 4.94
        assert entry["stale"] is False

    def test_state_b_after_pickup_dispatch_retires_the_checkpoint(self, monkeypatch, runner, conn, active_sprint):
        item_id, checkpoint_note_id = _seed_2431_checkpoint(conn, active_sprint["id"])
        _seed_2431_pickup(conn, active_sprint["id"], item_id, checkpoint_note_id)
        _freeze_now(monkeypatch, PICKUP_CREATED_AT)

        plain = runner.invoke(
            cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--include-checkpoints"]
        )
        assert plain.exit_code == 0, plain.output
        assert "Checkpointed unacked items" not in plain.output
        assert "lane/2431-item-release-to-pending" not in plain.output
        assert "89dc1d5" not in plain.output

        payload = _application(store=conn, backend=db).invoke(
            "work.read.next-work-explain", {"sprint_id": active_sprint["id"]}, _context()
        )
        assert payload["summary"]["checkpointed_unacked"] == 0
        assert payload["checkpointed_unacked"] == []


class TestBucketRejectsRealMicrosecondPrecisionTimestamps:
    """Field-gap finding from the #2431 replay (agentops#2476), now fixed.

    The real note #3134's `created_at`, verbatim, is
    "2026-09-19T17:07:01.250294Z" -- and `rows.py:iso_timestamp` documents
    that the served backend deliberately keeps fractional seconds on every
    read. `_parse_utc_timestamp` (application_common.py) used to parse with
    `"%Y-%m-%dT%H:%M:%SZ"`, which has no `%f` component, so it raised
    unhandled on this real shape. That was not a fixture artifact: it
    reproduced with the exact, unmodified timestamp string from note #3134.

    This class originally pinned that defect (`TestBucketRejectsRealMicrosecondPrecisionTimestamps`
    ::test_bucket_crashes_on_a_real_microsecond_precision_timestamp asserted a
    non-zero exit and an uncaught `ValueError`). agentops#2499 fixed
    `_parse_utc_timestamp` to accept both whole-second and fractional-second
    timestamps, so this class is inverted to assert the bucket renders the
    checkpoint from the real, unmodified microsecond-precision timestamp
    instead of crashing on it.
    """

    def test_bucket_renders_a_real_microsecond_precision_timestamp(self, monkeypatch, runner, conn, active_sprint):
        item_id, checkpoint_note_id = _seed_2431_checkpoint(
            conn, active_sprint["id"], created_at=REAL_CHECKPOINT_CREATED_AT
        )
        _freeze_now(monkeypatch, REAL_PICKUP_CREATED_AT)

        result = runner.invoke(
            cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--include-checkpoints"]
        )

        assert result.exit_code == 0, result.output
        assert result.exception is None
        assert "Checkpointed unacked items (1):" in result.output
        assert f"#{item_id}" in result.output
        assert "lane/2431-item-release-to-pending" in result.output
        assert "89dc1d5" in result.output

        payload = _application(store=conn, backend=db).invoke(
            "work.read.next-work-explain", {"sprint_id": active_sprint["id"]}, _context()
        )
        assert payload["summary"]["checkpointed_unacked"] == 1
        [entry] = payload["checkpointed_unacked"]
        assert entry["item_id"] == item_id
        assert entry["checkpoint_note_id"] == checkpoint_note_id
        assert entry["reason_code"] == "checkpoint-unacked"
        # Same recorded gap as the whole-second replay above (4h56m35.9s),
        # now derived from the real microsecond-precision instants.
        assert entry["age_hours"] == 4.94
