"""Deployment-only schema 26 storage for structured transition approvals.

``work_approval`` holds one immutable operator approval of a named transition
for one run of one repository, until ``expires_at``.  The approver is the
authenticated principal (with its OAuth client and grant) that recorded it,
never an argument.  Rows are append-only: an approval is never edited,
revoked by update or deleted; it simply expires.  The run foreign key is
RESTRICT, so a run that an approval names cannot be deleted from under it.

Schema 26 also makes the facts ``work.run.resolve-v2`` reports permanent in
storage, not only by convention: ``run_predecessor`` becomes append-only (no
UPDATE, DELETE or TRUNCATE, so a superseded run stays superseded and a run
with a successor link can no longer be deleted at all), and a run's binding
columns and ``created_at`` (``issued_at``) can no longer change.  Those
guard triggers are part of the exact catalog fingerprint.

Like the other deployment-only stores, :func:`install` runs only inside
:func:`sprintctl.pg_migrations.migrate_schema` (advisory lock, migration
role); the runtime only reads the catalog through :func:`inspect_catalog`
and is admitted at exactly schema 26.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

#: Upper bound on one approval's lifetime (expires_at - recorded_at).  A
#: sanity bound enforced by storage; the consumer applies its own, usually
#: much shorter, maximum approval age.
MAX_LIFETIME_SECONDS = 7 * 24 * 3600

TABLES = ("work_approval",)
INDEXES = ("idx_work_approval_run",)
RELATIONS = TABLES + INDEXES
#: Existing tables that schema 26 guards with triggers of its own.
TRIGGER_TABLES = ("run", "run_predecessor")
TRIGGERS = (
    "sprintctl_approval_append_only", "sprintctl_approval_no_truncate",
    "sprintctl_run_predecessor_append_only", "sprintctl_run_predecessor_no_truncate",
    "sprintctl_run_binding_immutable",
)
FUNCTIONS = ("sprintctl_approval_append_only", "sprintctl_run_binding_immutable")
EXPECTED_CATALOG_FINGERPRINT = "25937f07c44a1845174b46c9e3d0d50b939d0dfa298cec75baff340a43ff8ced"


def install(cur: Any) -> None:
    """Install fresh storage only; a pre-existing lookalike is not adopted."""
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)",
        (list(RELATIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema26 refuses a pre-existing approval relation or index")
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)",
        (list(FUNCTIONS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema26 refuses a pre-existing approval guard function")
    cur.execute(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND t.tgname=ANY(%s)",
        (list(TRIGGERS),),
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema26 refuses a pre-existing guard trigger")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    """Return an exact schema fingerprint for startup compatibility checks."""
    from .agent_inbox_schema import inspect_relations

    return inspect_relations(
        cur, relations=RELATIONS, functions=FUNCTIONS, table_total=len(TABLES),
        expected=EXPECTED_CATALOG_FINGERPRINT, trigger_tables=TRIGGER_TABLES,
    )


DDL = rf"""
CREATE TABLE work_approval (
    repo_id text NOT NULL,
    approval_id text NOT NULL CHECK (approval_id ~ '^apr_[0-9A-HJKMNP-TV-Z]{{26}}$'),
    workspace_id text NOT NULL CHECK (length(workspace_id) > 0),
    run_id text NOT NULL,
    transition text NOT NULL CHECK (transition ~ '^[a-z0-9][a-z0-9._-]{{0,127}}$'),
    rationale text CHECK (octet_length(rationale) <= 2048),
    approver_principal_id text NOT NULL CHECK (length(approver_principal_id) > 0),
    approver_client_id text,
    approver_grant_id text,
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{{8,128}}$'),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{{64}}$'),
    recorded_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, approval_id),
    UNIQUE (repo_id, workspace_id, approver_principal_id, idempotency_key),
    CHECK (expires_at > recorded_at
           AND expires_at <= recorded_at + interval '{MAX_LIFETIME_SECONDS} seconds'),
    FOREIGN KEY (repo_id, run_id) REFERENCES run(repo_id, run_id) ON DELETE RESTRICT
);
CREATE INDEX idx_work_approval_run ON work_approval(repo_id, run_id, transition);

CREATE FUNCTION sprintctl_approval_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514';
END; $$;

CREATE TRIGGER sprintctl_approval_append_only
    BEFORE UPDATE OR DELETE ON work_approval
    FOR EACH ROW EXECUTE FUNCTION sprintctl_approval_append_only();
CREATE TRIGGER sprintctl_approval_no_truncate
    BEFORE TRUNCATE ON work_approval
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_approval_append_only();

-- Supersession is permanent: a continuation link is never edited or removed,
-- including by the CASCADE from deleting its successor run.
CREATE TRIGGER sprintctl_run_predecessor_append_only
    BEFORE UPDATE OR DELETE ON run_predecessor
    FOR EACH ROW EXECUTE FUNCTION sprintctl_approval_append_only();
CREATE TRIGGER sprintctl_run_predecessor_no_truncate
    BEFORE TRUNCATE ON run_predecessor
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_approval_append_only();

-- A run's binding and issue time never change once minted.
CREATE FUNCTION sprintctl_run_binding_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF (NEW.repo_id, NEW.run_id, NEW.principal_id, NEW.workspace_id, NEW.client_id,
        NEW.grant_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.repo_id, OLD.run_id, OLD.principal_id, OLD.workspace_id, OLD.client_id,
        OLD.grant_id, OLD.created_at) THEN
        RAISE EXCEPTION 'a run''s binding and created_at are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER sprintctl_run_binding_immutable
    BEFORE UPDATE ON run
    FOR EACH ROW EXECUTE FUNCTION sprintctl_run_binding_immutable();
"""
