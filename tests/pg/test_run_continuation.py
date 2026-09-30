"""PostgreSQL integration tests: run continuation (agentops#2525, M1-5).

``work.run.register-v1`` records an optional ``predecessor_run_id`` and
``work.run.predecessor-context-v1`` reads that predecessor's session notes
and evidence back through the successor's own run.  Rules: vuoro
``docs/plans/2026-09-26-e2-e3-shared-contract.md`` section 4 (amendment
2026-09-27).  Exercised through ``WorkApplication.invoke()``, the path
vuoro_service dispatches through.
"""
from __future__ import annotations

import uuid

import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    assert_disposable_connection,
    dict_row,
    psycopg,
)
from tests.pg.test_run_evidence import (
    _app,
    _context as _base_context,
    _register,
    _validity,
)

pytestmark = PG_MARKS

CONTEXT_OPERATION = "work.run.predecessor-context-v1"

PREDECESSOR = dict(principal_id="github:1:0", client_id="claude-connector", grant_id="grant-1")
#: Another harness: another principal, OAuth client and grant, in the
#: predecessor's workspace and repository.
SUCCESSOR = dict(principal_id="github:2:0", client_id="other-harness", grant_id="grant-2")


def _context(**overrides):
    context = _base_context(**{"workspace_id": "ws-1", **overrides})
    context.identity.authorities = frozenset({"work:read", "work:evidence"})
    return context


def _key() -> str:
    return "key-" + uuid.uuid4().hex[:16]


def _run(app, *, context, predecessor_run_id=None) -> dict:
    extra = {} if predecessor_run_id is None else {"predecessor_run_id": predecessor_run_id}
    return _register(app, idempotency_key=_key(), context=context, **extra)["run"]


def _note(app, run_id: str, note: str, *, context) -> None:
    app.invoke(
        "work.session-note.write-v1",
        {"run_id": run_id, "note": note, "idempotency_key": _key()},
        context,
    )


def _append(app, run_id: str, *, context) -> dict:
    tail = app.invoke("work.evidence.tail-v1", {"run_id": run_id}, context)["item"]
    seq = 0 if tail is None else tail["chain_seq"] + 1
    item = {
        "run_id": run_id,
        "item_id": f"evi-{seq}-" + uuid.uuid4().hex[:8],
        "kind": "test-result",
        "ref": f"pytest://{seq}",
        "digest": "sha256:" + f"{seq:x}".rjust(64, "0"),
        "collector": "pytest",
        "validity": _validity(),
        "chain_seq": seq,
        "chain_prev_digest": None if tail is None else pg.evidence_entry_digest(tail),
        "idempotency_key": _key(),
    }
    return app.invoke("work.evidence.append-v1", item, context)["item"]


def _read(app, run_id: str, *, context) -> dict:
    return app.invoke(CONTEXT_OPERATION, {"run_id": run_id}, context)


def _refused(call) -> ApplicationRejection:
    with pytest.raises(ApplicationRejection) as excinfo:
        call()
    return excinfo.value


class TestContract:
    def test_the_read_operation_needs_work_read_and_is_a_read(self):
        contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == CONTEXT_OPERATION)
        assert contract.required_authority == "work:read"
        assert contract.execution_semantics == "read"
        assert list(contract.input_schema["required"]) == ["run_id"]

    def test_register_accepts_an_optional_predecessor(self):
        contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == "work.run.register-v1")
        assert "predecessor_run_id" in contract.input_schema["properties"]
        assert "predecessor_run_id" not in contract.input_schema["required"]


