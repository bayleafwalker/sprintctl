"""Releases on the SQLite backend (schema 24), mirroring PostgreSQL 15.

An execution reservation freezes what it picked up: the item revision, its
acceptance contract and its context refs, under a content digest.  Release
rows are append-only; "current release" is derived from them and from the
item's revise decisions.
"""

from __future__ import annotations

import sqlite3

import pytest

from sprintctl import db, releases
from tests.test_work_application import _application, _context
from sprintctl.application import ApplicationRejection

UUID = "a0000000-0000-4000-8000-000000000001"
EDIT_REVISION = f"item:{UUID}@description:v0@sha256:" + "e" * 64


def _item(conn, title="release me"):
    sprint_id = db.create_sprint(conn, f"Releases {title}", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "releases")
    item_id = db.create_work_item(conn, sprint_id, track_id, title)
    db.set_work_item_status(conn, item_id, "active")
    return item_id


def _reserve(conn, item_id, role="execution", **kwargs):
    return db.reserve(conn, item_id, actor="agent", session_id="s1", role=role, **kwargs)


def _edit(conn, item_id, description):
    _item_row, revision = db.get_work_item_with_edit_revision(conn, item_id)
    db.update_work_item_description(
        conn, item_id, description, expected_revision=revision, actor="editor"
    )


class TestDigest:
    def test_same_inputs_give_the_same_digest(self):
        revision = releases.release_revision(EDIT_REVISION, 0)
        refs = releases.canonical_context_refs(
            [{"ref_type": "pr", "url": "u2", "label": "b", "id": 9},
             {"ref_type": "doc", "url": "u1", "label": None, "id": 3}]
        )
        again = releases.canonical_context_refs(
            [{"ref_type": "doc", "url": "u1", "label": "", "id": 71},
             {"ref_type": "pr", "url": "u2", "label": "b", "id": 72}]
        )
        contract = releases.normalize_acceptance_contract(None)
        assert contract == {"review_required": True}
        first = releases.release_digest(UUID, revision, contract, refs)
        assert first == releases.release_digest(UUID.upper(), revision, dict(contract), again)
        assert len(first) == 64

    def test_any_component_change_gives_a_new_digest(self):
        revision = releases.release_revision(EDIT_REVISION, 0)
        contract = {"review_required": True}
        base = releases.release_digest(UUID, revision, contract, [])
        assert base != releases.release_digest(
            UUID, releases.release_revision(EDIT_REVISION, 1), contract, []
        )
        assert base != releases.release_digest(UUID, revision, {"review_required": False}, [])
        assert base != releases.release_digest(
            UUID, revision, contract, [{"ref_type": "doc", "url": "x", "label": ""}]
        )

    def test_basis_accepts_the_release_or_the_edit_revision(self):
        revision = releases.release_revision(EDIT_REVISION, 2)
        assert releases.basis_matches(revision, revision)
        assert releases.basis_matches(EDIT_REVISION, revision)
        assert not releases.basis_matches(releases.release_revision(EDIT_REVISION, 1), revision)
        with pytest.raises(ValueError, match="valid item revision"):
            releases.validate_basis("item:nope")


