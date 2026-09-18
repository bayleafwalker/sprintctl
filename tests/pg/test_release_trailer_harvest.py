"""PostgreSQL integration tests: Vuoro-Release trailer harvest (S3 PR4).

A sync from a checkout whose commits carry ``Vuoro-Release`` trailers binds
known release digests into ``release_commit``; unknown digests stay only as
ingest records and never fail the batch.
"""
from __future__ import annotations

from tests.pg._shared import PG_MARKS, _uid, outbox, pg, sync
from tests.test_release_trailers import _commit, _git

pytestmark = PG_MARKS

UNKNOWN = "f" * 64


def _released_item(store) -> str:
    sprint_id = pg.create_sprint(store, f"Harvest-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "harvest")
    item_id = pg.create_work_item(store, sprint_id, track_id, f"item {_uid()}")
    pg.set_work_item_status(store, item_id, "active")
    reservation = pg.reserve(store, item_id, actor="agent", session_id="s1", role="execution")
    return reservation["release_digest"]


def _checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://bot:tok3n@example.com/o/r.git")
    (root / ".sprintctl").mkdir()
    return root


def _sync(store, root):
    return sync.synchronize_repository(
        store,
        outbox_path=root / ".sprintctl" / "sync-outbox.db",
        projection_path=root / ".sprintctl" / "sync-projection.db",
    )


def _observed(store):
    return [
        r.record for r in pg.list_ingested_records(store, limit=1000)
        if r.record.event_type == "release.commit-observed"
    ]


def test_known_digest_binds_unknown_stays_an_ingest_record(store, tmp_path):
    known = _released_item(store)
    root = _checkout(tmp_path)
    sha = _commit(
        root,
        f"deliver\n\nVuoro-Release: sha256:{known}\nVuoro-Release: {UNKNOWN}\n"
        "Vuoro-Release: garbage",
    )

    result = _sync(store, root)

    assert result.release_harvest.enqueued == 2
    assert result.release_harvest.malformed == 1
    [row] = pg.list_release_commits(store, known)
    assert row["commit_sha"] == sha
    assert row["ref"] == "refs/heads/main"
    assert row["remote_hint"] == "https://example.com/o/r.git"
    assert pg.list_release_commits(store, UNKNOWN) == []
    assert {known, UNKNOWN} <= {r.payload["release_digest"] for r in _observed(store)}


def test_resync_is_a_noop_and_rebinding_is_idempotent(store, tmp_path):
    known = _released_item(store)
    root = _checkout(tmp_path)
    _commit(root, f"deliver\n\nVuoro-Release: {known}")
    _sync(store, root)

    again = _sync(store, root)

    assert again.release_harvest.enqueued == 0
    assert all(outcome.duplicate for outcome in again.uploaded)
    assert [r.payload["release_digest"] for r in _observed(store)].count(known) == 1
    assert len(pg.list_release_commits(store, known)) == 1

    # A second clone observing the same pair adds an ingest record only.
    first = outbox.open_outbox(root / ".sprintctl" / "sync-outbox.db")
    try:
        [record] = [
            r for r in outbox.list_records(first)
            if r.event_type == "release.commit-observed"
        ]
    finally:
        first.close()
    conn = outbox.open_outbox(tmp_path / "other-outbox.db")
    try:
        duplicate = outbox.append_observation(
            conn, event_type=record.event_type, actor="other", payload=record.payload
        )
    finally:
        conn.close()
    [admitted] = pg.ingest_records(store, [duplicate])
    assert admitted.duplicate is False
    assert len(pg.list_release_commits(store, known)) == 1


def test_malformed_payload_never_fails_the_batch(store, tmp_path):
    conn = outbox.open_outbox(tmp_path / "outbox.db")
    try:
        records = [
            outbox.append_observation(
                conn, event_type="release.commit-observed", actor="a", payload=payload
            )
            for payload in (
                {"release_digest": "nope", "commit_sha": "a" * 40},
                {"release_digest": UNKNOWN, "commit_sha": "B" * 40},
                {"release_digest": UNKNOWN, "commit_sha": "a" * 40, "ref": 3},
            )
        ]
    finally:
        conn.close()
    admitted = pg.ingest_records(store, records)
    assert [a.duplicate for a in admitted] == [False, False, False]
    assert pg.list_release_commits(store, UNKNOWN) == []
