"""Disposable PostgreSQL tests for material catalog drift detection."""

import pytest

from sprintctl.material_inventory import (
    build_manifest,
    build_writer_inventory,
    require_manifest_match,
    snapshot,
    verify_postgres_manifest,
)

from tests.pg._shared import PG_MARKS


pytestmark = PG_MARKS


def _baseline(conn):
    cur = conn.cursor()
    cur.execute('SELECT current_user AS role_name')
    role = cur.fetchone()['role_name']
    return snapshot(cur, schema='public', role_names=[role])


def _assert_drift_and_rollback(conn, mutation, manifest, writer_inventory):
    conn.rollback()
    expected = _baseline(conn)
    try:
        mutation(conn.cursor())
        with pytest.raises(ValueError, match='catalog or role inventory mismatch'):
            require_manifest_match(conn.cursor(), manifest,
                                   source_commit='a' * 40,
                                   writer_inventory=writer_inventory)
    finally:
        conn.rollback()
    assert _baseline(conn) == expected


def test_catalog_inventory_matches_baseline_and_detects_column_drift(store, tmp_path):
    conn = store.conn
    role_names = _baseline(conn)['role_names']
    source = tmp_path / 'writer.py'
    source.write_text('sql = "UPDATE work_item SET title=%s"\n')
    writer_inventory = build_writer_inventory(
        [source], {f'literal:{source}:1:work_item': 'work item writer'})
    manifest = build_manifest(
        conn.cursor(), schema='public', role_names=role_names, source_commit='a' * 40,
        source_files=[str(source)], writer_inventory=writer_inventory)
    require_manifest_match(conn.cursor(), manifest,
                           source_commit='a' * 40,
                           writer_inventory=writer_inventory)
    conn.rollback()
    verify_postgres_manifest(conn, manifest, source_commit='a' * 40,
                             source_paths=[source])
    _assert_drift_and_rollback(
        conn, lambda cur: cur.execute('ALTER TABLE public.work_item ADD COLUMN inventory_probe integer'),
        manifest, writer_inventory)


def test_catalog_inventory_detects_trigger_removal(store, tmp_path):
    conn = store.conn
    manifest, writer_inventory = _manifest(conn, tmp_path)
    _assert_drift_and_rollback(
        conn, lambda cur: cur.execute('DROP TRIGGER work_item_terminal_guard ON public.work_item'),
        manifest, writer_inventory)


def test_catalog_inventory_detects_grant_drift(store, tmp_path):
    conn = store.conn
    manifest, writer_inventory = _manifest(conn, tmp_path)
    _assert_drift_and_rollback(
        conn, lambda cur: cur.execute('GRANT SELECT ON public.work_item TO PUBLIC'),
        manifest, writer_inventory)


def _manifest(conn, tmp_path):
    source = tmp_path / 'writer.py'
    source.write_text('sql = "UPDATE work_item SET title=%s"\n')
    writer_inventory = build_writer_inventory(
        [source], {f'literal:{source}:1:work_item': 'work item writer'})
    role_names = _baseline(conn)['role_names']
    manifest = build_manifest(
        conn.cursor(), schema='public', role_names=role_names, source_commit='a' * 40,
        source_files=[str(source)], writer_inventory=writer_inventory)
    return manifest, writer_inventory
