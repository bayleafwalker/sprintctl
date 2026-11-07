"""Deployment-only schema 34 for adjunct agent capability revisions."""
from __future__ import annotations

from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

TABLES = ("agent_capability_revision",)
INDEXES = ("idx_agent_capability_latest",)
RELATIONS = TABLES + INDEXES
FUNCTIONS = ("sprintctl_agent_capability_append_only",)
EXPECTED_CATALOG_FINGERPRINT = "58cf59f4aff2501df8abca67448c851a52d1ebc56c7951f91ad9ffbb8b6417ac"


def install(cur: Any) -> None:
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema34 refuses a pre-existing agent capability relation or index")
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema34 refuses a pre-existing agent capability guard function")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    from .agent_inbox_schema import inspect_relations
    return inspect_relations(
        cur, relations=RELATIONS, functions=FUNCTIONS, table_total=len(TABLES),
        expected=EXPECTED_CATALOG_FINGERPRINT, trigger_tables=TABLES,
    )


DDL = r"""
CREATE TABLE agent_capability_revision (
    repo_id text NOT NULL,
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    instance_id text NOT NULL CHECK (instance_id ~ '^ins_[0-9A-HJKMNP-TV-Z]{26}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    base_declaration_revision integer NOT NULL CHECK (base_declaration_revision >= 1),
    capability jsonb NOT NULL CHECK (
        jsonb_typeof(capability) = 'object' AND capability->>'schema' = 'agent-declaration/v2'
    ),
    capability_digest text NOT NULL CHECK (capability_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, instance_id, revision),
    FOREIGN KEY (repo_id, workspace_id, instance_id)
        REFERENCES agent_instance(repo_id, workspace_id, instance_id) ON DELETE RESTRICT,
    FOREIGN KEY (repo_id, workspace_id, instance_id, base_declaration_revision)
        REFERENCES agent_declaration_revision(repo_id, workspace_id, instance_id, revision) ON DELETE RESTRICT
);
CREATE INDEX idx_agent_capability_latest
    ON agent_capability_revision(repo_id, workspace_id, instance_id, revision DESC);

CREATE FUNCTION sprintctl_agent_capability_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514';
END; $$;
CREATE TRIGGER agent_capability_append_only
    BEFORE UPDATE OR DELETE ON agent_capability_revision
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_capability_append_only();
CREATE TRIGGER agent_capability_no_truncate
    BEFORE TRUNCATE ON agent_capability_revision
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_capability_append_only();
"""
