"""Unit coverage for concurrent-safe PostgreSQL schema bootstrap."""

from __future__ import annotations

import pytest

from sprintctl import pg


class _BootstrapCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self._conn.calls.append((query, params))
        if self._conn.fail_on and self._conn.fail_on in query:
            raise RuntimeError("bootstrap failed")
        return self

    def fetchone(self):
        return {"seq": None}


class _BootstrapConnection:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _BootstrapCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_init_db_acquires_transaction_lock_before_mutable_ddl():
    conn = _BootstrapConnection()

    pg.init_db(pg.PgStore(conn=conn, repo_id="test-repo"))

    assert conn.calls[0] == (
        "SELECT pg_advisory_xact_lock(%s, %s)",
        pg._SCHEMA_BOOTSTRAP_LOCK_KEYS,
    )
    assert conn.calls[1] == (pg.PG_DDL, None)
    assert conn.committed
    assert not conn.rolled_back


def test_init_db_rolls_back_if_bootstrap_fails_after_lock_acquisition():
    conn = _BootstrapConnection(fail_on="ALTER TABLE ingest_record")

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        pg.init_db(pg.PgStore(conn=conn, repo_id="test-repo"))

    assert conn.calls[0] == (
        "SELECT pg_advisory_xact_lock(%s, %s)",
        pg._SCHEMA_BOOTSTRAP_LOCK_KEYS,
    )
    assert conn.rolled_back
    assert not conn.committed