class TestRegisterWithPredecessor:
    def test_a_successor_on_another_identity_records_its_predecessor(self, store):
        app = _app(store)
        predecessor = _run(app, context=_context(**PREDECESSOR))
        successor = _run(
            app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor["run_id"]
        )
        assert successor["predecessor_run_id"] == predecessor["run_id"]
        assert successor["principal_id"] == SUCCESSOR["principal_id"]
        assert successor["grant_id"] == SUCCESSOR["grant_id"]
        assert predecessor["predecessor_run_id"] is None

    def test_a_run_without_a_predecessor_echoes_null(self, store):
        app = _app(store)
        assert _run(app, context=_context(**PREDECESSOR))["predecessor_run_id"] is None

    @pytest.mark.parametrize(
        "candidate",
        ["unknown", "malformed", "other-workspace"],
    )
    def test_ineligible_predecessors_are_refused_alike_and_write_nothing(self, store, candidate):
        app = _app(store)
        if candidate == "unknown":
            predecessor_run_id = "run_" + "0" * 26
        elif candidate == "malformed":
            predecessor_run_id = "not-a-run"
        else:
            predecessor_run_id = _run(
                app, context=_context(workspace_id="ws-other", **PREDECESSOR)
            )["run_id"]
        key = _key()
        error = _refused(
            lambda: _register(
                app, idempotency_key=key, context=_context(**SUCCESSOR),
                predecessor_run_id=predecessor_run_id,
            )
        )
        assert error.code == "predecessor-not-eligible"
        assert error.message == "that run cannot be continued by the caller"
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM run WHERE repo_id = %s AND idempotency_key = %s",
                (store.repo_id, key),
            )
            assert cur.fetchone()["n"] == 0
        store.conn.rollback()

    def test_another_repository_s_run_is_not_eligible(self, store, pg_test_scope):
        other = pg.PgStore(conn=store.conn, repo_id=pg_test_scope("continuation-other"))
        foreign_run = _run(_app(other), context=_context(**PREDECESSOR))["run_id"]
        error = _refused(
            lambda: _run(_app(store), context=_context(**SUCCESSOR), predecessor_run_id=foreign_run)
        )
        assert error.code == "predecessor-not-eligible"

    def test_the_same_key_with_another_predecessor_is_a_conflict(self, store):
        app = _app(store)
        first = _run(app, context=_context(**PREDECESSOR))["run_id"]
        second = _run(app, context=_context(**PREDECESSOR))["run_id"]
        key = _key()
        registered = _register(
            app, idempotency_key=key, context=_context(**SUCCESSOR), predecessor_run_id=first
        )["run"]
        replay = _register(
            app, idempotency_key=key, context=_context(**SUCCESSOR), predecessor_run_id=first
        )["run"]
        assert replay["run_id"] == registered["run_id"]
        assert replay["predecessor_run_id"] == first
        for other in (second, None):
            extra = {} if other is None else {"predecessor_run_id": other}
            error = _refused(
                lambda: _register(
                    app, idempotency_key=key, context=_context(**SUCCESSOR), **extra
                )
            )
            assert error.code == "idempotency-conflict"

    def test_callers_omit_the_predecessor_the_catalog_refuses_null(self, store):
        """Omitting predecessor_run_id is "no predecessor"; the served
        catalog admits only a string, so an explicit null never reaches the
        handler (and the replay digest of a request without one is unchanged)."""
        jsonschema = pytest.importorskip("jsonschema")
        contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == "work.run.register-v1")
        base = {
            "harness_id": "h", "harness_build": "1", "model_id": "m", "recipe_id": "r",
            "observed_profile": {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []},
            "idempotency_key": "key-00000001",
        }
        jsonschema.validate(base, contract.input_schema)
        jsonschema.validate({**base, "predecessor_run_id": "run_" + "0" * 26}, contract.input_schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**base, "predecessor_run_id": None}, contract.input_schema)
        app = _app(store)
        key = _key()
        first = _register(app, idempotency_key=key, context=_context(**PREDECESSOR))["run"]
        again = _register(app, idempotency_key=key, context=_context(**PREDECESSOR))["run"]
        assert again["run_id"] == first["run_id"]

    def test_naming_a_predecessor_needs_work_read(self, store):
        app = _app(store)
        predecessor = _run(app, context=_context(**PREDECESSOR))["run_id"]
        evidence_only = _context(**SUCCESSOR)
        evidence_only.identity.authorities = frozenset({"work:evidence"})
        key = _key()
        error = _refused(
            lambda: _register(
                app, idempotency_key=key, context=evidence_only, predecessor_run_id=predecessor
            )
        )
        assert error.code == "authority-required"
        # Without a predecessor, work:evidence alone still registers a run.
        assert _run(app, context=evidence_only)["predecessor_run_id"] is None

    def test_a_continued_run_cannot_be_deleted_from_under_its_successor(self, store):
        app = _app(store)
        predecessor = _run(app, context=_context(**PREDECESSOR))["run_id"]
        successor = _run(
            app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor
        )["run_id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with store.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM run WHERE repo_id = %s AND run_id = %s",
                    (store.repo_id, predecessor),
                )
        store.conn.rollback()
        # Deleting the successor drops its link and nothing else.
        with store.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM run WHERE repo_id = %s AND run_id = %s", (store.repo_id, successor)
            )
            cur.execute(
                "SELECT count(*) AS n FROM run_predecessor WHERE repo_id = %s AND predecessor_run_id = %s",
                (store.repo_id, predecessor),
            )
            assert cur.fetchone()["n"] == 0
        store.conn.commit()


