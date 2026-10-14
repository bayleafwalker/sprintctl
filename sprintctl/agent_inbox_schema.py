"""Deployment-only schema 25 storage for the agent directory and inbox.

Contract v1 (vuoro docs/architecture/agent-directory-and-inbox.md).  Every
table is scoped by ``(repo_id, workspace_id)``.  Declarations, observations,
messages and receipts are append-only; an instance row changes once, when it
is retired.  ``agent_inbox_counter`` holds the per-repository ``inbox_seq``
allocation row (see :func:`sprintctl.agent_inbox_pg._allocate_inbox_seq`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

TABLES = (
    "agent_instance",
    "agent_declaration_revision",
    "agent_observation",
    "agent_inbox_counter",
    "agent_message",
    "agent_message_receipt",
)
INDEXES = (
    "idx_agent_instance_principal",
    "idx_agent_observation_latest",
    "idx_agent_message_recipient",
    "idx_agent_message_receipt_message",
    "uq_agent_receipt_per_instance",
    "uq_agent_receipt_decision",
    "uq_agent_receipt_run",
    "uq_agent_receipt_result",
    "uq_agent_receipt_cancel",
)
RELATIONS = TABLES + INDEXES
FUNCTIONS = ("sprintctl_agent_append_only", "sprintctl_agent_instance_guard")
EXPECTED_CATALOG_FINGERPRINT = "95bbcf667de720ad6668913d2328d6ea693972ad5c7d403d47f6c595e5f5c93b"


def install(cur: Any) -> None:
    """Install fresh tables only; a pre-existing lookalike is not adopted."""
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema25 refuses a pre-existing agent inbox relation or index")
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema25 refuses a pre-existing agent inbox guard function")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    """Return an exact schema fingerprint for startup compatibility checks."""
    return inspect_relations(
        cur, relations=RELATIONS, functions=FUNCTIONS, table_total=len(TABLES),
        expected=EXPECTED_CATALOG_FINGERPRINT,
    )


def inspect_relations(
    cur: Any, *, relations: tuple[str, ...], functions: tuple[str, ...],
    table_total: int, expected: str, trigger_tables: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Fingerprint the named relations, their columns, constraints, indexes,
    triggers and guard functions in the current schema.  Shared by every
    deployment-only store that is admitted by an exact catalog fingerprint
    (schema 25's inbox, schema 26's approvals).  ``trigger_tables`` are
    existing tables whose user triggers alone are fingerprinted as well (a
    guard a schema adds to a table it does not own)."""
    cur.execute(
        "WITH names(name) AS (SELECT unnest(%s::text[])), "
        "rels AS (SELECT count(*) FILTER (WHERE c.relkind='r') AS table_count, count(*) AS object_count, "
        "jsonb_agg(jsonb_build_array(c.relname,c.relkind) ORDER BY c.relname) AS v "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname IN (SELECT name FROM names) "
        "AND c.relkind IN ('r','i')), "
        "idx AS (SELECT jsonb_agg(jsonb_build_array(c.relname,replace(pg_get_indexdef(c.oid),"
        "quote_ident(current_schema()) || '.', ''),i.indisvalid,i.indisready) ORDER BY c.relname) v "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND t.relname IN (SELECT name FROM names)), "
        "cols AS (SELECT jsonb_agg(jsonb_build_array(c.relname,a.attname,a.attnum,"
        "format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,"
        "COALESCE(pg_get_expr(d.adbin,d.adrelid),'')) ORDER BY c.relname,a.attnum) v "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
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
        "AND (c.relname IN (SELECT name FROM names) OR c.relname = ANY(%s)) AND NOT t.tgisinternal), "
        "funcs AS (SELECT count(*) AS function_count, "
        "jsonb_agg(jsonb_build_array(p.proname,p.prosrc) ORDER BY p.proname) v "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)) "
        "SELECT rels.table_count, rels.object_count, funcs.function_count, rels.v AS relations, "
        "idx.v AS indexes, cols.v AS columns, cons.v AS constraints, trigs.v AS triggers, "
        "funcs.v AS functions FROM rels,idx,cols,cons,trigs,funcs",
        (list(relations), list(trigger_tables), list(functions)),
    )
    row = cur.fetchone()
    names = ("table_count", "object_count", "function_count", "relations", "indexes", "columns",
             "constraints", "triggers", "functions")
    get = row.get if isinstance(row, dict) else (lambda key: row[names.index(key)])
    table_count = int(get("table_count") or 0)
    object_count = int(get("object_count") or 0)
    function_count = int(get("function_count") or 0)
    parts = {key: get(key) for key in names[3:]}
    fingerprint = hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    available = table_count == table_total and fingerprint == expected
    return {
        "table_count": table_count,
        "catalog_fingerprint": fingerprint,
        "expected_catalog_fingerprint": expected,
        "available": available,
        "complete": (object_count == 0 and function_count == 0) or available,
    }


