"""Real SQLite failure, retry and restore evidence for the existing runner."""
from contextlib import closing
import shutil
import sqlite3

import pytest

from sprintctl import db


def rows(conn, table):
    return [tuple(row) for row in conn.execute(f'SELECT id, payload FROM {table} ORDER BY id')]


def seed(conn):
    conn.execute('CREATE TABLE migration_probe (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    conn.execute("INSERT INTO migration_probe VALUES (1, 'preserved')")
    conn.commit()


@pytest.mark.parametrize('foreign_keys_off', [False, True])
def test_failed_ddl_and_backfill_roll_back_then_retry_once(conn, db_path, foreign_keys_off):
    seed(conn)
    original_version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
    expected = rows(conn, 'migration_probe')
    target = original_version + 1  # Synthetic version in this disposable file only.
    calls = []
    def change(connection, fail):
        calls.append(fail)
        connection.execute('CREATE TABLE migration_candidate (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        connection.execute('INSERT INTO migration_candidate SELECT id, payload FROM migration_probe')
        connection.execute('DROP TABLE migration_probe')
        if fail:
            raise RuntimeError('injected failure after DDL and backfill')
    with pytest.raises(RuntimeError, match='after DDL'):
        db._run_migration(conn, target, lambda c: change(c, True), foreign_keys_off=foreign_keys_off)
    assert not conn.in_transaction
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with closing(db.get_connection(db_path)) as reopened:
        assert rows(reopened, 'migration_probe') == expected
        assert reopened.execute('SELECT version FROM schema_version').fetchone()[0] == original_version
        assert reopened.execute("SELECT name FROM sqlite_master WHERE name='migration_candidate'").fetchone() is None
    db._run_migration(conn, target, lambda c: change(c, False), foreign_keys_off=foreign_keys_off)
    assert rows(conn, 'migration_candidate') == expected
    assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == target
    db._run_migration(conn, target, lambda c: pytest.fail('already committed migration reran'))
    assert calls == [True, False]
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_backup_restores_pre_migration_rows_schema_and_version(conn, db_path, tmp_path):
    seed(conn)
    original_version = conn.execute('SELECT version FROM schema_version').fetchone()[0]
    expected = rows(conn, 'migration_probe')
    backup = tmp_path / 'before-upgrade.sqlite'
    with closing(sqlite3.connect(backup)) as snapshot:
        conn.backup(snapshot)
        assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    db._run_migration(conn, original_version + 1, lambda c: c.execute('DROP TABLE migration_probe'))
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchone() is None
    restored_path = tmp_path / 'restored-candidate.sqlite'
    shutil.copyfile(backup, restored_path)
    with closing(db.get_connection(restored_path)) as restored:
        assert rows(restored, 'migration_probe') == expected
        assert restored.execute('SELECT version FROM schema_version').fetchone()[0] == original_version
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.execute('PRAGMA foreign_key_check').fetchall()
    # Restoring a candidate does not rewrite or downgrade the migrated source.
    assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == original_version + 1
