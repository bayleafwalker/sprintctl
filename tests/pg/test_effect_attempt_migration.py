"""Populated upgrade, atomic refusal and storage catalog qualification."""
import re

import pytest

from sprintctl import effect_attempt_schema as schema, pg, pg_migrations
from tests.pg._shared import PG_MARKS, psycopg
from tests.pg.test_effect_verification_migration import migration_store
from tests.pg.test_effect_intent import _accept, _get
from tests.pg.test_protected_artifact_acceptance import setup

pytestmark = PG_MARKS


def make_schema21(store):
    with store.conn.cursor() as cur:
        cur.execute("DROP TABLE work_effect_attempt_event, work_effect_attempt")
        cur.execute("DROP FUNCTION sprintctl_effect_attempt_guard(), "
                    "sprintctl_effect_attempt_event_guard(), sprintctl_effect_attempt_consistency()")
        cur.execute("UPDATE schema_version SET version=21")
    store.conn.commit()


def assert_no_attempt_storage(store):
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 21
        cur.execute("SELECT to_regclass('work_effect_attempt') AS attempt, "
                    "to_regclass('work_effect_attempt_event') AS event")
        assert cur.fetchone() == {"attempt": None, "event": None}
    store.conn.rollback()


def test_populated_upgrade_preserves_acceptance_and_runtime_probe_does_not_migrate(migration_store):
    store = migration_store
    _, _, intent = setup(store, required=False)
    accepted = _accept(store, intent)
    make_schema21(store)
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError):
        pg_migrations.require_compatible_schema(store)
    store.conn.rollback()
    assert_no_attempt_storage(store)
    result = pg_migrations.migrate_schema(store)
    assert result["applied_versions"] == [22]
    assert result["compatibility"]["remote_schema"]["actual"] == 22
    assert _get(store, intent["intent_id"]) == accepted
    with store.conn.cursor() as cur:
        cur.execute("SELECT p.proname,pg_get_functiondef(p.oid) AS body FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() "
                    "AND p.proname=ANY(%s) ORDER BY p.proname", (list(schema.FUNCTIONS),))
        definitions = cur.fetchall()
    store.conn.rollback()
    assert pg_migrations.migrate_schema(store)["applied_versions"] == []
    with store.conn.cursor() as cur:
        cur.execute("SELECT p.proname,pg_get_functiondef(p.oid) AS body FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() "
                    "AND p.proname=ANY(%s) ORDER BY p.proname", (list(schema.FUNCTIONS),))
        assert cur.fetchall() == definitions
    store.conn.rollback()
    assert _get(store, intent["intent_id"]) == accepted


@pytest.mark.parametrize("name", schema.RELATIONS)
def test_foreign_relation_or_index_name_refuses_atomically(migration_store, name):
    store = migration_store
    make_schema21(store)
    with store.conn.cursor() as cur:
        cur.execute(f'CREATE VIEW "{name}" AS SELECT 42 AS foreign_value')
    store.conn.commit()
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="pre-existing attempt relation"):
        pg_migrations.migrate_schema(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 21
        cur.execute(f'SELECT foreign_value FROM "{name}"')
        assert cur.fetchone()["foreign_value"] == 42
        cur.execute("SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)", (list(schema.RELATIONS),))
        assert cur.fetchone()["n"] == 1
    store.conn.rollback()


@pytest.mark.parametrize("name", schema.FUNCTIONS)
def test_foreign_guard_overload_refuses_atomically(migration_store, name):
    store = migration_store
    make_schema21(store)
    with store.conn.cursor() as cur:
        cur.execute(f'CREATE FUNCTION "{name}"(integer) RETURNS integer LANGUAGE sql AS $$SELECT $1$$')
    store.conn.commit()
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="pre-existing attempt guard"):
        pg_migrations.migrate_schema(store)
    assert_no_attempt_storage(store)
    with store.conn.cursor() as cur:
        cur.execute(f'SELECT "{name}"(42) AS value')
        assert cur.fetchone()["value"] == 42
    store.conn.rollback()


def test_injected_mid_ddl_failure_rolls_back_objects_and_version(migration_store, monkeypatch):
    store = migration_store
    make_schema21(store)
    ddl = schema.DDL
    monkeypatch.setattr(schema, "DDL", ddl + "\nSELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        pg_migrations.migrate_schema(store)
    assert_no_attempt_storage(store)
    monkeypatch.setattr(schema, "DDL", ddl)
    assert pg_migrations.migrate_schema(store)["applied_versions"] == [22]


def test_catalog_has_restrictive_parent_links_unique_authorizations_and_exact_guard_definitions(migration_store):
    store = migration_store
    with store.conn.cursor() as cur:
        cur.execute("SELECT c.relname AS child,f.relname AS parent,k.confdeltype AS deletion "
                    "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                    "JOIN pg_class f ON f.oid=k.confrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND c.relname IN ('work_effect_attempt','work_effect_attempt_event') "
                    "AND k.contype='f' ORDER BY c.relname,f.relname")
        assert cur.fetchall() == [
            {"child": "work_effect_attempt", "parent": "work_effect_intent", "deletion": "r"},
            {"child": "work_effect_attempt", "parent": "work_item", "deletion": "r"},
            {"child": "work_effect_attempt", "parent": "work_release", "deletion": "r"},
            {"child": "work_effect_attempt_event", "parent": "work_effect_attempt", "deletion": "r"},
        ]
        cur.execute("SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                    "WHERE conrelid='work_effect_attempt'::regclass AND contype='u' ORDER BY conname")
        assert cur.fetchall() == [
            {"conname": "effect_attempt_intent_operation_unique",
             "definition": "UNIQUE (repo_id, intent_id, intent_revision, provider_operation)"},
            {"conname": "effect_attempt_open_key_unique",
             "definition": "UNIQUE (repo_id, workspace_id, principal_id, idempotency_key)"},
        ]
        cur.execute("SELECT tgname,tgenabled,tgdeferrable,tginitdeferred FROM pg_trigger "
                    "WHERE tgrelid IN ('work_effect_attempt'::regclass,'work_effect_attempt_event'::regclass) "
                    "AND NOT tgisinternal ORDER BY tgname")
        triggers = cur.fetchall()
        assert len(triggers) == 6 and all(r["tgenabled"] == "O" for r in triggers)
        assert {r["tgname"] for r in triggers if r["tgdeferrable"] and r["tginitdeferred"]} == {
            "sprintctl_effect_attempt_consistency", "sprintctl_effect_attempt_event_consistency"}
        for name in schema.FUNCTIONS:
            cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=current_schema() AND proname=%s", (name,))
            installed = cur.fetchone()["prosrc"]
            declared = re.search(r"CREATE FUNCTION " + name + r"\(\).*?AS \$\$(.*?)\$\$;", schema.DDL, re.S).group(1)
            assert installed == declared
    store.conn.rollback()
