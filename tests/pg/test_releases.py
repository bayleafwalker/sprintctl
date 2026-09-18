"""PostgreSQL integration tests: Releases (S3 PR2, schema 15).

An execution reservation freezes what it picked up under a content digest, in
the reservation's own transaction.  Release rows are append-only; a revise
decision retires the current release, and decisions default to it.
"""
from __future__ import annotations

import io

import pytest

from sprintctl import authority, db as _db, outbox, releases
from tests.pg._shared import (
    pg,
    pg_migrations,
    PG_MARKS,
    _PG_URL,
    _append_authority_command,
    _uid,
    assert_disposable_connection,
    json,
    uuid,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


def _item(store, title=None):
    sprint_id = pg.create_sprint(store, f"Releases-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "releases")
    item_id = pg.create_work_item(store, sprint_id, track_id, title or f"item {_uid()}")
    pg.set_work_item_status(store, item_id, "active")
    return item_id


def _reserve(store, item_id, role="execution", **kwargs):
    return pg.reserve(store, item_id, actor="agent", session_id="s1", role=role, **kwargs)


def _edit(backend, store, item_id, description):
    _row, revision = backend.get_work_item_with_edit_revision(store, item_id)
    backend.update_work_item_description(
        store, item_id, description, expected_revision=revision, actor="editor"
    )


class TestReserveFreezesARelease:
    def test_execution_reserve_creates_exactly_one_release_and_is_idempotent(self, store):
        item_id = _item(store)
        pg.add_ref(store, item_id, "doc", "docs/plans/release.md", "plan")
        first = _reserve(store, item_id)
        second = _reserve(store, item_id)
        [release] = pg.list_releases(store, item_id)
        assert first["release_digest"] == second["release_digest"] == release["release_digest"]
        assert release["acceptance_contract"] == {"review_required": True}
        assert release["context_refs"] == [
            {"label": "plan", "ref_type": "doc", "url": "docs/plans/release.md"}
        ]
        item = pg.get_work_item(store, item_id)
        assert release["release_digest"] == releases.release_digest(
            item["aggregate_uuid"], release["item_revision"],
            release["acceptance_contract"], release["context_refs"],
        )
        assert pg.get_release(store, release["release_digest"])["id"] == release["id"]
        assert pg.current_release(store, item_id)["id"] == release["id"]

    @pytest.mark.parametrize("role", ["verification", "observation"])
    def test_non_execution_reserve_creates_none(self, store, role):
        item_id = _item(store)
        assert _reserve(store, item_id, role=role)["release_digest"] is None
        assert pg.list_releases(store, item_id) == []
        assert pg.current_release(store, item_id) is None

    def test_an_edit_freezes_a_new_release_on_the_next_reserve(self, store):
        item_id = _item(store)
        first = _reserve(store, item_id)["release_digest"]
        _edit(pg, store, item_id, "sharper acceptance criteria")
        second = _reserve(store, item_id)["release_digest"]
        assert first != second
        assert pg.current_release(store, item_id)["release_digest"] == second

    def test_a_stale_queued_request_is_rejected(self, store):
        item_id = _item(store)
        requested_against = pg.item_release_revision(store, item_id)
        _row, edit_revision = pg.get_work_item_with_edit_revision(store, item_id)
        _edit(pg, store, item_id, "changed while the request was queued")
        for basis in (requested_against, edit_revision):
            with pytest.raises(releases.StaleReleaseBasis, match="revision changed"):
                _reserve(store, item_id, expected_revision=basis)
        assert pg.list_releases(store, item_id) == []
        assert pg.list_reservations(store, item_id) == []
        current = pg.item_release_revision(store, item_id)
        frozen = _reserve(store, item_id, expected_revision=current)["release_digest"]
        assert pg.get_release(store, frozen)["item_revision"] == current


class TestReviseAndDecisions:
    def test_revise_then_reserve_creates_a_new_current_release(self, store):
        item_id = _item(store)
        first = _reserve(store, item_id)["release_digest"]
        revise = pg.record_decision(store, item_id, "revise", actor="reviewer")
        assert revise["release_digest"] == first
        assert pg.current_release(store, item_id) is None
        second = _reserve(store, item_id)["release_digest"]
        assert second != first
        current = pg.current_release(store, item_id)
        assert current["release_digest"] == second
        assert current["item_revision"].endswith("@revise:1")

    def test_decision_defaults_to_the_current_release(self, store):
        item_id = _item(store)
        digest = _reserve(store, item_id)["release_digest"]
        pg.set_work_item_status(store, item_id, "done", actor="closer")
        [decision] = pg.list_decisions(store, item_id)
        assert decision["release_digest"] == digest

    def test_decision_refuses_a_foreign_release(self, store):
        item_id = _item(store)
        foreign = _reserve(store, _item(store))["release_digest"]
        for digest in (foreign, "f" * 64):
            with pytest.raises(releases.ReleaseMismatch, match="not a release of item"):
                pg.record_decision(store, item_id, "accept", actor="owner", release_digest=digest)
        assert pg.list_decisions(store, item_id) == []
        assert pg.get_work_item(store, item_id)["status"] == "active"

    def test_authority_decision_command_refuses_a_foreign_release(self, store, tmp_path):
        item_id = _item(store)
        foreign = _reserve(store, _item(store))["release_digest"]
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "release-authority.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="decision.record",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"kind": "reject", "release_digest": foreign},
            )
            refused = authority.arbitrate_command(store, command)
        finally:
            producer.close()
        assert refused.accepted is False
        assert refused.reason_code == "invalid-command"
        assert pg.list_decisions(store, item_id) == []


class TestAppendOnly:
    def test_release_tables_refuse_update_delete_and_truncate(self, store):
        item_id = _item(store)
        digest = _reserve(store, item_id)["release_digest"]
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO release_commit (repo_id, release_digest, commit_sha) "
                "VALUES (%s, %s, %s)",
                (store.repo_id, digest, "a" * 40),
            )
        store.conn.commit()
        for statement in (
            "UPDATE work_release SET actor = 'x' WHERE repo_id = %s",
            "DELETE FROM work_release WHERE repo_id = %s",
            "UPDATE release_commit SET ref = 'x' WHERE repo_id = %s",
            "DELETE FROM release_commit WHERE repo_id = %s",
            # work_release is FK-referenced, so only its dependent can be
            # truncated without CASCADE; both carry the same trigger.
            "TRUNCATE release_commit",
        ):
            with pytest.raises(psycopg.errors.Error, match="append-only"):
                with store.conn.cursor() as cur:
                    cur.execute(statement, (store.repo_id,) if "%s" in statement else None)
            store.conn.rollback()
        assert [c["commit_sha"] for c in pg.list_release_commits(store, digest)] == ["a" * 40]
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT tgname FROM pg_trigger WHERE tgname IN "
                "('work_release_no_truncate', 'release_commit_no_truncate') ORDER BY tgname"
            )
            assert [row["tgname"] for row in cur.fetchall()] == [
                "release_commit_no_truncate", "work_release_no_truncate",
            ]
        store.conn.rollback()

    def test_release_commit_and_reservation_shape_checks(self, store):
        item_id = _item(store)
        digest = _reserve(store, item_id)["release_digest"]
        for statement, params in (
            (
                "INSERT INTO release_commit (repo_id, release_digest, commit_sha) "
                "VALUES (%s, %s, 'NOTHEX')",
                (store.repo_id, digest),
            ),
            (
                "INSERT INTO release_commit (repo_id, release_digest, commit_sha) "
                "VALUES (%s, %s, %s)",
                (store.repo_id, "d" * 64, "a" * 40),
            ),
            (
                "INSERT INTO reservation (repo_id, work_item_id, session_id, actor, role, "
                "release_digest) VALUES (%s, %s, 's', 'a', 'observation', %s)",
                (store.repo_id, item_id, digest),
            ),
        ):
            with pytest.raises(psycopg.errors.IntegrityError):
                with store.conn.cursor() as cur:
                    cur.execute(statement, params)
            store.conn.rollback()


