"""PostgreSQL integration tests: work decisions (TS-5) and the schema 14 fold.

A decision is the only writer of terminal status.  These tests exercise the
database guards directly, the runtime aliases that route ``done`` through an
accept decision, carryover's supersede decision, and the migration that folds
the retired capability-receipt surface.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    pg,
    pg_migrations,
    maintain,
    PG_MARKS,
    _PG_URL,
    _uid,
    assert_disposable_connection,
    json,
    uuid,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


def _item(store, status="active"):
    sprint_id = pg.create_sprint(store, f"Decisions-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "decisions")
    item_id = pg.create_work_item(store, sprint_id, track_id, f"item {_uid()}")
    if status in {"active", "blocked"}:
        pg.set_work_item_status(store, item_id, "active")
    if status == "blocked":
        pg.set_work_item_status(store, item_id, "blocked")
    return sprint_id, track_id, item_id


def _raises_on_commit(store, statement, params, match):
    with pytest.raises(psycopg.errors.Error, match=match):
        with store.conn.cursor() as cur:
            cur.execute(statement, params)
        store.conn.commit()
    store.conn.rollback()


class TestDecisionGuards:
    def test_done_without_a_decision_is_refused(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        _raises_on_commit(
            store,
            "UPDATE work_item SET status = 'done' WHERE repo_id = %s AND id = %s",
            (store.repo_id, item_id),
            "done without a terminal decision",
        )
        assert pg.get_work_item(store, item_id)["status"] == "active"

    def test_work_decision_refuses_update_delete_and_truncate(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        decision = pg.record_decision(store, item_id, "revise", actor="reviewer")
        for statement in (
            "UPDATE work_decision SET rationale = 'rewritten' WHERE repo_id = %s AND id = %s",
            "DELETE FROM work_decision WHERE repo_id = %s AND id = %s",
        ):
            _raises_on_commit(store, statement, (store.repo_id, decision["id"]), "append-only")
        _raises_on_commit(store, "TRUNCATE work_decision CASCADE", (), "append-only")
        assert [d["id"] for d in pg.list_decisions(store, item_id)] == [decision["id"]]

    def test_legacy_is_immutable_and_never_set_on_insert(self, store):
        sprint_id, track_id, item_id = _item(store)
        _raises_on_commit(
            store,
            "UPDATE work_item SET legacy = true WHERE repo_id = %s AND id = %s",
            (store.repo_id, item_id),
            "legacy is immutable",
        )
        _raises_on_commit(
            store,
            "INSERT INTO work_item (repo_id, sprint_id, track_id, title, legacy, aggregate_uuid) "
            "VALUES (%s, %s, %s, 'forged', true, %s)",
            (store.repo_id, sprint_id, track_id, str(uuid.uuid4())),
            "legacy is set only by the schema 14 migration",
        )
        assert pg.get_work_item(store, item_id)["legacy"] is False

    def test_done_never_transitions_back(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        pg.set_work_item_status(store, item_id, "done", actor="closer")
        _raises_on_commit(
            store,
            "UPDATE work_item SET status = 'pending' WHERE repo_id = %s AND id = %s",
            (store.repo_id, item_id),
            "is terminal",
        )
        assert pg.get_work_item(store, item_id)["status"] == "done"

    def test_resolution_must_match_the_decision_kind(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_decision (repo_id, kind, work_item_id, actor) "
                "VALUES (%s, 'reject', %s, 'forger') RETURNING id",
                (store.repo_id, item_id),
            )
            decision_id = cur.fetchone()["id"]
        _raises_on_commit(
            store,
            "UPDATE work_item SET status = 'done', resolution = 'accepted', "
            "terminal_decision_id = %s WHERE repo_id = %s AND id = %s",
            (decision_id, store.repo_id, item_id),
            "does not match its reject decision",
        )

    def test_a_terminal_decision_must_close_its_item(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        _raises_on_commit(
            store,
            "INSERT INTO work_decision (repo_id, kind, work_item_id, actor) "
            "VALUES (%s, 'withdraw', %s, 'forger')",
            (store.repo_id, item_id),
            "is not bound to work item",
        )


class TestDecisionWritePath:
    def test_set_status_done_is_an_accept_decision(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        pg.set_work_item_status(store, item_id, "done", actor="closer")
        item = pg.get_work_item(store, item_id)
        [decision] = pg.list_decisions(store, item_id)
        assert (decision["kind"], decision["actor"]) == ("accept", "closer")
        assert item["resolution"] == "accepted"
        assert item["terminal_decision_id"] == decision["id"]

    @pytest.mark.parametrize(
        ("kind", "resolution"),
        [("reject", "rejected"), ("withdraw", "withdrawn")],
    )
    def test_terminal_kinds_close_any_open_item(self, store, kind, resolution):
        _sprint_id, _track_id, item_id = _item(store, status="blocked")
        # A decision may only name a release of its own item (schema 15).
        digest = pg.reserve(store, item_id, actor="owner", session_id="s")["release_digest"]
        decision = pg.record_decision(
            store,
            item_id,
            kind,
            actor="owner",
            rationale="not needed",
            evidence_digests=["b" * 64, "b" * 64],
            release_digest=digest,
        )
        item = pg.get_work_item(store, item_id)
        assert (item["status"], item["resolution"]) == ("done", resolution)
        assert decision["evidence_digests"] == ["b" * 64]
        assert decision["release_digest"] == digest

    def test_revise_is_recorded_and_leaves_the_item_open(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        pg.record_decision(store, item_id, "revise", actor="reviewer", rationale="tighten")
        item = pg.get_work_item(store, item_id)
        assert (item["status"], item["resolution"]) == ("active", None)
        assert item["terminal_decision_id"] is None

    def test_terminal_item_takes_no_further_decision(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        pg.record_decision(store, item_id, "withdraw", actor="owner")
        with pytest.raises(pg.InvalidTransition, match="terminal"):
            pg.record_decision(store, item_id, "revise", actor="owner")
        assert len(pg.list_decisions(store, item_id)) == 1

    def test_accept_requires_an_active_item(self, store):
        _sprint_id, _track_id, item_id = _item(store, status="pending")
        with pytest.raises(pg.InvalidTransition, match="requires an active item"):
            pg.record_decision(store, item_id, "accept", actor="owner")
        assert pg.list_decisions(store, item_id) == []

    def test_carryover_supersedes_with_the_new_item(self, store):
        from_sprint, track_id, item_id = _item(store)
        to_sprint = pg.create_sprint(store, f"Carry-to-{_uid()}", status="active")
        created = maintain.carryover(store, from_sprint, to_sprint, _m=pg)
        [new_item] = created
        [decision] = pg.list_decisions(store, item_id)
        assert decision["kind"] == "supersede"
        assert decision["superseded_by_item_id"] == new_item["id"]
        assert decision["actor"] == "maintain-carryover"
        original = pg.get_work_item(store, item_id)
        assert (original["status"], original["resolution"]) == ("done", "superseded")
        assert new_item["status"] == "pending"


# ---------------------------------------------------------------------------
# Schema 14 migration: legacy marking and the capability-receipt fold
# ---------------------------------------------------------------------------

_SHA = {name: (name * 64)[:64] for name in "abcdef"}


def _ingest(cur, repo_id, stream, seq, offset, event_id, event_type, record_class, payload, digest):
    cur.execute(
        "INSERT INTO ingest_record (repo_id, ingest_offset, origin_stream_id, origin_seq, "
        "event_id, schema_version, record_class, event_type, actor, occurred_at, "
        "payload, payload_sha256, record_sha256, producer_created_at) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, 'receipt-agent', now(), %s, %s, %s, now())",
        (repo_id, offset, stream, seq, event_id, record_class, event_type,
         json.dumps(payload), digest, digest),
    )


def _seed_v13_fixture(cur, repo_id, *, with_items=True):
    """A schema-13 repository holding the whole retired receipt surface.

    ``with_items=False`` seeds only what an old (0.3.7) pod can still write
    after schema 14 is live: receipt events and journal rows.
    """
    sprint_ids = []
    for name in ("closed-a", "closed-b", "closed-c"):
        cur.execute(
            "INSERT INTO sprint (repo_id, name, status, aggregate_uuid) "
            "VALUES (%s, %s, 'closed', %s) RETURNING id, aggregate_uuid",
            (repo_id, name, str(uuid.uuid4())),
        )
        sprint_ids.append(cur.fetchone())
    cur.execute(
        "INSERT INTO track (repo_id, sprint_id, name) VALUES (%s, %s, 't') RETURNING id",
        (repo_id, sprint_ids[0]["id"]),
    )
    track_id = cur.fetchone()["id"]
    for title, status in (("finished", "done"), ("open", "pending")) if with_items else ():
        cur.execute(
            "INSERT INTO work_item (repo_id, sprint_id, track_id, title, status, aggregate_uuid) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (repo_id, sprint_ids[0]["id"], track_id, title, status, str(uuid.uuid4())),
        )
    # Four drafted receipts (the production shape), one demoted import, and
    # an unrelated event that must not be folded.
    for index, event_type in enumerate(
        ["capability-receipt-drafted"] * 4 + ["capability-receipt-drafted-imported", "note"]
    ):
        cur.execute(
            "INSERT INTO event (repo_id, sprint_id, source_type, actor, event_type, payload) "
            "VALUES (%s, %s, 'actor', 'drafting-agent', %s, %s)",
            (repo_id, sprint_ids[index % 3]["id"], event_type,
             json.dumps({"receipt_sha256": _SHA["a"], "n": index})),
        )
    stream = "stream:" + repo_id
    cur.execute(
        "INSERT INTO ingest_stream (repo_id, origin_stream_id, highest_origin_seq) "
        "VALUES (%s, %s, 8)",
        (repo_id, stream),
    )
    offset = 0
    # (request type, decision type, outcome, sprint ref, effect carries sprint_id)
    cases = [
        ("capability-receipt.accept", "capability-receipt.accepted", "accepted", 0, True),
        ("capability-receipt.accept", "capability-receipt.accepted", "accepted", 1, False),
        ("capability-receipt.accept", "command.rejected", "rejected", 2, True),
        ("item.done", "item.transitioned", "accepted", 0, True),
    ]
    for seq, (request_type, decision_type, outcome, sprint_index, effect_has_sprint) in enumerate(cases):
        sprint = sprint_ids[sprint_index]
        request_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        request_digest = (str(seq) * 64)[:64]
        pointer = {"receipt_sha256": _SHA["b"]}
        _ingest(
            cur, repo_id, stream, seq * 2 + 1, offset + 1, request_id, request_type,
            "authority-command",
            {"payload": {"pointer": pointer},
             "refs": {"aggregate_uuid": str(sprint["aggregate_uuid"])}},
            request_digest,
        )
        _ingest(
            cur, repo_id, stream, seq * 2 + 2, offset + 2, decision_id, decision_type,
            "remote-decision", {"outcome": outcome}, _SHA["c"],
        )
        effect = {"receipt_sha256": _SHA["b"]}
        if effect_has_sprint:
            effect["sprint_id"] = sprint["id"]
        cur.execute(
            "INSERT INTO authority_decision (repo_id, request_event_id, request_record_sha256, "
            "decision_event_id, decision_ingest_offset, outcome, effect) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (repo_id, request_id, request_digest, decision_id, offset + 2, outcome,
             json.dumps(effect)),
        )
        offset += 2
    return [row["id"] for row in sprint_ids]


def _fold_counts(cur, repo_id):
    cur.execute(
        "SELECT sprint_id, kind, actor, evidence_digests, legacy_source FROM work_decision "
        "WHERE repo_id = %s ORDER BY sprint_id",
        (repo_id,),
    )
    decisions = cur.fetchall()
    cur.execute(
        "SELECT kind, count(*) AS n FROM work_legacy_evidence WHERE repo_id = %s "
        "GROUP BY kind ORDER BY kind",
        (repo_id,),
    )
    evidence = {row["kind"]: int(row["n"]) for row in cur.fetchall()}
    cur.execute(
        "SELECT title, status, legacy, resolution, terminal_decision_id FROM work_item "
        "WHERE repo_id = %s ORDER BY title",
        (repo_id,),
    )
    items = cur.fetchall()
    return decisions, evidence, items


class TestSchema14Fold:
    def test_v13_fixture_folds_exact_counts_and_rerun_is_a_no_op(
        self, pg_test_scope, monkeypatch
    ):
        schema = "decision_fold_" + uuid.uuid4().hex
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        repo_id = pg_test_scope("decision-fold")
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            store = pg.PgStore(conn, repo_id)
            # Build exactly schema 13: run the ladder with 14 withheld.
            with monkeypatch.context() as patch:
                patch.setattr(pg, "_apply_schema_version_14", lambda cur: None)
                # 15's guards name the decision table 14 creates.
                patch.setattr(pg, "_apply_schema_version_15", lambda cur: None)
                pg_migrations.migrate_schema(store)
            with conn.cursor() as cur:
                cur.execute("UPDATE schema_version SET version = 13")
                cur.execute("SELECT to_regclass('work_decision') AS relation")
                assert cur.fetchone()["relation"] is None
                sprint_ids = _seed_v13_fixture(cur, repo_id)
            conn.commit()

            migrated = pg_migrations.migrate_schema(store)
            assert migrated["applied_versions"] == [14, 15, 16]

            with conn.cursor() as cur:
                decisions, evidence, items = _fold_counts(cur, repo_id)
            assert [
                (row["sprint_id"], row["kind"], row["legacy_source"], row["actor"])
                for row in decisions
            ] == [
                (sprint_ids[0], "accept", "capability-receipt", "receipt-agent"),
                (sprint_ids[1], "accept", "capability-receipt", "receipt-agent"),
            ]
            assert decisions[0]["evidence_digests"] == [_SHA["b"], "0" * 64]
            assert decisions[1]["evidence_digests"] == [_SHA["b"], "1" * 64]
            assert evidence == {
                "capability-receipt-drafted": 4,
                "capability-receipt-drafted-imported": 1,
            }
            assert [
                (row["title"], row["status"], row["legacy"], row["resolution"],
                 row["terminal_decision_id"])
                for row in items
            ] == [
                ("finished", "done", True, None, None),
                ("open", "pending", True, None, None),
            ]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM event WHERE repo_id = %s "
                    "AND event_type LIKE 'capability-receipt%%'",
                    (repo_id,),
                )
                assert int(cur.fetchone()["n"]) == 5  # the event rows are kept

            # Re-running the migration body is a no-op, and so is the ladder.
            with conn.cursor() as cur:
                pg._apply_schema_version_14(cur)
                assert _fold_counts(cur, repo_id) == (decisions, evidence, items)
            conn.commit()
            assert pg_migrations.migrate_schema(store)["applied_versions"] == []

            # A new item in the migrated schema is not legacy.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM track WHERE repo_id = %s LIMIT 1", (repo_id,)
                )
                track_id = cur.fetchone()["id"]
            new_id = pg.create_work_item(store, sprint_ids[0], track_id, "after")
            assert pg.get_work_item(store, new_id)["legacy"] is False
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()


# ---------------------------------------------------------------------------
# Legacy items that were open at migration
# ---------------------------------------------------------------------------

# The statements a 0.3.7 runtime issues to make an item done.  They stay
# here verbatim because a rolling deploy runs that code against schema 14.
_OLD_RUNTIME_DONE_WRITES = {
    "set_work_item_status": (
        "UPDATE work_item SET status = %s, updated_at = now() WHERE repo_id = %s AND id = %s",
        lambda repo_id, item_id: ("done", repo_id, item_id),
    ),
    "force_item_done_for_carryover (maintain.carryover)": (
        "UPDATE work_item SET status = 'done', updated_at = now() WHERE repo_id = %s AND id = %s",
        lambda repo_id, item_id: (repo_id, item_id),
    ),
    "authority item.done (_handle_item)": (
        "UPDATE work_item SET status = %s, updated_at = now() "
        "WHERE repo_id = %s AND id = %s RETURNING *",
        lambda repo_id, item_id: ("done", repo_id, item_id),
    ),
}


def _legacy_item(store, status):
    """Insert a row as the schema 14 migration leaves a pre-decision item."""
    sprint_id = pg.create_sprint(store, f"Legacy-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "legacy")
    with store.conn.cursor() as cur:
        cur.execute(f"SET LOCAL {pg.LEGACY_IMPORT_SETTING} = 'on'")
        cur.execute(
            "INSERT INTO work_item (repo_id, sprint_id, track_id, title, status, legacy, "
            "aggregate_uuid) VALUES (%s, %s, %s, 'legacy', %s, true, %s) RETURNING id",
            (store.repo_id, sprint_id, track_id, status, str(uuid.uuid4())),
        )
        item_id = cur.fetchone()["id"]
    store.conn.commit()
    return sprint_id, track_id, item_id


class TestLegacyOpenItems:
    @pytest.mark.parametrize("writer", sorted(_OLD_RUNTIME_DONE_WRITES))
    @pytest.mark.parametrize("status", ["pending", "active", "blocked"])
    def test_old_runtime_done_writes_fail_loudly(self, store, writer, status):
        _sprint_id, _track_id, item_id = _legacy_item(store, status)
        statement, params = _OLD_RUNTIME_DONE_WRITES[writer]
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="cannot become done without a terminal decision",
        ):
            with store.conn.cursor() as cur:
                cur.execute(statement, params(store.repo_id, item_id))
        store.conn.rollback()
        item = pg.get_work_item(store, item_id)
        assert (item["status"], item["legacy"]) == (status, True)

    def test_open_legacy_item_closes_through_a_decision(self, store):
        _sprint_id, _track_id, item_id = _legacy_item(store, "active")
        pg.set_work_item_status(store, item_id, "done", actor="closer")
        item = pg.get_work_item(store, item_id)
        [decision] = pg.list_decisions(store, item_id)
        assert (item["status"], item["legacy"], item["resolution"]) == ("done", True, "accepted")
        assert item["terminal_decision_id"] == decision["id"]

    def test_done_legacy_item_takes_no_unevidenced_decision(self, store):
        # A re-mark (schema 16) needs a rationale and evidence; see
        # tests/pg/test_legacy_remark.py for the path that succeeds.
        _sprint_id, _track_id, item_id = _legacy_item(store, "done")
        with pytest.raises(pg.InvalidTransition, match="rationale"):
            pg.record_decision(store, item_id, "reject", actor="owner")
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_decision (repo_id, kind, work_item_id, actor) "
                "VALUES (%s, 'reject', %s, 'forger') RETURNING id",
                (store.repo_id, item_id),
            )
            forged = cur.fetchone()["id"]
        _raises_on_commit(
            store,
            "UPDATE work_item SET resolution = 'rejected', terminal_decision_id = %s "
            "WHERE repo_id = %s AND id = %s",
            (forged, store.repo_id, item_id),
            "legacy re-mark",
        )


# ---------------------------------------------------------------------------
# Copies: PG -> PG backfill, import --replace, SQLite parity of evidence
# ---------------------------------------------------------------------------


def _schema_url(schema):
    separator = "&" if "?" in _PG_URL else "?"
    return f"{_PG_URL}{separator}options=-csearch_path%3D{schema}"


class TestCopies:
    def test_remote_backfill_carries_decisions_evidence_and_legacy_rows(
        self, pg_test_scope, tmp_path
    ):
        from click.testing import CliRunner
        from sprintctl.cli import cli

        schema = "decision_copy_" + uuid.uuid4().hex
        repo_id = pg_test_scope("decision-copy")
        source_conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(source_conn)
        try:
            with source_conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            source_conn.commit()
            source = pg.PgStore(source_conn, repo_id)
            pg_migrations.migrate_schema(source)
            sprint_id, track_id, legacy_open = _legacy_item(source, "active")
            _s, _t, legacy_done = _legacy_item(source, "done")
            pg.set_work_item_status(source, legacy_open, "done", actor="closer")
            fresh = pg.create_work_item(source, sprint_id, track_id, "fresh")
            successor = pg.create_work_item(source, sprint_id, track_id, "successor")
            pg.record_decision(
                source, fresh, "supersede", actor="planner", superseded_by_item_id=successor
            )
            pg.record_decision(source, successor, "revise", actor="reviewer")
            with source_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO event (repo_id, sprint_id, source_type, actor, event_type, "
                    "payload) VALUES (%s, %s, 'actor', 'agent', 'capability-receipt-drafted', "
                    "%s) RETURNING id",
                    (repo_id, sprint_id, json.dumps({"receipt_id": "r", "b": 1})),
                )
            source_conn.commit()
            pg.refold_capability_receipts(source_conn)

            # The copy lands among other repositories' rows, so ids remap.
            result = CliRunner().invoke(
                cli,
                [
                    "remote-backfill", "--source-url", _schema_url(schema),
                    "--url", _PG_URL, "--repo-id", repo_id, "--yes", "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            summary = json.loads(result.output)
            assert summary["parity"]["work_decision"] == {"source": 3, "destination": 3}
            assert summary["parity"]["work_legacy_evidence"] == {"source": 1, "destination": 1}

            dest = pg.PgStore(psycopg.connect(_PG_URL, row_factory=dict_row), repo_id)
            try:
                items = pg.list_work_items(dest)
                decisions = {d["id"]: d for d in pg.list_decisions(dest)}
                closed = [i for i in items if i["terminal_decision_id"]]
                for item in closed:
                    assert decisions[item["terminal_decision_id"]]["work_item_id"] == item["id"]
                assert sorted(i["resolution"] for i in closed) == ["accepted", "superseded"]
                assert {(i["status"], i["legacy"], i["resolution"]) for i in items} >= {
                    ("done", True, "accepted"),   # legacy-open, decided after 14
                    ("done", True, None),         # legacy-done, no invented decision
                    ("done", False, "superseded"),
                }
                with dest.conn.cursor() as cur:
                    cur.execute(
                        "SELECT w.event_id, e.event_type, w.payload_sha256 FROM work_legacy_evidence w "
                        "JOIN event e ON e.repo_id = w.repo_id AND e.id = w.event_id "
                        "WHERE w.repo_id = %s",
                        (repo_id,),
                    )
                    [evidence] = cur.fetchall()
                assert evidence["event_type"] == "capability-receipt-drafted"
                assert evidence["payload_sha256"] == pg._decisions.legacy_evidence_digest(
                    {"receipt_id": "r", "b": 1}
                )

                # Replace refuses up front: the copy now holds append-only rows.
                again = CliRunner().invoke(
                    cli,
                    [
                        "remote-backfill", "--source-url", _schema_url(schema),
                        "--url", _PG_URL, "--repo-id", repo_id, "--yes", "--replace",
                    ],
                )
                assert again.exit_code == 1
                assert "append-only" in again.output
                assert len(pg.list_decisions(dest)) == 3
            finally:
                dest.conn.close()
        finally:
            source_conn.rollback()
            with source_conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            source_conn.commit()
            source_conn.close()

    def test_replace_is_refused_before_any_delete(self, store):
        _sprint_id, _track_id, item_id = _item(store)
        pg.record_decision(store, item_id, "withdraw", actor="owner")
        before = pg.backfill_repo_row_counts(store.conn, store.repo_id)
        with pytest.raises(ValueError, match="cannot replace repository .* append-only"):
            pg.import_ndjson(store, [], replace=True)
        assert pg.backfill_repo_row_counts(store.conn, store.repo_id) == before

    def test_recovery_snapshot_carries_evidence(self, store):
        sprint_id, _track_id, _item_id = _item(store)
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO event (repo_id, sprint_id, source_type, actor, event_type, payload) "
                "VALUES (%s, %s, 'actor', 'agent', 'capability-receipt-drafted', '{}') RETURNING id",
                (store.repo_id, sprint_id),
            )
            event_id = cur.fetchone()["id"]
        store.conn.commit()
        pg.refold_capability_receipts(store.conn)
        snapshot = pg.recover_repo_snapshot(store)
        assert [row["event_id"] for row in snapshot["work_legacy_evidence"]] == [event_id]
        assert snapshot["work_legacy_evidence"][0]["payload_sha256"] == (
            pg._decisions.legacy_evidence_digest({})
        )


class TestRefold:
    def test_receipts_written_after_14_fold_on_the_next_migrate(self, pg_test_scope):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        repo_id = pg_test_scope("decision-refold")
        store = pg.PgStore(conn, repo_id)
        try:
            pg_migrations.migrate_schema(store)
            with conn.cursor() as cur:
                # What a 0.3.7 pod writes during the rolling window.
                sprint_ids = _seed_v13_fixture(cur, repo_id, with_items=False)
            conn.commit()
            with conn.cursor() as cur:
                assert _fold_counts(cur, repo_id)[:2] == ([], {})
            conn.rollback()

            result = pg_migrations.migrate_schema(store)
            assert result["applied_versions"] == []
            assert result["capability_receipts_refolded"]["accept_decisions"] >= 2
            with conn.cursor() as cur:
                decisions, evidence, _items = _fold_counts(cur, repo_id)
            conn.rollback()
            assert [(d["sprint_id"], d["kind"]) for d in decisions] == [
                (sprint_ids[0], "accept"), (sprint_ids[1], "accept"),
            ]
            assert evidence == {
                "capability-receipt-drafted": 4,
                "capability-receipt-drafted-imported": 1,
            }
            assert pg.refold_capability_receipts(conn) == {
                "accept_decisions": 0, "drafted_evidence": 0,
            }
        finally:
            conn.close()
