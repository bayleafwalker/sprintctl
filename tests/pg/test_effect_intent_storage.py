"""Storage and edge behaviour of the effect-intent store beyond the frozen
oracle (tests/pg/test_effect_intent.py): the schema 19 migration, the
immutability trigger's transition table, replay answering with the current
intent, and argument refusals.  agentops#2541."""
from __future__ import annotations

import uuid

import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    assert_disposable_connection,
    dict_row,
    psycopg,
)
from tests.pg.test_effect_intent import (
    ACCEPT,
    ACCEPTOR,
    LIST_PROPOSED,
    PROPOSE,
    PROPOSER,
    SECOND_ACCEPTOR as SECOND,
    _READ_EFFECTS,
    _context,
    _accept,
    _app,
    _binding,
    _fresh,
    _get,
    _invoke,
    _items,
    _key,
    _mark_applied,
    _propose,
    _propose_args,
    _refused,
    _reject,
    _run,
)

pytestmark = PG_MARKS


class TestSchema19Migration:
    def test_migrating_from_schema_2_installs_exactly_the_recorded_shape(self, store):
        schema = "migration_19_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            applied = pg_migrations.migrate_schema(pg.PgStore(conn, "migration-19"))
            # Schema 20 (agentops#2525) follows 19 in the ladder.
            assert 19 in applied["applied_versions"]
            assert applied["to_version"] == pg_migrations.CURRENT_SCHEMA_VERSION
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                assert pg._foreign_relations(
                    cur, pg._SCHEMA_19_TABLES, pg._SCHEMA_19_INDEXES, pg._SCHEMA_19_INDEX_SHAPES
                ) == []
                # Additive and idempotent: re-applying over its own shape is a no-op.
                pg._apply_schema_version_19(cur)
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    @pytest.mark.parametrize("table", sorted(pg._SCHEMA_19_TABLES))
    def test_a_foreign_table_with_matching_columns_is_refused(self, store, table):
        schema = f"migration_19_{table}_" + uuid.uuid4().hex
        columns, _ = pg._SCHEMA_19_TABLES[table]
        ddl = ", ".join(f"{n} {t} {'NOT NULL' if nn else ''}" for n, t, nn in columns)
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
            cur.execute(f"CREATE TABLE {table} ({ddl})")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match=table):
                pg_migrations.migrate_schema(pg.PgStore(conn, f"migration-19-{table}"))
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_version")
                assert cur.fetchone()["version"] == 2
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()


