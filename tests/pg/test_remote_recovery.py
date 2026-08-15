"""PostgreSQL integration tests: RecoverFromRemote, RemoteBackfill, RemoteBackfillCli.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

from tests.pg._shared import (
    db,
    pg,
    cli,
    _uid,
    PG_MARKS,
    _PG_URL,
    json,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


class TestRecoverFromRemote:
    def test_snapshot_then_write_preserves_ids_and_passes_integrity(
        self, tmp_path, store, sprint_id, track_id, work_item_id,
    ):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        work_item_id=work_item_id,
                        payload={"summary": "recovery source event"})
        pg.reserve(store, work_item_id, actor="ag", session_id="session-recovery")
        # Archive-only. The live claim relation is gone as of migration 10;
        # claim_history survives as read-only evidence, and
        # write_recovery_snapshot still strips ownership out of it, so the
        # row is seeded directly.
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claim_history (repo_id, work_item_id, agent, exclusive, "
                "expires_at, claim_token, status) "
                "VALUES (%s, %s, 'ag', true, now() + interval '300 seconds', "
                "'legacy-token', 'active')",
                (store.repo_id, work_item_id),
            )
        store.conn.commit()
        pg.add_ref(store, work_item_id, "doc", "docs/plans/x.md")
        other_item = pg.create_work_item(store, sprint_id, track_id, f"Dep-{_uid()}")
        pg.add_dep(store, work_item_id, other_item)

        snapshot = pg.recover_repo_snapshot(store)
        assert any(row["id"] == sprint_id for row in snapshot["sprint"])
        assert any(row["id"] == work_item_id for row in snapshot["work_item"])
        assert snapshot["reservation"] and snapshot["ref"] and snapshot["dep"]
        assert snapshot["claim_history"]

        dest = tmp_path / "recovery.db"
        conn = db.get_connection(dest)
        try:
            db.init_db(conn)
            counts = db.write_recovery_snapshot(
                conn,
                snapshot,
                provenance={"recovered_at": "2026-07-24T00:00:00Z",
                            "source_repo_id": store.repo_id},
            )
            assert counts["sprint"] == len(snapshot["sprint"])
            assert counts["work_item"] == len(snapshot["work_item"])

            assert db.get_sprint(conn, sprint_id) is not None
            assert db.get_work_item(conn, work_item_id)["id"] == work_item_id
            assert db.get_work_item(conn, other_item)["id"] == other_item

            report = db.check_integrity(conn)
            assert report["ok"] is True, report

            reservation_row = conn.execute(
                "SELECT actor, state FROM reservation WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            assert reservation_row["actor"] == "ag"
            # Ownership never survives recovery: the row is kept for audit,
            # but a recovered database is a new authority instance and must
            # not read as still held by the pre-recovery session.
            assert reservation_row["state"] == "interrupted"

            claim_row = conn.execute(
                "SELECT exclusive, status, claim_token FROM claim_history WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            assert claim_row["exclusive"] == 1
            # ownership is never restored: an archived claim comes back
            # closed, with its bearer token stripped
            assert claim_row["status"] == "expired"
            assert claim_row["claim_token"] is None

            # Provenance is one repo-level record, not one event per sprint.
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM event WHERE event_type = 'recovery.completed'"
            ).fetchone()["n"] == 0
            provenance_rows = conn.execute(
                "SELECT recovered_at, source_repo_id, reservations_interrupted"
                " FROM recovery_record"
            ).fetchall()
            assert len(provenance_rows) == 1
            assert provenance_rows[0]["source_repo_id"] == store.repo_id
            assert provenance_rows[0]["reservations_interrupted"] == 1

            event_row = conn.execute(
                "SELECT payload FROM event WHERE work_item_id = ? AND event_type = 'note'",
                (work_item_id,),
            ).fetchone()
            assert json.loads(event_row["payload"])["summary"] == "recovery source event"

            # A subsequent synthetic recovery.completed event must not collide
            # with any restored event id.
            max_restored_event_id = max(row["id"] for row in snapshot["event"])
            new_event_id = db.create_event(
                conn, sprint_id, "sprintctl", "recovery.completed", source_type="system",
            )
            assert new_event_id > max_restored_event_id
        finally:
            conn.close()

    def test_refuses_to_overwrite_existing_output(self, tmp_path, runner, monkeypatch):
        (tmp_path / ".git").mkdir()
        dest = tmp_path / "existing.db"
        dest.write_text("not a real db")
        monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
        monkeypatch.setenv("SPRINTCTL_URL", _PG_URL)
        result = runner.invoke(
            cli,
            [
                "--repo-id", tmp_path.name,
                "--allow-markerless-nonlocal",
                "db", "recover-from-remote",
                "--output", str(dest),
            ],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output


# ---------------------------------------------------------------------------
# NDJSON round-trip (export sqlite → import pg)
# ---------------------------------------------------------------------------

class TestRemoteBackfill:
    """export_from_postgres + import_ndjson(remap_ids=True): the PostgreSQL-to-
    PostgreSQL path `sprintctl remote-backfill` uses to copy a repository's
    history out of a separate, already-deployed authority. Both connections
    point at the same disposable database in these tests (there is only one
    to test against), but the functions themselves are connection-agnostic --
    export_from_postgres only ever reads, import_ndjson only ever writes to
    the store it's given."""

    def test_export_from_postgres_matches_backfill_row_counts(self, store, pg_test_scope):
        repo_id = pg_test_scope("backfill-export")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "Backfill Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        item_id = pg.create_work_item(source_store, sprint_id, track_id, "Backfill Item")
        pg.add_ref(source_store, item_id, "pr", "https://github.com/org/repo/pull/1")
        pg.create_event(
            source_store, sprint_id, actor="a", event_type="note",
            source_type="actor", work_item_id=item_id, payload={"summary": "x"},
        )

        counts = pg.backfill_repo_row_counts(store.conn, repo_id)
        assert counts["sprint"] == 1
        assert counts["track"] == 1
        assert counts["work_item"] == 1
        assert counts["ref"] == 1
        assert counts["event"] == 1
        assert counts["claim_history"] == 0
        assert counts["dep"] == 0

        records = pg.export_from_postgres(store.conn, repo_id)
        assert len(records) == sum(counts.values())
        by_table = {}
        for record in records:
            by_table.setdefault(record["table"], []).append(record)
            assert record["repo_id"] == repo_id
            assert "repo_id" not in record["data"]
        assert len(by_table["sprint"]) == 1
        assert by_table["sprint"][0]["data"]["name"] == "Backfill Sprint"
        assert by_table["ref"][0]["data"]["url"] == "https://github.com/org/repo/pull/1"

    def test_backfill_round_trip_remaps_ids_and_preserves_parity(self, store, pg_test_scope):
        repo_id = pg_test_scope("backfill-roundtrip")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "RT Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        item_id = pg.create_work_item(source_store, sprint_id, track_id, "RT Item")
        pg.add_ref(source_store, item_id, "pr", "https://github.com/org/repo/pull/2")
        pg.create_event(
            source_store, sprint_id, actor="a", event_type="note",
            source_type="actor", work_item_id=item_id, payload={"summary": "rt"},
        )

        source_counts = pg.backfill_repo_row_counts(store.conn, repo_id)
        records = pg.export_from_postgres(store.conn, repo_id)

        dest_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row), repo_id=repo_id)
        try:
            # replace=True simulates copying into a genuinely separate,
            # already-populated-by-source destination: delete the captured
            # rows (records already holds their data in memory), then
            # reinsert with fresh remapped IDs -- exactly what
            # remote_backfill_cmd does against two real databases.
            imported = pg.import_ndjson(dest_store, records, replace=True, remap_ids=True)
            dest_counts = pg.backfill_repo_row_counts(dest_store.conn, repo_id)
            assert dest_counts == source_counts
            assert imported["sprint"] == 1
            assert imported["work_item"] == 1

            new_item = pg.get_work_item(dest_store, pg.list_work_items(dest_store, sprint_id=None)[0]["id"])
            assert new_item["id"] != item_id, "remap_ids=True must not preserve the source's literal id"
            assert new_item["title"] == "RT Item"
        finally:
            dest_store.conn.close()

    def test_remote_backfill_row_counts_ignore_other_repos(self, store, pg_test_scope):
        repo_a = pg_test_scope("backfill-scope-a")
        repo_b = pg_test_scope("backfill-scope-b")
        store_a = pg.PgStore(conn=store.conn, repo_id=repo_a)
        store_b = pg.PgStore(conn=store.conn, repo_id=repo_b)
        pg.create_sprint(store_a, "A Sprint", "G", status="active")
        pg.create_sprint(store_b, "B Sprint 1", "G", status="active")
        pg.create_sprint(store_b, "B Sprint 2", "G", status="active")

        assert pg.backfill_repo_row_counts(store.conn, repo_a)["sprint"] == 1
        assert pg.backfill_repo_row_counts(store.conn, repo_b)["sprint"] == 2
        records_a = pg.export_from_postgres(store.conn, repo_a)
        assert len([r for r in records_a if r["table"] == "sprint"]) == 1

