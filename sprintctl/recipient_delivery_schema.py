"""Deployment-only schema 33 for operation-bound recipient delivery receipts.

This ledger stores only verified assertion identifiers and digests.  The
signed assertion itself is deliberately never persisted.
"""
from __future__ import annotations

from .pg_migrations import RemoteSchemaMigrationError

TABLES = ("operator_request_delivery", "operator_request_delivery_key")
RELATIONS = TABLES + (
    "operator_request_delivery_pkey", "uq_request_delivery_logical",
    "uq_request_delivery_jti", "operator_request_delivery_key_pkey",
)
FUNCTIONS = ("sprintctl_request_delivery_append_only",)
EXPECTED_CATALOG_FINGERPRINT = "329e20935208b4c53b1f09dc018dd8a054fb11969210ee67c5ee6e900494e8ea"


def install(cur):
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)", (list(RELATIONS),)
    )
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema33 refuses pre-existing request delivery relations")
    cur.execute(DDL)


def inspect_catalog(cur):
    from .agent_inbox_schema import inspect_relations
    return inspect_relations(cur, relations=RELATIONS, functions=FUNCTIONS,
                             table_total=len(TABLES), expected=EXPECTED_CATALOG_FINGERPRINT,
                             trigger_tables=TABLES)


DDL = r"""
CREATE TABLE operator_request_delivery (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    resolution_revision integer NOT NULL CHECK (resolution_revision > 0),
    work_item_id bigint NOT NULL,
    work_status_token text NOT NULL,
    recipient_instance_id text NOT NULL,
    binding_id text NOT NULL,
    binding_revision integer NOT NULL CHECK (binding_revision > 0),
    sender_principal_id text NOT NULL CHECK (length(sender_principal_id) > 0),
    admission_issuer text NOT NULL CHECK (length(admission_issuer) > 0),
    admission_jti text NOT NULL CHECK (length(admission_jti) > 0),
    message_id text NOT NULL,
    envelope_digest text NOT NULL CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, message_id),
    CONSTRAINT uq_request_delivery_logical UNIQUE
      (repo_id, workspace_id, request_id, resolution_revision, work_item_id,
       work_status_token, recipient_instance_id, binding_id, binding_revision),
    CONSTRAINT uq_request_delivery_jti UNIQUE (admission_issuer, admission_jti)
);

CREATE TABLE operator_request_delivery_key (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    sender_principal_id text NOT NULL CHECK (length(sender_principal_id) > 0),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    message_id text NOT NULL,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, sender_principal_id, idempotency_key),
    FOREIGN KEY (repo_id, workspace_id, message_id)
      REFERENCES operator_request_delivery(repo_id, workspace_id, message_id) ON DELETE RESTRICT
);

CREATE FUNCTION sprintctl_request_delivery_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514';
END; $$;
CREATE TRIGGER request_delivery_append_only BEFORE UPDATE OR DELETE ON operator_request_delivery
    FOR EACH ROW EXECUTE FUNCTION sprintctl_request_delivery_append_only();
CREATE TRIGGER request_delivery_key_append_only BEFORE UPDATE OR DELETE ON operator_request_delivery_key
    FOR EACH ROW EXECUTE FUNCTION sprintctl_request_delivery_append_only();
CREATE TRIGGER request_delivery_no_truncate BEFORE TRUNCATE ON operator_request_delivery
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_request_delivery_append_only();
CREATE TRIGGER request_delivery_key_no_truncate BEFORE TRUNCATE ON operator_request_delivery_key
    FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_request_delivery_append_only();
"""