class TestGuardTrigger:
    """The trigger is the storage backstop of INV-E1, independent of the
    application that normally writes the row."""

    def _update(self, store, intent_id: str, assignment: str, *params) -> None:
        try:
            with store.conn.cursor() as cur:
                cur.execute(
                    f"UPDATE work_effect_intent SET {assignment} "
                    "WHERE repo_id = %s AND intent_id = %s",
                    (*params, store.repo_id, intent_id),
                )
            store.conn.commit()
        finally:
            store.conn.rollback()

    def _state(self, store, intent_id: str) -> str:
        return _get(store, intent_id)["state"]

    @pytest.mark.parametrize(
        "assignment,params",
        [
            ("title = %s", ("another title",)),
            ("unified_diff = %s", ("--- a\n+++ b\n",)),
            ("revision = %s", (2,)),
            ("proposer_principal = %s", ("github:999:0",)),
            ("work_item_id = work_item_id + 1", ()),
        ],
    )
    def test_no_content_column_changes_even_while_proposed(self, store, assignment, params):
        _item, _run_id, intent = _fresh(store)
        with pytest.raises(psycopg.errors.CheckViolation):
            self._update(store, intent["intent_id"], assignment, *params)
        assert _get(store, intent["intent_id"]) == intent

    def test_a_proposed_intent_cannot_be_edited_without_a_transition(self, store):
        _item, _run_id, intent = _fresh(store)
        with pytest.raises(psycopg.errors.CheckViolation):
            self._update(store, intent["intent_id"], "reject_reason = %s", "sneaky")
        assert self._state(store, intent["intent_id"]) == "proposed"

    def test_proposed_cannot_jump_to_applied(self, store):
        _item, _run_id, intent = _fresh(store)
        with pytest.raises(psycopg.errors.CheckViolation):
            self._update(
                store, intent["intent_id"],
                "state = 'applied', applied_at = now(), applier_principal = 'x', "
                "applied_commit_sha = %s, applied_pr_url = 'https://e.invalid/1', "
                "accepted_at = now(), acceptor_principal = 'x'",
                "c" * 40,
            )
        assert self._state(store, intent["intent_id"]) == "proposed"

    def test_the_acceptance_record_cannot_change_when_the_intent_is_applied(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        with pytest.raises(psycopg.errors.CheckViolation):
            self._update(
                store, intent["intent_id"],
                "state = 'applied', applied_at = now(), applier_principal = 'x', "
                "applied_commit_sha = %s, applied_pr_url = 'https://e.invalid/1', "
                "acceptor_principal = 'someone-else'",
                "c" * 40,
            )
        assert _get(store, intent["intent_id"])["acceptance"]["acceptor_principal"] == "github:900:0"

    @pytest.mark.parametrize("target", ["proposed", "accepted", "applied"])
    def test_a_rejected_intent_is_terminal(self, store, target):
        _item, _run_id, intent = _fresh(store)
        _reject(store, intent)
        with pytest.raises(psycopg.errors.Error):
            self._update(store, intent["intent_id"], "state = %s", target)
        assert self._state(store, intent["intent_id"]) == "rejected"

    @pytest.mark.parametrize("target", ["proposed", "accepted", "rejected"])
    def test_an_applied_intent_is_terminal(self, store, target):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        _mark_applied(store, _get(store, intent["intent_id"]))
        with pytest.raises(psycopg.errors.Error):
            self._update(store, intent["intent_id"], "state = %s", target)
        assert self._state(store, intent["intent_id"]) == "applied"

    def test_an_accepted_intent_cannot_go_back_to_proposed(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        with pytest.raises(psycopg.errors.Error):
            self._update(store, intent["intent_id"], "state = 'proposed'")
        assert self._state(store, intent["intent_id"]) == "accepted"


class TestProposeEdges:
    def test_a_replay_answers_with_the_current_intent(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        key = _key("replay-now")
        first = _propose(store, item, run, key)
        accepted = _accept(store, first)
        again = _propose(store, item, run, key)
        assert again["intent_id"] == first["intent_id"]
        assert again["state"] == "accepted" and again["acceptance"] == accepted["acceptance"]

    def test_the_same_key_under_another_principal_is_another_proposal(self, store):
        (item,) = _items(store)
        run_a = _run(store, PROPOSER)
        first = _propose(store, item, run_a, "shared-key-1")
        other = _context_with_propose("github:777:0")
        run_b = _run(store, other)
        second = _propose(store, item, run_b, "shared-key-1", context=other)
        assert second["intent_id"] != first["intent_id"]
        assert second["proposer_principal"] == "github:777:0"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"base_commit": "not-a-commit"},
            {"base_commit": "B" * 40},
            {"title": ""},
            {"unified_diff": ""},
            {"unified_diff": "--- a\n\x00"},
            {"rationale": "x" * 9000},
            {"repository": ""},
        ],
    )
    def test_invalid_content_is_refused(self, store, overrides):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        args = _propose_args(item, run, _key("invalid"), **overrides)
        refused = _refused(lambda: _app(store).invoke(PROPOSE, args, PROPOSER))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)

    @pytest.mark.parametrize("field", ["proposer_principal", "canonical_intent_digest", "state", "revision"])
    def test_the_proposer_digest_state_and_revision_cannot_be_named(self, store, field):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        args = {**_propose_args(item, run, _key("named")), field: "x"}
        refused = _refused(lambda: _app(store).invoke(PROPOSE, args, PROPOSER))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)