class TestRemoteBackfillCli:
    """CLI wiring for `sprintctl remote-backfill`. --source-url and --url both
    point at the same disposable Postgres in these tests -- the command
    itself does not require them to differ, and in production they name two
    genuinely separate deployments."""

    def test_dry_run_reports_source_counts_without_writing(self, store, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-dry")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        pg.create_sprint(source_store, "CLI Dry Sprint", "G", status="active")

        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--dry-run",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["source_counts"]["sprint"] == 1

    def test_missing_repo_data_at_source_is_an_error(self, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-missing")
        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--dry-run",
        ])
        assert result.exit_code != 0
        assert "no rows found" in result.output.lower()

    def test_existing_destination_data_requires_replace(self, store, pg_test_scope, runner):
        # Since --source-url and --url are the same disposable database in
        # this test, any pre-existing row for repo_id already satisfies the
        # "destination has data" guard -- no separate backfill run needed to
        # set it up.
        repo_id = pg_test_scope("backfill-cli-guard")
        guard_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        pg.create_sprint(guard_store, "Guard Sprint", "G", status="active")

        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--yes",
        ])
        assert result.exit_code != 0
        assert "already has data" in result.output.lower()

    def test_full_backfill_with_replace_reports_parity(self, store, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-full")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "CLI Full Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        pg.create_work_item(source_store, sprint_id, track_id, "CLI Full Item")

        # --replace lets the (trivially true, same-database-in-this-test)
        # existing-destination-data guard through; source_counts is computed
        # from the CLI's own pre-write read, before import_ndjson's replace
        # delete+reinsert -- so parity still means what it means in
        # production, where source and destination are genuinely separate.
        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--replace",
            "--yes",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["parity_ok"] is True
        assert payload["parity"]["sprint"] == {"source": 1, "destination": 1}
        assert payload["parity"]["track"] == {"source": 1, "destination": 1}
        assert payload["parity"]["work_item"] == {"source": 1, "destination": 1}


# ---------------------------------------------------------------------------
# Maintain
# ---------------------------------------------------------------------------
