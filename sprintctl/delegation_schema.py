"""Deployment-only schema 27 storage for run-scoped delegations."""

from __future__ import annotations

from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

TABLES = ("work_run_delegation",)
INDEXES = ("idx_work_run_delegation_run",)
RELATIONS = TABLES + INDEXES
FUNCTIONS = ("sprintctl_run_delegation_guard",)
TRIGGERS = (
    "sprintctl_run_delegation_append_only",
    "sprintctl_run_delegation_no_truncate",
)
EXPECTED_CATALOG_FINGERPRINT = "d7c1c644419ffe549448f7e1efe42870df1368880d3c3b312e943ce775929bc7"


def install(cur: Any) -> None:
    """Install fresh owner storage only; refuse lookalike pre-existing objects."""
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema27 refuses a pre-existing delegation relation or index")
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema27 refuses a pre-existing delegation guard function")
    cur.execute(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND t.tgname=ANY(%s)",
        (list(TRIGGERS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema27 refuses a pre-existing delegation guard trigger")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    from .agent_inbox_schema import inspect_relations

    return inspect_relations(
        cur, relations=RELATIONS, functions=FUNCTIONS,
        table_total=len(TABLES), expected=EXPECTED_CATALOG_FINGERPRINT,
        trigger_tables=TABLES,
    )


DDL = """
CREATE TABLE work_run_delegation (
    repo_id text NOT NULL,
    delegation_id text NOT NULL CHECK (delegation_id ~ '^dlg_[0-9A-HJKMNP-TV-Z]{26}$'),
    run_id text NOT NULL,
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    principal_id text NOT NULL CHECK (length(principal_id) > 0),
    client_id text,
    grant_id text,
    creator_instance_id text NOT NULL,
    operations jsonb NOT NULL CHECK (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) > 0
        AND operations <@ '["work.run.resolve-v1","work.run.predecessor-context-v1",
            "work.session-note.write-v1","work.evidence.append-v1"]'::jsonb),
    parent_authorities jsonb NOT NULL CHECK (jsonb_typeof(parent_authorities) = 'array'
        AND parent_authorities <@ '["work:read","work:evidence"]'::jsonb),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{8,128}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    token_sha256 text NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_reason text CHECK (revoked_reason IS NULL OR revoked_reason ~ '^[a-z][a-z0-9-]{0,63}$'),
    PRIMARY KEY (repo_id, delegation_id),
    UNIQUE (token_sha256),
    UNIQUE (repo_id, workspace_id, principal_id, idempotency_key),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '4 hours'),
    CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL)),
    FOREIGN KEY (repo_id, run_id) REFERENCES run(repo_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (repo_id, workspace_id, creator_instance_id)
        REFERENCES agent_instance(repo_id, workspace_id, instance_id) ON DELETE RESTRICT
);
CREATE INDEX idx_work_run_delegation_run
    ON work_run_delegation(repo_id, workspace_id, run_id, expires_at);

CREATE FUNCTION sprintctl_run_delegation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'work_run_delegation is append-only except for revocation'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'work_run_delegation is append-only except for revocation'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.repo_id, NEW.delegation_id, NEW.run_id, NEW.workspace_id, NEW.principal_id,
        NEW.client_id, NEW.grant_id, NEW.creator_instance_id, NEW.operations,
        NEW.parent_authorities, NEW.idempotency_key, NEW.request_digest, NEW.token_sha256,
        NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.repo_id, OLD.delegation_id, OLD.run_id, OLD.workspace_id, OLD.principal_id,
        OLD.client_id, OLD.grant_id, OLD.creator_instance_id, OLD.operations,
        OLD.parent_authorities, OLD.idempotency_key, OLD.request_digest, OLD.token_sha256,
        OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'delegation identity and scope are immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revoked_reason IS NULL THEN
        RAISE EXCEPTION 'delegation revocation is one-way and requires a reason' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER sprintctl_run_delegation_append_only
    BEFORE UPDATE OR DELETE ON work_run_delegation
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_delegation_guard();
CREATE TRIGGER sprintctl_run_delegation_no_truncate
    BEFORE TRUNCATE ON work_run_delegation
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_run_delegation_guard();
"""
