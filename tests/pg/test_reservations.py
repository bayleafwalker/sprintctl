"""PostgreSQL integration tests: advisory reservations.

The reservation contract is deliberately identical on both backends, so the
overlap, role, and activity behavior pinned in tests/test_reservations.py is
re-established here against a real database -- including the concurrent
histories that only a second connection can produce.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    assert_disposable_connection,
    pg,
    _uid,
    json,
    PG_MARKS,
    _PG_URL,
    dict_row,
    psycopg,
    threading,
)

pytestmark = PG_MARKS


class TestReservations:
    def _independent_store(self, store):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        return pg.PgStore(conn=conn, repo_id=store.repo_id)

    def test_two_connections_both_reserve_and_each_sees_the_conflict(
        self, store, sprint_id, track_id
    ):
        """Overlapping execution reservations both commit; neither is refused.

        This is the concurrency evidence the protocol claims: visibility, not
        exclusivity.  The database no longer arbitrates who may reserve, so
        both histories are accepted and the second one carries the conflict
        report that makes the overlap operator-visible.
        """
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Overlap-{_uid()}")
        other = self._independent_store(store)
        try:
            first = pg.reserve(store, item_id, actor="one", session_id="session-one")
            second = pg.reserve(other, item_id, actor="two", session_id="session-two")

            assert first["conflict"] is False
            assert second["conflict"] is True
            assert second["conflict_severity"] == "warning"
            assert [row["id"] for row in second["conflicting_reservations"]] == [first["id"]]

            active = {row["id"] for row in pg.list_reservations(other, item_id, active_only=True)}
            assert active == {first["id"], second["id"]}
        finally:
            other.conn.close()

    def test_concurrent_reserves_all_commit_without_serializing_on_the_item(
        self, store, sprint_id, track_id
    ):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Race-{_uid()}")
        barrier = threading.Barrier(4)
        results: list[dict] = []
        errors: list[BaseException] = []

        def reserve(index: int) -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            try:
                actor_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
                barrier.wait(timeout=10)
                results.append(
                    pg.reserve(actor_store, item_id, actor=f"agent-{index}",
                               session_id=f"session-{index}")
                )
            except BaseException as exc:  # pragma: no cover - exercised on failure
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, [repr(error) for error in errors]
        assert len(results) == 4
        assert len(pg.list_reservations(store, item_id, active_only=True)) == 4

    def test_takeover_displaces_execution_only_and_records_why(
        self, store, sprint_id, track_id
    ):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Takeover-{_uid()}")
        first = pg.reserve(store, item_id, actor="one", session_id="session-one")
        reviewer = pg.reserve(store, item_id, actor="rev", session_id="session-rev",
                              role="verification")

        replacement = pg.reserve(store, item_id, actor="two", session_id="session-two",
                                 interrupt_existing=True)

        assert pg.get_reservation(store, first["id"])["state"] == "interrupted"
        assert pg.get_reservation(store, first["id"])["interruption_reason"] == (
            "interrupted by two (session-two)"
        )
        assert pg.get_reservation(store, reviewer["id"])["state"] == "active"
        assert [row["id"] for row in replacement["conflicting_reservations"]] == [reviewer["id"]]

    def test_roles_are_normalized_and_constrained_by_the_database(
        self, store, sprint_id, track_id
    ):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Roles-{_uid()}")
        assert pg.reserve(store, item_id, actor="a", session_id="s-a", role="execute")["role"] == "execution"
        assert pg.reserve(store, item_id, actor="b", session_id="s-b", role="coordinate")["role"] == "observation"
        with pytest.raises(ValueError, match="invalid reservation role"):
            pg.reserve(store, item_id, actor="c", session_id="s-c", role="lease")

        with store.conn.cursor() as cur:
            with pytest.raises(psycopg.Error):
                cur.execute(
                    "INSERT INTO reservation(repo_id, work_item_id, session_id, actor, role, "
                    "state, created_at, last_activity_at) "
                    "VALUES (%s, %s, 's-legacy', 'legacy', 'execute', 'active', now(), now())",
                    (store.repo_id, item_id),
                )
        store.conn.rollback()

    def test_no_unique_index_claims_exclusivity_on_active_execution(self, store):
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'reservation'"
            )
            definitions = [row["indexdef"] for row in cur.fetchall()]
        store.conn.rollback()
        # Identity uniqueness (the primary key and the repo-scoped id) is
        # expected; what must not exist is a partial unique index over the
        # work item, which is how exclusivity was previously asserted as a
        # database law.
        exclusivity = [
            definition for definition in definitions
            if "UNIQUE" in definition and "work_item_id" in definition
        ]
        assert exclusivity == []

    def test_activity_follows_the_session_that_holds_the_reservation(
        self, store, sprint_id, track_id
    ):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Activity-{_uid()}")
        row = pg.reserve(store, item_id, actor="one", session_id="session-one")
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation SET last_activity_at = now() - interval '9 hours' "
                "WHERE repo_id = %s AND id = %s",
                (store.repo_id, row["id"]),
            )
        store.conn.commit()
        assert pg.get_reservation(store, row["id"])["stale"] is True

        assert pg.note_session_activity(store, item_id, session_id="other-session") == []
        assert pg.note_session_activity(store, item_id, session_id=None) == []
        assert pg.get_reservation(store, row["id"])["stale"] is True

        bumped = pg.note_session_activity(store, item_id, session_id="session-one")
        assert [entry["id"] for entry in bumped] == [row["id"]]
        assert pg.get_reservation(store, row["id"])["stale"] is False


class TestReservationRoleMigration:
    """Schema 12 upgrade behavior, exercised on a throwaway schema.

    The shared integration database is already migrated, so the pre-12 shape
    is rebuilt inside a temporary schema on the search path and rolled back;
    this keeps a real PostgreSQL parser in the loop for DDL that only ever
    runs once per deployment.
    """

    PRE_V12_DDL = """
        CREATE TABLE reservation (
            repo_id text NOT NULL,
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            work_item_id bigint NOT NULL,
            session_id text NOT NULL,
            actor text NOT NULL,
            role text NOT NULL CHECK (role IN ('inspect','execute','review','coordinate')),
            state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','released','interrupted')),
            created_at timestamptz NOT NULL DEFAULT now(),
            last_activity_at timestamptz NOT NULL DEFAULT now(),
            released_at timestamptz,
            interruption_reason text,
            correlation_ref text,
            UNIQUE(repo_id, id)
        );
        CREATE UNIQUE INDEX idx_reservation_active_execute
            ON reservation(repo_id, work_item_id) WHERE state = 'active' AND role = 'execute';
    """

    def _seed(self, cur):
        cur.execute("CREATE SCHEMA pre_v12_upgrade")
        cur.execute("SET LOCAL search_path = pre_v12_upgrade")
        cur.execute(self.PRE_V12_DDL)
        for index, role in enumerate(("execute", "review", "inspect", "coordinate")):
            cur.execute(
                "INSERT INTO reservation(repo_id, work_item_id, session_id, actor, role) "
                "VALUES ('repo', 1, %s, 'agent', %s)",
                (f"session-{index}", role),
            )

    def test_legacy_roles_fold_in_and_exclusivity_stops_being_a_database_law(self, store):
        with store.conn.cursor() as cur:
            try:
                self._seed(cur)

                # Before: the index, not the operator, decided who may work.
                with store.conn.transaction(force_rollback=True):
                    with pytest.raises(psycopg.errors.UniqueViolation):
                        cur.execute(
                            "INSERT INTO reservation(repo_id, work_item_id, session_id, actor, role) "
                            "VALUES ('repo', 1, 'session-rival', 'rival', 'execute')"
                        )

                pg._apply_schema_version_12(cur)

                cur.execute("SELECT session_id, role FROM reservation ORDER BY session_id")
                assert {row["session_id"]: row["role"] for row in cur.fetchall()} == {
                    "session-0": "execution",
                    "session-1": "verification",
                    "session-2": "observation",
                    "session-3": "observation",
                }

                # After: a second execution reservation is ordinary data.
                cur.execute(
                    "INSERT INTO reservation(repo_id, work_item_id, session_id, actor, role) "
                    "VALUES ('repo', 1, 'session-rival', 'rival', 'execution')"
                )
                with store.conn.transaction(force_rollback=True):
                    with pytest.raises(psycopg.errors.CheckViolation):
                        cur.execute(
                            "INSERT INTO reservation(repo_id, work_item_id, session_id, actor, role) "
                            "VALUES ('repo', 1, 'session-old', 'old', 'execute')"
                        )

                cur.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'pre_v12_upgrade' AND tablename = 'reservation'"
                )
                names = {row["indexname"] for row in cur.fetchall()}
                assert "idx_reservation_active_execute" not in names
                assert {"idx_reservation_item_state", "idx_reservation_session_active"} <= names
            finally:
                store.conn.rollback()

    def test_upgrade_step_is_replayable(self, store):
        with store.conn.cursor() as cur:
            try:
                self._seed(cur)
                pg._apply_schema_version_12(cur)
                pg._apply_schema_version_12(cur)
                cur.execute(
                    "SELECT count(*) AS n FROM pg_constraint "
                    "WHERE conrelid = 'reservation'::regclass AND contype = 'c' "
                    "AND pg_get_constraintdef(oid) LIKE '%execution%'"
                )
                assert cur.fetchone()["n"] == 1
            finally:
                store.conn.rollback()


class TestReservationAuditTrail:
    """Lifecycle events, which are the only durable record of who did what.

    Reservations carry no credential, so if the event trail is missing an
    operator cannot reconstruct who interrupted whom. SQLite pins the same
    trail in tests/test_reservations.py; this is the parity half.
    """

    @staticmethod
    def _payload(event):
        # PostgreSQL returns event payloads as JSON text (pinned by
        # tests/pg/test_event.py::test_payload_is_string), so decode rather
        # than assume the SQLite-side dict.
        payload = event["payload"]
        return json.loads(payload) if isinstance(payload, str) else payload

    def _reservation_events(self, store, sprint_id):
        # list_events reads newest-first; sort by id so the assertion is
        # about the order things happened, not the read surface's convention.
        return sorted(
            (event for event in pg.list_events(store, sprint_id)
             if event["event_type"].startswith("reservation.")),
            key=lambda event: event["id"],
        )

    def test_reserve_touch_reassign_release_leave_an_attributable_trail(
        self, store, sprint_id, track_id
    ):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Audit-{_uid()}")
        row = pg.reserve(store, item_id, actor="one", session_id="session-one")
        pg.reassign_reservation(store, row["id"], actor="two", session_id="session-two")
        pg.release_reservation(store, row["id"], actor="two")

        events = self._reservation_events(store, sprint_id)
        assert [event["event_type"] for event in events] == [
            "reservation.reserved",
            "reservation.reassigned",
            "reservation.released",
        ]
        assert [event["actor"] for event in events] == ["one", "two", "two"]
        assert all(event["work_item_id"] == item_id for event in events)

    def test_takeover_and_sweep_record_who_and_why(self, store, sprint_id, track_id):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Audit2-{_uid()}")
        first = pg.reserve(store, item_id, actor="one", session_id="session-one")
        replacement = pg.reserve(store, item_id, actor="two", session_id="session-two",
                                 interrupt_existing=True)

        interrupted = [
            event for event in self._reservation_events(store, sprint_id)
            if event["event_type"] == "reservation.interrupted"
        ]
        assert len(interrupted) == 1
        assert interrupted[0]["actor"] == "two"
        assert self._payload(interrupted[0])["reservation_id"] == first["id"]
        assert self._payload(interrupted[0])["reason"] == "explicit-takeover"
        assert self._payload(interrupted[0])["replacement_id"] == replacement["id"]

        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation SET last_activity_at = now() - interval '30 days' "
                "WHERE repo_id = %s AND id = %s",
                (store.repo_id, replacement["id"]),
            )
        store.conn.commit()
        pg.sweep_stale_reservations(store)

        swept = [
            event for event in self._reservation_events(store, sprint_id)
            if event["event_type"] == "reservation.interrupted"
            and self._payload(event)["reservation_id"] == replacement["id"]
        ]
        assert len(swept) == 1
        assert swept[0]["actor"] == "maintenance"
        assert self._payload(swept[0])["reason"] == "7-day inactivity sweep"


class TestIntegrityParity:
    def test_both_backends_report_the_same_repository_tables(self, store, tmp_path):
        """An operator must not see a different repository per backend.

        The row-count sets are the operator-visible surface of `doctor` and
        `db integrity`; when they diverge, a missing relation reads as a
        missing feature rather than as a gap in the report.
        """
        from sprintctl import db as sqlite_backend

        connection = sqlite_backend.get_connection(tmp_path / "parity.db")
        sqlite_backend.init_db(connection)
        try:
            local = set(sqlite_backend.check_integrity(connection)["table_counts"])
        finally:
            connection.close()

        assert set(pg.check_integrity(store)["table_counts"]) == local