class TestReserveFreezesARelease:
    def test_execution_reserve_creates_exactly_one_release_and_is_idempotent(self, conn):
        item_id = _item(conn)
        db.add_ref(conn, item_id, "doc", "docs/plans/release.md", "plan")
        first = _reserve(conn, item_id)
        second = _reserve(conn, item_id, interrupt_existing=True)
        [release] = db.list_releases(conn, item_id)
        assert first["release_digest"] == second["release_digest"] == release["release_digest"]
        assert release["acceptance_contract"] == {"review_required": True}
        assert release["context_refs"] == [
            {"label": "plan", "ref_type": "doc", "url": "docs/plans/release.md"}
        ]
        assert release["item_revision"].endswith("@revise:0")
        assert release["actor"] == "agent"
        item = db.get_work_item(conn, item_id)
        assert release["release_digest"] == releases.release_digest(
            item["aggregate_uuid"],
            release["item_revision"],
            release["acceptance_contract"],
            release["context_refs"],
        )
        assert db.get_release(conn, release["release_digest"])["id"] == release["id"]
        assert db.current_release(conn, item_id)["id"] == release["id"]

    @pytest.mark.parametrize("role", ["verification", "observation"])
    def test_non_execution_reserve_creates_none(self, conn, role):
        item_id = _item(conn, f"{role} only")
        reservation = _reserve(conn, item_id, role=role)
        assert reservation["release_digest"] is None
        assert db.list_releases(conn, item_id) == []
        assert db.current_release(conn, item_id) is None

    def test_an_edit_freezes_a_new_release_on_the_next_reserve(self, conn):
        item_id = _item(conn)
        first = _reserve(conn, item_id)["release_digest"]
        _edit(conn, item_id, "sharper acceptance criteria")
        second = _reserve(conn, item_id)["release_digest"]
        assert first != second
        assert [r["release_digest"] for r in db.list_releases(conn, item_id)] == [first, second]
        assert db.current_release(conn, item_id)["release_digest"] == second

    def test_refreezing_an_older_release_makes_it_current_again(self, conn):
        item_id = _item(conn)
        first = _reserve(conn, item_id)["release_digest"]
        ref_id = db.add_ref(conn, item_id, "doc", "docs/extra.md")
        second = _reserve(conn, item_id)["release_digest"]
        db.remove_ref(conn, ref_id, item_id)
        again = _reserve(conn, item_id)["release_digest"]
        assert again == first != second
        assert len(db.list_releases(conn, item_id)) == 2
        assert db.current_release(conn, item_id)["release_digest"] == first

    def test_reserved_event_names_the_release(self, conn):
        item_id = _item(conn)
        reservation = _reserve(conn, item_id)
        [event] = [
            e for e in db.list_events(conn, db.get_work_item(conn, item_id)["sprint_id"])
            if e["event_type"] == "reservation.reserved"
        ]
        assert f'"release_digest": "{reservation["release_digest"]}"' in event["payload"]


class TestStaleRequest:
    def test_a_request_whose_item_revision_moved_is_rejected(self, conn):
        item_id = _item(conn)
        requested_against = db.item_release_revision(conn, item_id)
        _item_row, edit_revision = db.get_work_item_with_edit_revision(conn, item_id)
        _edit(conn, item_id, "changed while the request was queued")
        for basis in (requested_against, edit_revision):
            with pytest.raises(releases.StaleReleaseBasis, match="revision changed"):
                _reserve(conn, item_id, expected_revision=basis)
        assert db.list_releases(conn, item_id) == []
        assert db.list_reservations(conn, item_id) == []

    def test_a_revise_since_the_request_is_also_stale(self, conn):
        item_id = _item(conn)
        requested_against = db.item_release_revision(conn, item_id)
        db.record_decision(conn, item_id, "revise", actor="reviewer")
        with pytest.raises(releases.StaleReleaseBasis):
            _reserve(conn, item_id, expected_revision=requested_against)

    def test_a_current_request_freezes_the_revision_it_saw(self, conn):
        item_id = _item(conn)
        basis = db.item_release_revision(conn, item_id)
        reservation = _reserve(conn, item_id, expected_revision=basis)
        assert db.get_release(conn, reservation["release_digest"])["item_revision"] == basis

    def test_served_reserve_maps_a_stale_basis_to_the_stale_basis_rejection(
        self, conn, active_sprint
    ):
        track = db.get_or_create_track(conn, active_sprint["id"], "served-release")
        item_id = db.create_work_item(conn, active_sprint["id"], track, "queued")
        _item_row, edit_revision = db.get_work_item_with_edit_revision(conn, item_id)
        _edit(conn, item_id, "moved on")
        app = _application(store=conn, backend=db)
        with pytest.raises(ApplicationRejection) as rejected:
            app.invoke(
                "work.reservation.reserve",
                {"item_id": item_id, "actor": "agent", "session_id": "s1",
                 "expected_revision": edit_revision},
                _context(actor="agent"),
            )
        assert rejected.value.code == "stale-basis"
        assert db.list_reservations(conn, item_id) == []