DDL = r"""
CREATE TABLE agent_instance (
    repo_id text NOT NULL,
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    instance_id text NOT NULL CHECK (instance_id ~ '^ins_[0-9A-HJKMNP-TV-Z]{26}$'),
    principal_id text NOT NULL CHECK (length(principal_id) BETWEEN 1 AND 512),
    registered_at timestamptz NOT NULL,
    retired_at timestamptz,
    retire_reason text,
    PRIMARY KEY (repo_id, workspace_id, instance_id),
    CHECK ((retired_at IS NULL) = (retire_reason IS NULL))
);
CREATE INDEX idx_agent_instance_principal ON agent_instance(repo_id, workspace_id, principal_id);

CREATE TABLE agent_declaration_revision (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    instance_id text NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    declaration jsonb NOT NULL,
    declaration_digest text NOT NULL CHECK (declaration_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, instance_id, revision),
    FOREIGN KEY (repo_id, workspace_id, instance_id)
        REFERENCES agent_instance(repo_id, workspace_id, instance_id) ON DELETE RESTRICT
);

CREATE TABLE agent_observation (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    observation_id bigint GENERATED ALWAYS AS IDENTITY,
    instance_id text NOT NULL,
    fact text NOT NULL CHECK (fact IN ('observed', 'reachable', 'executing', 'attended')),
    channel text CHECK (channel ~ '^[a-z][a-z0-9.-]{0,63}$'),
    run_id text CHECK (run_id ~ '^run_[0-9A-HJKMNP-TV-Z]{26}$'),
    observed_at timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    source text NOT NULL CHECK (source = 'self-reported'),
    PRIMARY KEY (repo_id, observation_id),
    CHECK ((fact = 'reachable') = (channel IS NOT NULL)),
    CHECK ((fact = 'executing') = (run_id IS NOT NULL)),
    CHECK (valid_until > observed_at AND valid_until <= observed_at + interval '3600 seconds'),
    FOREIGN KEY (repo_id, workspace_id, instance_id)
        REFERENCES agent_instance(repo_id, workspace_id, instance_id) ON DELETE RESTRICT
);
CREATE INDEX idx_agent_observation_latest
    ON agent_observation(repo_id, workspace_id, instance_id, fact, observation_id);

CREATE TABLE agent_inbox_counter (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    last_seq bigint NOT NULL CHECK (last_seq >= 0),
    PRIMARY KEY (repo_id, workspace_id)
);

CREATE TABLE agent_message (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    message_id text NOT NULL CHECK (message_id ~ '^msg_[0-9A-HJKMNP-TV-Z]{26}$'),
    inbox_seq bigint NOT NULL CHECK (inbox_seq >= 1),
    sender_principal_id text NOT NULL CHECK (length(sender_principal_id) > 0),
    recipient_principal_id text NOT NULL CHECK (length(recipient_principal_id) BETWEEN 1 AND 512),
    recipient_instance_id text CHECK (recipient_instance_id ~ '^ins_[0-9A-HJKMNP-TV-Z]{26}$'),
    envelope text NOT NULL CHECK (octet_length(envelope) <= 65536),
    envelope_digest text NOT NULL
        CHECK (envelope_digest = encode(sha256(convert_to(envelope, 'UTF8')), 'hex')),
    persisted_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, message_id),
    UNIQUE (repo_id, workspace_id, inbox_seq)
);
CREATE INDEX idx_agent_message_recipient
    ON agent_message(repo_id, workspace_id, recipient_principal_id, inbox_seq);

CREATE TABLE agent_message_receipt (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    receipt_seq bigint GENERATED ALWAYS AS IDENTITY,
    message_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('observed', 'acknowledged', 'accepted', 'refused',
        'run-linked', 'launch-ambiguous', 'result-linked', 'cancel-requested')),
    principal_id text NOT NULL CHECK (length(principal_id) > 0),
    instance_id text CHECK (instance_id ~ '^ins_[0-9A-HJKMNP-TV-Z]{26}$'),
    fields jsonb NOT NULL CHECK (jsonb_typeof(fields) = 'object'),
    field_digest text NOT NULL CHECK (field_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, receipt_seq),
    CHECK ((kind = 'cancel-requested') = (instance_id IS NULL)),
    FOREIGN KEY (repo_id, workspace_id, message_id)
        REFERENCES agent_message(repo_id, workspace_id, message_id) ON DELETE RESTRICT
);
CREATE INDEX idx_agent_message_receipt_message
    ON agent_message_receipt(repo_id, workspace_id, message_id, recorded_at, receipt_seq);
CREATE UNIQUE INDEX uq_agent_receipt_per_instance
    ON agent_message_receipt(repo_id, workspace_id, message_id, instance_id, kind)
    WHERE kind IN ('observed', 'acknowledged', 'launch-ambiguous');
CREATE UNIQUE INDEX uq_agent_receipt_decision
    ON agent_message_receipt(repo_id, workspace_id, message_id)
    WHERE kind IN ('accepted', 'refused');
CREATE UNIQUE INDEX uq_agent_receipt_run
    ON agent_message_receipt(repo_id, workspace_id, message_id) WHERE kind = 'run-linked';
CREATE UNIQUE INDEX uq_agent_receipt_result
    ON agent_message_receipt(repo_id, workspace_id, message_id) WHERE kind = 'result-linked';
CREATE UNIQUE INDEX uq_agent_receipt_cancel
    ON agent_message_receipt(repo_id, workspace_id, message_id) WHERE kind = 'cancel-requested';

CREATE FUNCTION sprintctl_agent_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514';
END; $$;

CREATE FUNCTION sprintctl_agent_instance_guard() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'agent instances are never deleted' USING ERRCODE='23514';
    END IF;
    IF OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
       OR (NEW.repo_id, NEW.workspace_id, NEW.instance_id, NEW.principal_id, NEW.registered_at)
          IS DISTINCT FROM
          (OLD.repo_id, OLD.workspace_id, OLD.instance_id, OLD.principal_id, OLD.registered_at) THEN
        RAISE EXCEPTION 'an agent instance changes only once, when it is retired' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;

CREATE TRIGGER sprintctl_agent_instance_guard
    BEFORE UPDATE OR DELETE ON agent_instance
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_instance_guard();
CREATE TRIGGER sprintctl_agent_instance_no_truncate
    BEFORE TRUNCATE ON agent_instance
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_declaration_append_only
    BEFORE UPDATE OR DELETE ON agent_declaration_revision
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_declaration_no_truncate
    BEFORE TRUNCATE ON agent_declaration_revision
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_observation_append_only
    BEFORE UPDATE OR DELETE ON agent_observation
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_observation_no_truncate
    BEFORE TRUNCATE ON agent_observation
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_message_append_only
    BEFORE UPDATE OR DELETE ON agent_message
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_message_no_truncate
    BEFORE TRUNCATE ON agent_message
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_receipt_append_only
    BEFORE UPDATE OR DELETE ON agent_message_receipt
    FOR EACH ROW EXECUTE FUNCTION sprintctl_agent_append_only();
CREATE TRIGGER sprintctl_agent_receipt_no_truncate
    BEFORE TRUNCATE ON agent_message_receipt
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_agent_append_only();
"""