class TestSchema15Migration:
    def test_migrating_14_to_15_is_idempotent(self, pg_test_scope, monkeypatch):
        schema = "release_v15_" + uuid.uuid4().hex
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        repo_id = pg_test_scope("release-migration")
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            store = pg.PgStore(conn, repo_id)
            with monkeypatch.context() as patch:
                patch.setattr(pg, "_apply_schema_version_15", lambda cur: None)
                pg_migrations.migrate_schema(store)
            with conn.cursor() as cur:
                cur.execute("UPDATE schema_version SET version = 14")
                cur.execute("SELECT to_regclass('work_release') AS relation")
                assert cur.fetchone()["relation"] is None
            conn.commit()
            sprint_id = pg.create_sprint(store, "v14", status="active")
            track_id = pg.get_or_create_track(store, sprint_id, "t")
            item_id = pg.create_work_item(store, sprint_id, track_id, "reserved before 15")
            pg.set_work_item_status(store, item_id, "active")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO reservation (repo_id, work_item_id, session_id, actor, role) "
                    "VALUES (%s, %s, 's', 'a', 'execution')",
                    (repo_id, item_id),
                )
            conn.commit()

            assert pg_migrations.migrate_schema(store)["applied_versions"] == [15]
            with conn.cursor() as cur:
                pg._apply_schema_version_15(cur)
            conn.commit()
            assert pg_migrations.migrate_schema(store)["applied_versions"] == []

            with conn.cursor() as cur:
                cur.execute("SELECT release_digest FROM reservation WHERE repo_id = %s", (repo_id,))
                assert [row["release_digest"] for row in cur.fetchall()] == [None]
            conn.commit()
            assert pg.list_releases(store, item_id) == []
            assert _reserve(store, item_id)["release_digest"] is not None
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()