class TestTransitionEdges:
    def test_the_revision_is_checked_before_the_digest_and_the_state(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        refused = _refused(
            lambda: _accept(
                store, intent, SECOND, revision=intent["revision"] + 1,
                canonical_intent_digest="0" * 64,
            )
        )
        assert refused.code == "effect-revision-mismatch"

    def test_the_acceptance_names_the_acceptors_own_principal(self, store):
        _item, _run_id, intent = _fresh(store)
        assert _accept(store, intent, SECOND)["acceptance"]["acceptor_principal"] == "github:901:0"

    def test_a_content_row_that_no_longer_matches_its_digest_is_not_accepted(self, store):
        """A row changed behind the trigger's back (here: with it disabled)
        is refused rather than accepted on the strength of a stale digest."""
        _item, _run_id, intent = _fresh(store)
        with store.conn.cursor() as cur:
            cur.execute("ALTER TABLE work_effect_intent DISABLE TRIGGER sprintctl_work_effect_intent_guard")
            cur.execute(
                "UPDATE work_effect_intent SET title = 'tampered' "
                "WHERE repo_id = %s AND intent_id = %s",
                (store.repo_id, intent["intent_id"]),
            )
            cur.execute("ALTER TABLE work_effect_intent ENABLE TRIGGER sprintctl_work_effect_intent_guard")
        store.conn.commit()
        refused = _refused(lambda: _accept(store, intent))
        assert (refused.code, refused.http_status) == ("effect-digest-mismatch", 409)
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    @pytest.mark.parametrize("field,value", [
        ("intent_id", "bad id"), ("revision", 0), ("revision", "1"), ("revision", True),
        ("canonical_intent_digest", "A" * 64), ("canonical_intent_digest", "abc"),
    ])
    def test_a_malformed_binding_is_invalid_arguments(self, store, field, value):
        _item, _run_id, intent = _fresh(store)
        args = _binding(intent, **{field: value})
        refused = _refused(lambda: _app(store).invoke(ACCEPT, args, ACCEPTOR))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_reject_records_who_and_why(self, store):
        _item, _run_id, intent = _fresh(store)
        rejected = _reject(store, intent, reason="wrong repository")
        assert rejected["rejection"]["reason"] == "wrong repository"
        assert rejected["rejection"]["rejector_principal"] == "github:900:0"

    def test_mark_applied_records_who_and_where(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        applied = _mark_applied(store, _get(store, intent["intent_id"]))
        assert applied["application"]["applier_principal"] == "github:900:0"
        assert applied["application"]["commit_sha"] == "c" * 40

    def test_mark_applied_needs_an_http_pull_request_url(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        args = {**_binding(_get(store, intent["intent_id"])), "commit_sha": "c" * 40, "pr_url": "file:///x"}
        refused = _refused(lambda: _app(store).invoke("work.effect.mark-applied-v1", args, ACCEPTOR))
        assert refused.code == "invalid-arguments"


class TestListEdges:
    def test_list_filters_by_item_and_honours_the_limit(self, store):
        first, second = _items(store, 2)
        run = _run(store, PROPOSER)
        a = _propose(store, first, run, title="a")
        b = _propose(store, first, run, title="b")
        c = _propose(store, second, run, title="c")
        only_first = _invoke(store, LIST_PROPOSED, {"item_id": first}, ACCEPTOR)["intents"]
        assert [i["intent_id"] for i in only_first] == [a["intent_id"], b["intent_id"]]
        limited = _invoke(store, LIST_PROPOSED, {"limit": 1, "item_id": second}, ACCEPTOR)["intents"]
        assert [i["intent_id"] for i in limited] == [c["intent_id"]]
        refused = _refused(lambda: _app(store).invoke(LIST_PROPOSED, {"limit": 10**6}, ACCEPTOR))
        assert refused.code == "invalid-arguments"

    def test_an_intent_is_scoped_to_its_repository(self, store):
        _item, _run_id, intent = _fresh(store)
        other = pg.PgStore(
            conn=store.conn, repo_id=store.repo_id + "-other",
            authority_repo_uuid=store.authority_repo_uuid,
        )
        assert pg.get_effect_intent(other, intent["intent_id"]) is None
        assert pg.list_proposed_effect_intents(other) == []


def _context_with_propose(principal: str):
    return _context(principal, {"work:read", "work:claim", "work:evidence", "work.effect.propose"} | _READ_EFFECTS)
