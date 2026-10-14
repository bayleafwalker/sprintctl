"""PostgreSQL integration tests: run resolution facts and structured
approvals (schema 26).

``work.run.resolve-v2`` adds ``issued_at``, ``superseded_by_run_id`` and
``state`` to resolve-v1's binding; ``work.approval.record-v1`` /
``work.approval.get-v1`` store and read one immutable operator approval.
Exercised through ``WorkApplication.invoke()``, the path vuoro_service
dispatches through.  The 25 -> 26 upgrade runs in an isolated schema.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import approval_schema, pg, pg_migrations
from sprintctl.application import ApplicationRejection

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    assert_disposable_connection,
    dict_row,
    drop_schema26,
    psycopg,
)
from tests.pg.test_run_evidence import _app, _context as _base_context, _register

pytestmark = PG_MARKS

AGENT = dict(principal_id="github:1:0", client_id="claude-connector", grant_id="grant-1")
SUCCESSOR = dict(principal_id="github:2:0", client_id="other-harness", grant_id="grant-2")
OPERATOR = dict(principal_id="github:7:0", client_id="operator-console", grant_id="grant-op")


def _context(*, authorities=("work:read", "work:evidence"), **overrides):
    context = _base_context(**{"workspace_id": "ws-1", **overrides})
    context.identity.authorities = frozenset(authorities)
    return context


def _operator(**overrides):
    return _context(authorities=("work:read", "work:approve"), **{**OPERATOR, **overrides})


def _key() -> str:
    return "key-" + uuid.uuid4().hex[:16]


def _run(app, *, context=None, predecessor_run_id=None) -> dict:
    extra = {} if predecessor_run_id is None else {"predecessor_run_id": predecessor_run_id}
    return _register(app, idempotency_key=_key(), context=context or _context(**AGENT), **extra)["run"]


def _rejection(call) -> ApplicationRejection:
    with pytest.raises(ApplicationRejection) as excinfo:
        call()
    return excinfo.value


def _in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _approve(app, run_id, *, context=None, key=None, **overrides) -> dict:
    arguments = {
        "transition": "rollout-resume", "run_id": run_id, "expires_at": _in(3600),
        "rationale": "resume after review", "idempotency_key": key or _key(), **overrides,
    }
    return app.invoke("work.approval.record-v1", arguments, context or _operator())


class TestResolveV2:
    def test_an_active_run_carries_v1_binding_issue_time_and_no_successor(self, store):
        app = _app(store)
        run = _run(app)
        v1 = app.invoke("work.run.resolve-v1", {"run_id": run["run_id"]}, _context(**AGENT))
        v2 = app.invoke("work.run.resolve-v2", {"run_id": run["run_id"]}, _context(**AGENT))
        assert {key: v2[key] for key in v1} == v1
        assert v2["issued_at"] == run["created_at"]
        assert v2["superseded_by_run_id"] is None and v2["state"] == "active"
        assert set(v2) == set(v1) | {"issued_at", "superseded_by_run_id", "state"}

    def test_issued_at_is_the_owner_clock_not_a_caller_value(self, store):
        app = _app(store)
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        run = _run(app)
        v2 = app.invoke("work.run.resolve-v2", {"run_id": run["run_id"]}, _context(**AGENT))
        issued = datetime.fromisoformat(v2["issued_at"].replace("Z", "+00:00"))
        assert before <= issued <= datetime.now(timezone.utc) + timedelta(seconds=5)
        with store.conn.cursor() as cur:
            cur.execute("SELECT created_at FROM run WHERE repo_id=%s AND run_id=%s",
                        (store.repo_id, run["run_id"]))
            assert cur.fetchone()["created_at"] == issued
        store.conn.rollback()

    def test_a_registered_successor_supersedes_the_run_and_v1_is_unchanged(self, store):
        app = _app(store)
        predecessor = _run(app)
        v1_before = app.invoke("work.run.resolve-v1", {"run_id": predecessor["run_id"]}, _context(**AGENT))
        successor = _run(app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor["run_id"])
        v2 = app.invoke("work.run.resolve-v2", {"run_id": predecessor["run_id"]}, _context(**AGENT))
        assert v2["state"] == "superseded"
        assert v2["superseded_by_run_id"] == successor["run_id"]
        assert app.invoke("work.run.resolve-v1", {"run_id": predecessor["run_id"]}, _context(**AGENT)) == v1_before
        # The successor itself is active; supersession is one hop, not lineage.
        own = app.invoke("work.run.resolve-v2", {"run_id": successor["run_id"]}, _context(**SUCCESSOR))
        assert own["state"] == "active" and own["superseded_by_run_id"] is None

    def test_the_first_registered_successor_is_reported_and_stays_reported(self, store):
        app = _app(store)
        predecessor = _run(app)
        first = _run(app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor["run_id"])
        _run(app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor["run_id"])
        v2 = app.invoke("work.run.resolve-v2", {"run_id": predecessor["run_id"]}, _context(**AGENT))
        assert v2["superseded_by_run_id"] == first["run_id"]

    @pytest.mark.parametrize("other", [
        dict(principal_id="github:9:0"), dict(workspace_id="ws-other"),
        dict(grant_id="grant-other"), dict(client_id=None, grant_id=None),
    ], ids=["principal", "workspace", "grant", "no-grant"])
    def test_any_other_binding_is_run_not_found_as_in_v1(self, store, other):
        app = _app(store)
        run = _run(app)
        error = _rejection(lambda: app.invoke(
            "work.run.resolve-v2", {"run_id": run["run_id"]}, _context(**{**AGENT, **other})))
        assert (error.code, error.http_status) == ("run-not-found", 404)
        unknown = _rejection(lambda: app.invoke(
            "work.run.resolve-v2", {"run_id": "run_" + "0" * 26}, _context(**AGENT)))
        assert (unknown.code, str(unknown)) == (error.code, str(error).replace(run["run_id"], "run_" + "0" * 26))


class TestApprovalRecordAndGet:
    def test_record_names_transition_repo_run_expiry_and_the_authenticated_approver(self, store):
        app = _app(store)
        run = _run(app)
        expires_at = _in(1800)
        recorded = _approve(app, run["run_id"], expires_at=expires_at)
        assert recorded["approval_id"].startswith("apr_") and len(recorded["approval_id"]) == 30
        assert recorded["repo_id"] == store.repo_id and recorded["workspace_id"] == "ws-1"
        assert (recorded["transition"], recorded["run_id"]) == ("rollout-resume", run["run_id"])
        assert recorded["approver"] == {
            "principal_id": "github:7:0", "client_id": "operator-console", "grant_id": "grant-op",
        }
        assert datetime.fromisoformat(recorded["expires_at"].replace("Z", "+00:00")) == \
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        assert recorded["replayed"] is False
        got = app.invoke("work.approval.get-v1", {"approval_id": recorded["approval_id"]},
                         _context(**AGENT))
        assert {key: got[key] for key in recorded if key != "replayed"} == {
            key: value for key, value in recorded.items() if key != "replayed"}
        assert got["expired"] is False and got["evaluated_at"] >= got["recorded_at"]

    def test_record_requires_work_approve_which_agent_grants_do_not_carry(self, store):
        app = _app(store)
        run = _run(app)
        for authorities in (("work:read", "work:evidence"), ("work:write", "work:claim", "work:read"),
                            ("work.effect.accept",), ()):
            error = _rejection(lambda: _approve(app, run["run_id"], context=_context(
                authorities=authorities, **OPERATOR)))
            assert (error.code, error.http_status) == ("authority-required", 403)
        with store.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM work_approval WHERE repo_id=%s AND run_id=%s",
                        (store.repo_id, run["run_id"]))
            assert cur.fetchone()["n"] == 0
        store.conn.rollback()

    def test_get_requires_work_read(self, store):
        app = _app(store)
        recorded = _approve(app, _run(app)["run_id"])
        error = _rejection(lambda: app.invoke(
            "work.approval.get-v1", {"approval_id": recorded["approval_id"]},
            _context(authorities=("work:evidence",), **AGENT)))
        assert (error.code, error.http_status) == ("authority-required", 403)

    def test_same_key_replays_and_different_arguments_conflict(self, store):
        app = _app(store)
        run = _run(app)
        key, expires_at = _key(), _in(900)
        first = _approve(app, run["run_id"], key=key, expires_at=expires_at)
        again = _approve(app, run["run_id"], key=key, expires_at=expires_at)
        assert again == {**first, "replayed": True}
        for changed in (dict(transition="protected-publication"), dict(expires_at=_in(1200)),
                        dict(rationale="other")):
            error = _rejection(lambda: _approve(
                app, run["run_id"], key=key, **{"expires_at": expires_at, **changed}))
            assert (error.code, error.http_status) == ("idempotency-conflict", 409)
        other_grant = _rejection(lambda: _approve(
            app, run["run_id"], key=key, expires_at=expires_at, context=_operator(grant_id="grant-op-2")))
        assert other_grant.code == "idempotency-conflict"

    def test_the_run_must_be_this_workspaces_and_not_superseded(self, store):
        app = _app(store)
        foreign = _run(app, context=_context(**{**AGENT, "workspace_id": "ws-2"}))
        for run_id in (foreign["run_id"], "run_" + "0" * 26):
            error = _rejection(lambda: _approve(app, run_id))
            assert (error.code, error.http_status) == ("run-not-found", 404)
        predecessor = _run(app)
        successor = _run(app, context=_context(**SUCCESSOR), predecessor_run_id=predecessor["run_id"])
        error = _rejection(lambda: _approve(app, predecessor["run_id"]))
        assert (error.code, error.http_status) == ("run-superseded", 409)
        assert successor["run_id"] in str(error)

    @pytest.mark.parametrize("expires_at", [
        lambda: _in(-60), lambda: _in(0), lambda: _in(approval_schema.MAX_LIFETIME_SECONDS + 600),
    ], ids=["past", "now", "beyond-max-lifetime"])
    def test_expiry_must_be_future_and_bounded(self, store, expires_at):
        app = _app(store)
        error = _rejection(lambda: _approve(app, _run(app)["run_id"], expires_at=expires_at()))
        assert (error.code, error.http_status) == ("invalid-expiry", 422)

    @pytest.mark.parametrize("arguments", [
        dict(expires_at="tomorrow"), dict(expires_at="2026-10-14T10:00:00"),
        dict(transition="Rollout Resume"), dict(unexpected="x"), dict(rationale="x" * 2049),
    ], ids=["not-a-timestamp", "no-offset", "transition-pattern", "unknown-argument", "rationale-too-long"])
    def test_malformed_arguments_are_refused_before_any_write(self, store, arguments):
        app = _app(store)
        error = _rejection(lambda: _approve(app, _run(app)["run_id"], **arguments))
        assert (error.code, error.http_status) == ("invalid-arguments", 422)

    def test_get_is_scoped_to_repository_and_workspace(self, store, pg_test_scope):
        app = _app(store)
        recorded = _approve(app, _run(app)["run_id"])
        for approval_id, context in (
            (recorded["approval_id"], _context(**{**AGENT, "workspace_id": "ws-2"})),
            ("apr_" + "0" * 26, _context(**AGENT)),
            ("not-an-approval", _context(**AGENT)),
        ):
            error = _rejection(lambda: app.invoke(
                "work.approval.get-v1", {"approval_id": approval_id}, context))
            assert (error.code, error.http_status) == ("approval-not-found", 404)
        other_repo = pg.PgStore(conn=store.conn, repo_id=pg_test_scope("approval-other"))
        other_repo.remote_schema_version = store.remote_schema_version
        error = _rejection(lambda: _app(other_repo).invoke(
            "work.approval.get-v1", {"approval_id": recorded["approval_id"]}, _context(**AGENT)))
        assert error.code == "approval-not-found"

    def test_get_reports_expiry_by_the_owner_clock(self, store):
        app = _app(store)
        run = _run(app)
        approval_id = "apr_" + "1" * 26
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_approval(repo_id, approval_id, workspace_id, run_id, transition, "
                "approver_principal_id, idempotency_key, request_digest, recorded_at, expires_at) "
                "VALUES (%s, %s, 'ws-1', %s, 'rollout-resume', 'github:7:0', %s, %s, "
                "now() - interval '2 hours', now() - interval '1 hour')",
                (store.repo_id, approval_id, run["run_id"], _key(), "a" * 64),
            )
        store.conn.commit()
        got = app.invoke("work.approval.get-v1", {"approval_id": approval_id}, _context(**AGENT))
        assert got["expired"] is True and got["evaluated_at"] > got["expires_at"]

    def test_approvals_are_append_only_and_hold_their_run(self, store):
        app = _app(store)
        run = _run(app)
        recorded = _approve(app, run["run_id"])
        for statement in (
            "UPDATE work_approval SET expires_at = expires_at + interval '1 day' WHERE repo_id=%s",
            "DELETE FROM work_approval WHERE repo_id=%s",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                with store.conn.cursor() as cur:
                    cur.execute(statement, (store.repo_id,))
            store.conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with store.conn.cursor() as cur:
                cur.execute("DELETE FROM run WHERE repo_id=%s AND run_id=%s", (store.repo_id, run["run_id"]))
        store.conn.rollback()
        got = app.invoke("work.approval.get-v1", {"approval_id": recorded["approval_id"]}, _context(**AGENT))
        assert got["expires_at"] == recorded["expires_at"]

    def test_storage_refuses_a_lifetime_beyond_the_bound_on_its_own(self, store):
        app = _app(store)
        run = _run(app)
        with pytest.raises(psycopg.errors.CheckViolation):
            with store.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_approval(repo_id, approval_id, workspace_id, run_id, transition, "
                    "approver_principal_id, idempotency_key, request_digest, recorded_at, expires_at) "
                    "VALUES (%s, %s, 'ws-1', %s, 'rollout-resume', 'github:7:0', %s, %s, "
                    "now(), now() + interval '8 days')",
                    (store.repo_id, "apr_" + "2" * 26, run["run_id"], _key(), "a" * 64),
                )
        store.conn.rollback()

    def test_an_owner_below_schema_26_refuses_without_touching_storage(self, store):
        app = _app(store)
        run = _run(app)
        actual = store.remote_schema_version
        store.remote_schema_version = 25
        try:
            for call in (lambda: _approve(app, run["run_id"]),
                         lambda: app.invoke("work.approval.get-v1", {"approval_id": "apr_" + "0" * 26},
                                            _context(**AGENT))):
                error = _rejection(call)
                assert (error.code, error.http_status) == ("owner-capability-unavailable", 503)
        finally:
            store.remote_schema_version = actual

    def test_handshake_reports_the_exact_approval_catalog(self, store):
        compatibility = pg_migrations.compatibility_handshake(store)
        storage = compatibility["capabilities"]["approval_storage"]
        assert compatibility["compatible"] and compatibility["remote_schema"]["actual"] == 26
        assert storage["available"] and storage["complete"] and storage["table_count"] == 1
        assert storage["catalog_fingerprint"] == approval_schema.EXPECTED_CATALOG_FINGERPRINT


# --------------------------------------------------------------------------
# Schema 25 -> 26 upgrade, in an isolated schema of the disposable database.
# --------------------------------------------------------------------------
@pytest.fixture
def isolated():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema = "s26_" + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    store = pg.PgStore(conn, "itest-schema26")
    pg.init_db(store)
    try:
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.commit()
        conn.close()


def _reconstruct25(store) -> None:
    with store.conn.cursor() as cur:
        drop_schema26(cur)
        cur.execute("UPDATE schema_version SET version=25")
    store.conn.commit()


def _version(store) -> int:
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        version = cur.fetchone()["version"]
    store.conn.rollback()
    return version


def _approval_catalog(store) -> dict:
    with store.conn.cursor() as cur:
        catalog = approval_schema.inspect_catalog(cur)
    store.conn.rollback()
    return catalog


def test_upgrade_25_to_26_is_explicit_idempotent_and_the_runtime_never_migrates(isolated):
    store = isolated
    _reconstruct25(store)
    assert pg_migrations.compatibility_handshake(store)["reason"] == "schema-too-old"
    # The runtime (read-only startup) refuses a 25 authority and runs no DDL.
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match="actual=25"):
        pg_migrations.startup_schema_handshake(store, {})
    assert _version(store) == 25 and _approval_catalog(store)["table_count"] == 0
    result = pg_migrations.migrate_schema(store)
    assert result["applied_versions"] == [26] and result["to_version"] == 26
    assert result["compatibility"]["compatible"] is True
    assert _approval_catalog(store)["available"] is True
    assert pg_migrations.migrate_schema(store)["applied_versions"] == []
    assert pg_migrations.startup_schema_handshake(store, {})["remote_schema"]["actual"] == 26


def test_a_pre_existing_lookalike_is_refused_without_advancing(isolated):
    store = isolated
    _reconstruct25(store)
    with store.conn.cursor() as cur:
        cur.execute("CREATE TABLE work_approval (approval_id text)")
    store.conn.commit()
    assert pg_migrations.compatibility_handshake(store)["reason"] == "schema-too-old"
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="schema26 refuses"):
        pg_migrations.migrate_schema(store)
    assert _version(store) == 25
    assert _approval_catalog(store)["complete"] is False


def test_a_partial_install_on_26_is_reported_not_repaired(isolated):
    store = isolated
    with store.conn.cursor() as cur:
        cur.execute("DROP TRIGGER sprintctl_approval_no_truncate ON work_approval")
    store.conn.commit()
    handshake = pg_migrations.compatibility_handshake(store)
    assert handshake["compatible"] is False and handshake["reason"] == "approval-storage-partial"
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError):
        pg_migrations.require_compatible_schema(store)


def test_an_injected_ddl_failure_rolls_back_storage_and_version(isolated, monkeypatch):
    store = isolated
    _reconstruct25(store)
    original = approval_schema.DDL
    monkeypatch.setattr(approval_schema, "DDL", original + "; SELECT 1/0")
    with pytest.raises(psycopg.errors.DivisionByZero):
        pg_migrations.migrate_schema(store)
    assert _version(store) == 25
    assert _approval_catalog(store) | {"catalog_fingerprint": None} == {
        "table_count": 0, "catalog_fingerprint": None,
        "expected_catalog_fingerprint": approval_schema.EXPECTED_CATALOG_FINGERPRINT,
        "available": False, "complete": True,
    }
    monkeypatch.setattr(approval_schema, "DDL", original)
    assert pg_migrations.migrate_schema(store)["applied_versions"] == [26]