class TestReviseAndDecisions:
    def test_revise_retires_the_current_release_and_the_next_reserve_freezes_one(self, conn):
        item_id = _item(conn)
        first = _reserve(conn, item_id)["release_digest"]
        revise = db.record_decision(conn, item_id, "revise", actor="reviewer")
        assert revise["release_digest"] == first
        assert db.current_release(conn, item_id) is None
        second = _reserve(conn, item_id)["release_digest"]
        assert second != first
        current = db.current_release(conn, item_id)
        assert current["release_digest"] == second
        assert current["item_revision"].endswith("@revise:1")
        assert db.get_release(conn, first) is not None

    def test_decision_defaults_to_the_current_release(self, conn):
        item_id = _item(conn)
        digest = _reserve(conn, item_id)["release_digest"]
        decision = db.record_decision(conn, item_id, "accept", actor="owner")
        assert decision["release_digest"] == digest

    def test_decision_without_any_release_stays_unbound(self, conn):
        item_id = _item(conn)
        assert db.record_decision(conn, item_id, "withdraw", actor="owner")["release_digest"] is None

    def test_done_alias_also_binds_the_current_release(self, conn):
        item_id = _item(conn)
        digest = _reserve(conn, item_id)["release_digest"]
        db.set_work_item_status(conn, item_id, "done", actor="closer")
        [decision] = db.list_decisions(conn, item_id)
        assert decision["release_digest"] == digest

    def test_decision_accepts_an_older_release_of_its_item(self, conn):
        item_id = _item(conn)
        first = _reserve(conn, item_id)["release_digest"]
        _edit(conn, item_id, "v2")
        _reserve(conn, item_id)
        decision = db.record_decision(
            conn, item_id, "reject", actor="owner", release_digest=first
        )
        assert decision["release_digest"] == first

    def test_decision_refuses_a_foreign_or_unknown_release(self, conn):
        item_id = _item(conn, "mine")
        other_id = _item(conn, "theirs")
        foreign = _reserve(conn, other_id)["release_digest"]
        for digest in (foreign, "f" * 64):
            with pytest.raises(releases.ReleaseMismatch, match="not a release of item"):
                db.record_decision(conn, item_id, "accept", actor="owner", release_digest=digest)
        assert db.list_decisions(conn, item_id) == []
        assert db.get_work_item(conn, item_id)["status"] == "active"


class TestAppendOnly:
    def test_release_tables_refuse_update_and_delete(self, conn):
        item_id = _item(conn)
        digest = _reserve(conn, item_id)["release_digest"]
        conn.execute(
            "INSERT INTO release_commit (release_digest, commit_sha, ref) VALUES (?, ?, ?)",
            (digest, "a" * 40, "refs/heads/main"),
        )
        conn.commit()
        for statement, params in (
            ("UPDATE work_release SET actor = 'x' WHERE release_digest = ?", (digest,)),
            ("DELETE FROM work_release WHERE release_digest = ?", (digest,)),
            ("UPDATE release_commit SET ref = 'x' WHERE release_digest = ?", (digest,)),
            ("DELETE FROM release_commit WHERE release_digest = ?", (digest,)),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement, params)
            conn.rollback()
        assert [c["commit_sha"] for c in db.list_release_commits(conn, digest)] == ["a" * 40]

    def test_release_commit_checks_its_shape(self, conn):
        item_id = _item(conn)
        digest = _reserve(conn, item_id)["release_digest"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO release_commit (release_digest, commit_sha) VALUES (?, ?)",
                (digest, "NOTHEX"),
            )
        conn.rollback()
        conn.execute(
            "INSERT INTO release_commit (release_digest, commit_sha) VALUES (?, ?)",
            (digest, "b" * 40),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO release_commit (release_digest, commit_sha) VALUES (?, ?)",
                (digest, "b" * 40),
            )
        conn.rollback()

    def test_only_execution_reservations_carry_a_release(self, conn):
        item_id = _item(conn)
        digest = _reserve(conn, item_id)["release_digest"]
        with pytest.raises(sqlite3.IntegrityError, match="only an execution reservation"):
            conn.execute(
                "INSERT INTO reservation (work_item_id, session_id, actor, role, release_digest) "
                "VALUES (?, 's', 'a', 'observation', ?)",
                (item_id, digest),
            )
        conn.rollback()


class TestMigration24:
    def test_migrating_23_to_24_keeps_rows_and_is_idempotent(self, db_path):
        conn = db.get_connection(db_path)
        foreign_keys_off = {5, 14, 15, 22}
        for version in range(1, 24):
            db._run_migration(
                conn, version, getattr(db, f"_migration_{version}"),
                foreign_keys_off=version in foreign_keys_off,
            )
        sprint_id = db.create_sprint(conn, "Pre-release", status="active")
        track_id = db.get_or_create_track(conn, sprint_id, "t")
        item_id = db.create_work_item(conn, sprint_id, track_id, "reserved before 24")
        conn.execute(
            "INSERT INTO reservation (work_item_id, session_id, actor, role) "
            "VALUES (?, 's', 'a', 'execution')",
            (item_id,),
        )
        conn.commit()
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 23

        db.init_db(conn)
        db.init_db(conn)
        with conn:
            db._migration_24(conn)

        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 24
        [row] = conn.execute("SELECT release_digest FROM reservation").fetchall()
        assert row[0] is None
        assert db.list_releases(conn, item_id) == []
        db.set_work_item_status(conn, item_id, "active")
        assert _reserve(conn, item_id)["release_digest"] is not None
        conn.close()
