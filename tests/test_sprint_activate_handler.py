"""The domain-owned sprint-activation command handler (agentops item #1105).

DDL-inspection tests validate the SQL function contract the way
test_pg_schema.py validates tables; wrapper tests exercise the Python-side
error translation with a fake connection, no live PostgreSQL required.
"""

from __future__ import annotations

import re

import pytest

from sprintctl.db import InvalidTransition
from sprintctl.pg import PG_DDL, PgStore, activate_sprint


def _function_block() -> str:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION sprintctl_sprint_activate\(.*?\n\$\$;",
        PG_DDL,
        re.DOTALL,
    )
    assert match, "sprintctl_sprint_activate function not found in PG_DDL"
    return match.group(0)


def test_ddl_defines_sprint_activate_handler():
    block = _function_block()
    for param in ("p_repo_id text", "p_sprint_id bigint", "p_actor text", "p_context text"):
        assert param in block, f"missing parameter: {param}"


def test_handler_locks_row_and_mirrors_sprint_transitions():
    block = _function_block()
    assert "FOR UPDATE" in block, "handler must lock the sprint row"
    assert "current_row.kind = 'archive'" in block, "handler must reject archive sprints"
    assert "current_row.status <> 'planned'" in block, "handler must allow planned -> active only"
    assert "SET status = 'active', kind = 'active_sprint'" in block


def test_handler_records_ledger_event_with_provenance():
    block = _function_block()
    assert "'sprint-activated'" in block
    assert "INSERT INTO event" in block
    assert "'previous_status', current_row.status" in block
    assert "'context', p_context" in block


def test_handler_uses_stable_error_codes():
    block = _function_block()
    assert "ERRCODE = 'SP404'" in block, "not-found must raise SQLSTATE SP404"
    assert block.count("ERRCODE = 'SP409'") == 2, "both invalid transitions must raise SQLSTATE SP409"


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if self._conn.error is not None:
            raise self._conn.error

    def fetchone(self):
        return self._conn.row


class _FakeConn:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.executed = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _SqlStateError(Exception):
    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


def test_activate_sprint_wrapper_returns_effect_and_commits():
    row = {
        "id": 380,
        "repo_id": "agentops",
        "name": "backlog",
        "status": "active",
        "kind": "active_sprint",
        "previous_status": "planned",
        "previous_kind": "backlog",
        "actor": "operator:cli",
        "event_id": 991,
    }
    conn = _FakeConn(row=row)
    store = PgStore(conn=conn, repo_id="agentops")
    result = activate_sprint(store, 380, "operator:cli")
    assert result["status"] == "active"
    assert result["kind"] == "active_sprint"
    assert result["previous_status"] == "planned"
    assert result["event_id"] == 991
    assert conn.committed
    assert not conn.rolled_back
    sql, params = conn.executed[0]
    assert "sprintctl_sprint_activate" in sql
    assert params == ("agentops", 380, "operator:cli", "sprintctl:sprint-activate")


def test_activate_sprint_wrapper_translates_not_found():
    conn = _FakeConn(error=_SqlStateError("Sprint 999 not found for repo agentops", "SP404"))
    store = PgStore(conn=conn, repo_id="agentops")
    with pytest.raises(ValueError, match="not found"):
        activate_sprint(store, 999, "operator:cli")
    assert conn.rolled_back
    assert not conn.committed


def test_activate_sprint_wrapper_translates_invalid_transition():
    conn = _FakeConn(error=_SqlStateError("cannot transition sprint active -> active. Allowed: planned -> active", "SP409"))
    store = PgStore(conn=conn, repo_id="agentops")
    with pytest.raises(InvalidTransition, match="planned -> active"):
        activate_sprint(store, 380, "operator:cli")
    assert conn.rolled_back


def test_activate_sprint_wrapper_reraises_unknown_errors():
    conn = _FakeConn(error=RuntimeError("connection lost"))
    store = PgStore(conn=conn, repo_id="agentops")
    with pytest.raises(RuntimeError, match="connection lost"):
        activate_sprint(store, 380, "operator:cli")
    assert conn.rolled_back