class TestPredecessorContext:
    def test_the_successor_reads_its_predecessor_s_notes_and_evidence(self, store):
        app = _app(store)
        pred_ctx = _context(**PREDECESSOR)
        predecessor = _run(app, context=pred_ctx)["run_id"]
        _note(app, predecessor, "picked item 7, tests red", context=pred_ctx)
        _note(app, predecessor, "stopped at the migration", context=pred_ctx)
        first = _append(app, predecessor, context=pred_ctx)
        second = _append(app, predecessor, context=pred_ctx)
        succ_ctx = _context(**SUCCESSOR)
        successor = _run(app, context=succ_ctx, predecessor_run_id=predecessor)["run_id"]
        _note(app, successor, "the successor's own note", context=succ_ctx)

        context = _read(app, successor, context=succ_ctx)
        assert context["run_id"] == successor
        assert context["predecessor_run_id"] == predecessor
        assert [n["note"] for n in context["session_notes"]] == [
            "picked item 7, tests red", "stopped at the migration",
        ]
        assert all(isinstance(n["note_id"], int) and n["created_at"] for n in context["session_notes"])
        assert context["evidence"] == [first, second]

    def test_a_run_without_a_predecessor_reads_an_empty_context(self, store):
        app = _app(store)
        ctx = _context(**PREDECESSOR)
        run_id = _run(app, context=ctx)["run_id"]
        assert _read(app, run_id, context=ctx) == {
            "repo_id": store.repo_id, "run_id": run_id, "predecessor_run_id": None,
            "session_notes": [], "evidence": [],
            "next_after_note_id": None, "next_after_chain_seq": None,
        }

    def test_the_context_is_read_in_bounded_pages(self, store):
        app = _app(store)
        pred_ctx = _context(**PREDECESSOR)
        predecessor = _run(app, context=pred_ctx)["run_id"]
        for n in range(3):
            _note(app, predecessor, f"note {n}", context=pred_ctx)
        items = [_append(app, predecessor, context=pred_ctx) for _ in range(3)]
        succ_ctx = _context(**SUCCESSOR)
        successor = _run(app, context=succ_ctx, predecessor_run_id=predecessor)["run_id"]

        def page(**cursor):
            return app.invoke(CONTEXT_OPERATION, {"run_id": successor, "limit": 2, **cursor}, succ_ctx)

        first = page()
        assert [n["note"] for n in first["session_notes"]] == ["note 0", "note 1"]
        assert first["evidence"] == items[:2]
        assert first["next_after_note_id"] == first["session_notes"][-1]["note_id"]
        assert first["next_after_chain_seq"] == 1
        second = page(
            after_note_id=first["next_after_note_id"],
            after_chain_seq=first["next_after_chain_seq"],
        )
        assert [n["note"] for n in second["session_notes"]] == ["note 2"]
        assert second["evidence"] == items[2:]
        assert second["next_after_note_id"] is None and second["next_after_chain_seq"] is None
        # A page exactly as long as what is left is not cut.
        exact = app.invoke(CONTEXT_OPERATION, {"run_id": successor, "limit": 3}, succ_ctx)
        assert exact["next_after_note_id"] is None and exact["next_after_chain_seq"] is None
        # The default page holds everything here.
        whole = _read(app, successor, context=succ_ctx)
        assert len(whole["session_notes"]) == 3 and len(whole["evidence"]) == 3

    @pytest.mark.parametrize("arguments", [
        {"limit": 0}, {"limit": 501}, {"after_note_id": -1}, {"after_chain_seq": -1},
    ])
    def test_out_of_range_paging_is_refused(self, store, arguments):
        from sprintctl.application_common import PREDECESSOR_CONTEXT_MAX_LIMIT

        assert PREDECESSOR_CONTEXT_MAX_LIMIT == 500
        app = _app(store)
        ctx = _context(**PREDECESSOR)
        run_id = _run(app, context=ctx)["run_id"]
        error = _refused(
            lambda: app.invoke(CONTEXT_OPERATION, {"run_id": run_id, **arguments}, ctx)
        )
        assert error.code == "invalid-arguments"

    def test_only_the_caller_s_own_run_can_be_read(self, store):
        app = _app(store)
        pred_ctx = _context(**PREDECESSOR)
        predecessor = _run(app, context=pred_ctx)["run_id"]
        succ_ctx = _context(**SUCCESSOR)
        successor = _run(app, context=succ_ctx, predecessor_run_id=predecessor)["run_id"]
        third = _context(principal_id="github:3:0", client_id="c3", grant_id="g3")
        same_principal_other_grant = _context(**{**SUCCESSOR, "grant_id": "grant-9"})
        for run_id, ctx in (
            (successor, third),  # a third party presenting the successor's run
            (successor, pred_ctx),  # the predecessor presenting its successor's run
            (successor, same_principal_other_grant),  # another grant of the successor
            ("run_" + "1" * 26, succ_ctx),  # an unknown run
        ):
            error = _refused(lambda: _read(app, run_id, context=ctx))
            assert error.code == "run-not-found"

    def test_continuation_transfers_context_not_authority(self, store):
        app = _app(store)
        pred_ctx = _context(**PREDECESSOR)
        predecessor = _run(app, context=pred_ctx)["run_id"]
        succ_ctx = _context(**SUCCESSOR)
        _run(app, context=succ_ctx, predecessor_run_id=predecessor)
        # The predecessor's run never resolves to the successor, so the
        # successor cannot write to it or read its tail directly.
        for operation, arguments in (
            ("work.run.resolve-v1", {"run_id": predecessor}),
            ("work.evidence.tail-v1", {"run_id": predecessor}),
            (
                "work.session-note.write-v1",
                {"run_id": predecessor, "note": "x", "idempotency_key": _key()},
            ),
        ):
            error = _refused(lambda: app.invoke(operation, arguments, succ_ctx))
            assert error.code == "run-not-found"
        # Reading through the predecessor's handle is not reading its context.
        error = _refused(lambda: _read(app, predecessor, context=succ_ctx))
        assert error.code == "run-not-found"

    def test_one_hop_only(self, store):
        app = _app(store)
        a_ctx = _context(**PREDECESSOR)
        a = _run(app, context=a_ctx)["run_id"]
        _note(app, a, "from a", context=a_ctx)
        b_ctx = _context(**SUCCESSOR)
        b = _run(app, context=b_ctx, predecessor_run_id=a)["run_id"]
        _note(app, b, "from b", context=b_ctx)
        c_ctx = _context(principal_id="github:3:0", client_id="c3", grant_id="g3")
        c = _run(app, context=c_ctx, predecessor_run_id=b)["run_id"]
        context = _read(app, c, context=c_ctx)
        assert context["predecessor_run_id"] == b
        assert [n["note"] for n in context["session_notes"]] == ["from b"]


