"""Deployment-only schema 23 storage for run invocation receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

RELATIONS = (
    "work_run_invocation_binding",
    "work_run_invocation_attempt",
    "idx_work_run_invocation_attempt_owner",
)
FUNCTIONS = (
    "sprintctl_run_invocation_binding_guard",
    "sprintctl_run_invocation_attempt_guard",
    "sprintctl_run_invocation_immutable_guard",
    "sprintctl_run_invocation_has_attempt",
)
TABLES = ("work_run_invocation_binding", "work_run_invocation_attempt")
EXPECTED_CATALOG_FINGERPRINT = "5f692d61d0d4b7234bb5eb9626fa4a6d9510112fcc91812ba2a76bc6850f1291"


def install(cur: Any) -> None:
    """Install fresh tables only; a pre-existing lookalike is not adopted."""
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError(
            "schema23 refuses a pre-existing invocation relation or index"
        )
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError(
            "schema23 refuses a pre-existing invocation guard function"
        )
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    """Return an exact schema fingerprint for startup compatibility checks."""
    cur.execute(
        "WITH names(name) AS (VALUES "
        "('work_run_invocation_binding'),('work_run_invocation_attempt'),"
        "('idx_work_run_invocation_attempt_owner')), "
        "rels AS (SELECT count(*) FILTER (WHERE c.relkind='r') AS table_count, count(*) AS object_count, "
        "jsonb_agg(jsonb_build_array(c.relname,c.relkind) ORDER BY c.relname) AS v "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND (c.relname IN (SELECT name FROM names) "
        "OR left(c.relname,length('work_run_invocation_binding_'))='work_run_invocation_binding_' "
        "OR left(c.relname,length('work_run_invocation_attempt_'))='work_run_invocation_attempt_') "
        "AND c.relkind IN ('r','i')), "
        "idx AS (SELECT jsonb_agg(jsonb_build_array(c.relname,replace(pg_get_indexdef(c.oid),"
        "quote_ident(current_schema()) || '.', ''),"
        "i.indisvalid,i.indisready) ORDER BY c.relname) v FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname='idx_work_run_invocation_attempt_owner'), "
        "cols AS (SELECT jsonb_agg(jsonb_build_array(c.relname,a.attname,a.attnum,"
        "format_type(a.atttypid,a.atttypmod),t.typname,a.attnotnull,"
        "COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY c.relname,a.attnum) v "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE n.nspname=current_schema() AND c.relname IN (SELECT name FROM names) "
        "AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped), "
        "cons AS (SELECT jsonb_agg(jsonb_build_array(c.relname,con.conname,con.contype,"
        "pg_get_constraintdef(con.oid,true)) ORDER BY c.relname,con.conname) v "
        "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname IN (SELECT name FROM names)), "
        "trigs AS (SELECT jsonb_agg(jsonb_build_array(c.relname,t.tgname,p.proname,"
        "t.tgenabled,t.tgtype) ORDER BY c.relname,t.tgname) v FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid WHERE n.nspname=current_schema() "
        "AND c.relname IN (SELECT name FROM names) AND NOT t.tgisinternal), "
        "funcs AS (SELECT count(*) AS function_count, "
        "jsonb_agg(jsonb_build_array(p.proname,p.prosrc) ORDER BY p.proname) v "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)) "
        "SELECT rels.table_count, rels.object_count, funcs.function_count, rels.v AS relations, "
        "idx.v AS indexes, "
        "cols.v AS columns, cons.v AS constraints, trigs.v AS triggers, funcs.v AS functions "
        "FROM rels,idx,cols,cons,trigs,funcs",
        (list(FUNCTIONS),),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        get = row.get
    else:
        names = ("table_count", "object_count", "function_count", "relations", "indexes", "columns", "constraints", "triggers", "functions")
        get = lambda key: row[names.index(key)]
    table_count = int(get("table_count") or 0)
    object_count = int(get("object_count") or 0)
    function_count = int(get("function_count") or 0)
    parts = {
        "relations": get("relations"),
        "indexes": get("indexes"),
        "columns": get("columns"),
        "constraints": get("constraints"),
        "triggers": get("triggers"),
        "functions": get("functions"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    complete = (
        (object_count == 0 and function_count == 0)
        or (table_count == len(TABLES) and fingerprint == EXPECTED_CATALOG_FINGERPRINT)
    )
    return {
        "table_count": table_count,
        "catalog_fingerprint": fingerprint,
        "expected_catalog_fingerprint": EXPECTED_CATALOG_FINGERPRINT,
        "available": table_count == len(TABLES) and fingerprint == EXPECTED_CATALOG_FINGERPRINT,
        "complete": complete,
    }


DDL = r"""
CREATE TABLE work_run_invocation_binding (
    invocation_id text PRIMARY KEY
        CHECK (invocation_id ~ '^inv_[0-9A-HJKMNP-TV-Z]{26}$'),
    repo_id text NOT NULL,
    run_id text NOT NULL CHECK (run_id ~ '^run_[0-9A-HJKMNP-TV-Z]{26}$'),
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    principal_id text NOT NULL CHECK (length(principal_id) > 0),
    principal_epoch bigint NOT NULL CHECK (principal_epoch BETWEEN 0 AND 9007199254740991),
    client_id text,
    grant_id text,
    owner_request_digest text NOT NULL CHECK (owner_request_digest ~ '^[0-9a-f]{64}$'),
    owner_record_digest text NOT NULL CHECK (owner_record_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (repo_id, run_id),
    UNIQUE (invocation_id, repo_id, run_id),
    FOREIGN KEY (repo_id, run_id) REFERENCES run(repo_id, run_id) ON DELETE RESTRICT
);

CREATE TABLE work_run_invocation_attempt (
    attempt_id text PRIMARY KEY
        CHECK (attempt_id ~ '^att_[0-9A-HJKMNP-TV-Z]{26}$'),
    invocation_id text NOT NULL,
    repo_id text NOT NULL,
    run_id text NOT NULL,
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    principal_id text NOT NULL CHECK (length(principal_id) > 0),
    principal_epoch bigint NOT NULL CHECK (principal_epoch BETWEEN 0 AND 9007199254740991),
    client_id text,
    grant_id text,
    owner_request_digest text NOT NULL CHECK (owner_request_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (invocation_id) REFERENCES work_run_invocation_binding(invocation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (repo_id, run_id) REFERENCES run(repo_id, run_id) ON DELETE RESTRICT
);
CREATE INDEX idx_work_run_invocation_attempt_owner
    ON work_run_invocation_attempt(repo_id, run_id, invocation_id, attempt_id);

CREATE FUNCTION sprintctl_run_invocation_immutable_guard() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'run invocation receipts are append-only' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'run invocation receipts are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION sprintctl_run_invocation_binding_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE owner_run run%ROWTYPE;
BEGIN
    SELECT * INTO owner_run FROM run
      WHERE repo_id=NEW.repo_id AND run_id=NEW.run_id FOR KEY SHARE;
    IF NOT FOUND OR owner_run.workspace_id IS DISTINCT FROM NEW.workspace_id
       OR owner_run.principal_id IS DISTINCT FROM NEW.principal_id
       OR owner_run.client_id IS DISTINCT FROM NEW.client_id
       OR owner_run.grant_id IS DISTINCT FROM NEW.grant_id
       OR owner_run.request_digest IS DISTINCT FROM NEW.owner_request_digest THEN
        RAISE EXCEPTION 'run invocation owner binding mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION sprintctl_run_invocation_attempt_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE binding work_run_invocation_binding%ROWTYPE;
BEGIN
    SELECT * INTO binding FROM work_run_invocation_binding
      WHERE invocation_id=NEW.invocation_id FOR KEY SHARE;
    IF NOT FOUND OR binding.repo_id IS DISTINCT FROM NEW.repo_id
       OR binding.run_id IS DISTINCT FROM NEW.run_id
       OR binding.workspace_id IS DISTINCT FROM NEW.workspace_id
       OR binding.principal_id IS DISTINCT FROM NEW.principal_id
       OR binding.principal_epoch IS DISTINCT FROM NEW.principal_epoch
       OR binding.client_id IS DISTINCT FROM NEW.client_id
       OR binding.grant_id IS DISTINCT FROM NEW.grant_id
       OR binding.owner_request_digest IS DISTINCT FROM NEW.owner_request_digest THEN
        RAISE EXCEPTION 'run invocation attempt binding mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION sprintctl_run_invocation_has_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE target_invocation text;
BEGIN
    IF TG_TABLE_NAME='work_run_invocation_binding' THEN
        target_invocation := NEW.invocation_id;
    ELSE
        target_invocation := NEW.invocation_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM work_run_invocation_attempt
                   WHERE invocation_id=target_invocation) THEN
        RAISE EXCEPTION 'run invocation binding requires an admitted attempt'
            USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END; $$;

CREATE TRIGGER sprintctl_run_invocation_binding_insert_guard
    BEFORE INSERT ON work_run_invocation_binding
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_invocation_binding_guard();
CREATE TRIGGER sprintctl_run_invocation_binding_immutable
    BEFORE UPDATE OR DELETE ON work_run_invocation_binding
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_invocation_immutable_guard();
CREATE TRIGGER sprintctl_run_invocation_binding_no_truncate
    BEFORE TRUNCATE ON work_run_invocation_binding
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_run_invocation_immutable_guard();
CREATE TRIGGER sprintctl_run_invocation_attempt_insert_guard
    BEFORE INSERT ON work_run_invocation_attempt
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_invocation_attempt_guard();
CREATE TRIGGER sprintctl_run_invocation_attempt_immutable
    BEFORE UPDATE OR DELETE ON work_run_invocation_attempt
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_invocation_immutable_guard();
CREATE TRIGGER sprintctl_run_invocation_attempt_no_truncate
    BEFORE TRUNCATE ON work_run_invocation_attempt
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_run_invocation_immutable_guard();
CREATE CONSTRAINT TRIGGER sprintctl_run_invocation_binding_has_attempt
    AFTER INSERT ON work_run_invocation_binding
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION sprintctl_run_invocation_has_attempt();
"""
