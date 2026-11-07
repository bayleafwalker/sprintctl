"""Disposable PostgreSQL storage checks for schema34 adjunct agent capabilities."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from sprintctl import agent_capability_schema, pg, pg_migrations
from sprintctl.agent_inbox import canonical_bytes
from sprintctl.pg_migrations import RemoteSchemaMigrationError
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg

pytestmark = PG_MARKS


def _v1():
    return {
        "schema": "agent-declaration/v1", "harness": {"name": "test", "version": "1"},
        "adapter": None, "host_class": "cloud-unattended", "interactivity": "non-interactive",
        "accepted_kinds": ["continue"], "channels": [],
    }


def _v2():
    return {
        "schema": "agent-declaration/v2", "harness": {"name": "test", "version": "1"},
        "adapter": None, "host_class": "cloud-unattended", "interactivity": "non-interactive",
        "accepted_kinds": ["request-resolution/v1"], "channels": [],
    }


def test_schema34_catalog_and_append_only_guards_are_exact(store, pg_test_scope):
    repo_id = pg_test_scope("agent-capability-schema")
    scoped = pg.PgStore(conn=store.conn, repo_id=repo_id)
    pg.init_db(scoped)
    instance = "ins_" + "D" * 26
    base = _v1()
    encoded_base = canonical_bytes(base).decode()
    capability = _v2()
    encoded_capability = canonical_bytes(capability).decode()
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) "
            "VALUES (%s,'ws-capability',%s,'principal-capability',now())",
            (repo_id, instance),
        )
        cur.execute(
            "INSERT INTO agent_declaration_revision(repo_id,workspace_id,instance_id,revision,"
            "declaration,declaration_digest,recorded_at) VALUES (%s,'ws-capability',%s,1,%s::jsonb,%s,now())",
            (repo_id, instance, encoded_base, hashlib.sha256(encoded_base.encode()).hexdigest()),
        )
        cur.execute(
            "INSERT INTO agent_capability_revision(repo_id,workspace_id,instance_id,revision,"
            "base_declaration_revision,capability,capability_digest,recorded_at) "
            "VALUES (%s,'ws-capability',%s,1,1,%s::jsonb,%s,now())",
            (repo_id, instance, encoded_capability,
             hashlib.sha256(encoded_capability.encode()).hexdigest()),
        )
        catalog = agent_capability_schema.inspect_catalog(cur)
        assert catalog["available"], catalog

    for statement in (
        "UPDATE agent_capability_revision SET capability_digest=capability_digest WHERE repo_id=%s",
        "DELETE FROM agent_capability_revision WHERE repo_id=%s",
        "TRUNCATE agent_capability_revision",
    ):
        with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
            with store.conn.transaction(), store.conn.cursor() as cur:
                cur.execute(statement, (repo_id,)) if "%s" in statement else cur.execute(statement)


def test_schema34_migration_from_33_installs_only_adjunct_storage(store, pg_test_scope):
    schema = "agent_capability_migration_" + uuid.uuid4().hex
    with store.conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(pg.PG_DDL)
    store.conn.commit()
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        isolated = pg.PgStore(conn=conn, repo_id=pg_test_scope("agent-capability-migration"))
        pg_migrations.migrate_schema(isolated)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE agent_capability_revision")
            cur.execute("DROP FUNCTION sprintctl_agent_capability_append_only()")
            cur.execute("UPDATE schema_version SET version=33")
        conn.commit()
        migrated = pg_migrations.migrate_schema(
            isolated)
        assert migrated["applied_versions"] == [34]
        assert migrated["to_version"] == 34
        with conn.cursor() as cur:
            assert agent_capability_schema.inspect_catalog(cur)["available"]
        conn.commit()
    finally:
        conn.close()
        with store.conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        store.conn.commit()


def test_schema34_refuses_preexisting_capability_relation(store):
    schema = "agent_capability_foreign_" + uuid.uuid4().hex
    with store.conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute("CREATE TABLE agent_capability_revision (foreign_shape integer)")
        with pytest.raises(RemoteSchemaMigrationError, match="pre-existing agent capability"):
            agent_capability_schema.install(cur)
    store.conn.rollback()
    with store.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    store.conn.commit()