class TestSchema20Migration:
    def test_migrating_from_schema_2_installs_exactly_the_recorded_shape(self, store):
        schema = "migration_20_" + uuid.uuid4().hex
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
            applied = pg_migrations.migrate_schema(pg.PgStore(conn, "migration-20"))
            assert applied["applied_versions"][-1] == 20 and applied["to_version"] == 20
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                assert pg._foreign_relations(
                    cur, pg._SCHEMA_20_TABLES, pg._SCHEMA_20_INDEXES, pg._SCHEMA_20_INDEX_SHAPES
                ) == []
                # Schema 17's exact-shape guard for run still holds.
                assert pg._schema_17_foreign_relations(cur) == []
                # Additive and idempotent: re-applying over its own shape is a no-op.
                pg._apply_schema_version_20(cur)
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    def test_a_foreign_run_predecessor_table_is_refused(self, store):
        schema = "migration_20_foreign_" + uuid.uuid4().hex
        columns, _ = pg._SCHEMA_20_TABLES["run_predecessor"]
        ddl = ", ".join(f"{n} {t} {'NOT NULL' if nn else ''}" for n, t, nn in columns)
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
            cur.execute(f"CREATE TABLE run_predecessor ({ddl})")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="run_predecessor"):
                pg_migrations.migrate_schema(pg.PgStore(conn, "migration-20-foreign"))
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()