class TestBackendParity:
    def test_sqlite_releases_carry_to_postgres_and_both_freeze_the_same_digest(
        self, pg_test_scope, tmp_path
    ):
        conn = _db.get_connection(tmp_path / "releases.db")
        _db.init_db(conn)
        sid = _db.create_sprint(conn, "Release parity", status="active")
        tid = _db.get_or_create_track(conn, sid, "eng")
        item_id = _db.create_work_item(conn, sid, tid, "parity")
        _db.add_ref(conn, item_id, "pr", "https://example.invalid/pr/1", "pr")
        first = _db.reserve(conn, item_id, actor="agent", session_id="s1")["release_digest"]
        conn.execute(
            "INSERT INTO release_commit (release_digest, commit_sha, ref) VALUES (?, ?, ?)",
            (first, "c" * 40, "refs/heads/feature"),
        )
        conn.commit()
        _db.record_decision(conn, item_id, "revise", actor="reviewer")

        rt_repo_id = pg_test_scope("release-parity")
        buf = io.StringIO()
        pg.export_ndjson(conn, rt_repo_id, buf)
        rt_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row), repo_id=rt_repo_id)
        pg.init_db(rt_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            counts = pg.import_ndjson(rt_store, records, remap_ids=True)
            assert (counts["work_release"], counts["release_commit"]) == (1, 1)
            [pg_item] = pg.list_work_items(rt_store)
            pg_item_id = pg_item["id"]
            [carried] = pg.list_releases(rt_store, pg_item_id)
            [local] = _db.list_releases(conn, item_id)
            for key in ("release_digest", "item_revision", "acceptance_contract", "context_refs"):
                assert carried[key] == local[key]
            assert [c["commit_sha"] for c in pg.list_release_commits(rt_store, first)] == ["c" * 40]
            reservations = pg.list_reservations(rt_store, pg_item_id, active_only=False)
            assert [r["release_digest"] for r in reservations] == [first]
            [decision] = pg.list_decisions(rt_store, pg_item_id)
            assert decision["release_digest"] == first

            # The revise carried too: neither backend has a current release,
            # and the same next step freezes the same new release on both.
            assert pg.current_release(rt_store, pg_item_id) is None
            assert _db.current_release(conn, item_id) is None
            assert pg.item_release_revision(rt_store, pg_item_id) == _db.item_release_revision(
                conn, item_id
            )
            _edit(_db, conn, item_id, "after review")
            _edit(pg, rt_store, pg_item_id, "after review")
            local_next = _db.reserve(conn, item_id, actor="agent", session_id="s2")
            pg_next = pg.reserve(rt_store, pg_item_id, actor="agent", session_id="s2")
            assert local_next["release_digest"] == pg_next["release_digest"] != first
            assert (
                _db.current_release(conn, item_id)["release_digest"]
                == pg.current_release(rt_store, pg_item_id)["release_digest"]
            )
        finally:
            rt_store.conn.close()
            conn.close()
