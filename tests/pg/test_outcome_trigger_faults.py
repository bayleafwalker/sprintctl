"""Opt-in supported trigger-disabling fault setup on a new temporary database.

Only setup uses an administrator. The actual owner invocation reconnects with
an unprivileged preflighted role. No internal catalog rows are edited/deleted.
"""
import os
from pathlib import Path
import uuid

import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import assert_disposable_connection, dict_row, psycopg
from tests.pg.test_outcome_current_basis import setup, invoke


@pytest.mark.parametrize('function', ['RI_FKey_check_ins', 'RI_FKey_check_upd'])
def test_disabled_source_fk_trigger_refuses_unprivileged_owner(function):
    admin_url = os.environ['SPRINTCTL_TEST_FK_FAULT_ADMIN_URL']
    instance = Path(os.environ['SPRINTCTL_TEST_FK_FAULT_DATA_DIRECTORY'])
    assert instance.is_absolute() and instance.resolve() == instance
    assert Path('/tmp') in instance.parents and instance.name == 'data'
    role = os.environ['SPRINTCTL_TEST_FK_FAULT_ROLE']
    assert role.startswith('sprintctl_test_')
    database = 'sprintctl_test_fk_fault_' + uuid.uuid4().hex
    admin = psycopg.connect(admin_url, row_factory=dict_row, autocommit=True)
    admin.execute("SET client_encoding='UTF8'")
    target = owner = None
    created = False
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT current_setting('data_directory') AS directory, host(inet_server_addr()) AS address")
            row = cur.fetchone()
            assert Path(row['directory']) == instance
            assert row['address'] in ('127.0.0.1', '::1')
            cur.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s', (role,))
            assert not any(cur.fetchone().values())
            cur.execute(psycopg.sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                psycopg.sql.Identifier(database), psycopg.sql.Identifier(role)))
            created = True
            cur.execute(psycopg.sql.SQL("COMMENT ON DATABASE {} IS 'sprintctl:disposable-integration-test'").format(psycopg.sql.Identifier(database)))
        owner = psycopg.connect(admin_url, dbname=database, user=role, row_factory=dict_row)
        assert_disposable_connection(owner)
        store = pg.PgStore(conn=owner, repo_id='itest-fk-fault-' + uuid.uuid4().hex[:12], authority_repo_uuid=str(uuid.uuid4()),
                           connection_factory=lambda: psycopg.connect(admin_url, dbname=database, user=role, row_factory=dict_row))
        pg.init_db(store)
        item, body = setup(store)
        owner.commit()
        target = psycopg.connect(admin_url, dbname=database, row_factory=dict_row, autocommit=True)
        with target.cursor() as cur:
            cur.execute("SELECT t.tgname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                        "WHERE t.tgrelid='ref'::regclass AND t.tgisinternal AND p.proname=%s", (function,))
            trigger = cur.fetchone()
            assert trigger is not None
            cur.execute(psycopg.sql.SQL('ALTER TABLE ref DISABLE TRIGGER {}').format(psycopg.sql.Identifier(trigger['tgname'])))
        with pytest.raises(ApplicationRejection) as error:
            invoke(store, body)
        assert error.value.code == 'work-basis-unavailable'
        assert not pg.list_decisions(store, item)
    finally:
        if owner is not None: owner.close()
        if target is not None: target.close()
        if created:
            with admin.cursor() as cur:
                cur.execute(psycopg.sql.SQL('DROP DATABASE {}').format(psycopg.sql.Identifier(database)))
        admin.close()
