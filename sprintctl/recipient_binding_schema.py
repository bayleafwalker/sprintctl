"""Deployment-only schema 32: recipient self-binding storage."""
from __future__ import annotations

from .pg_migrations import RemoteSchemaMigrationError

TABLES = ("operator_request_recipient_binding",)
RELATIONS = TABLES + ("operator_request_recipient_binding_pkey",)
EXPECTED_CATALOG_FINGERPRINT = "b797d15f81812732957344df4a96ecf2203dcbf6f8379535a06afbcd0e02f94c"


def install(cur):
    cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)", (list(RELATIONS),))
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema32 refuses pre-existing recipient binding relations")
    cur.execute(DDL)


def inspect_catalog(cur):
    from .agent_inbox_schema import inspect_relations
    return inspect_relations(cur, relations=RELATIONS, functions=(), table_total=1,
                             expected=EXPECTED_CATALOG_FINGERPRINT, trigger_tables=TABLES)


DDL = """
CREATE TABLE operator_request_recipient_binding (
    binding_id text NOT NULL,
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    work_item_id bigint NOT NULL,
    principal_id text NOT NULL CHECK (length(principal_id) > 0),
    instance_id text NOT NULL,
    gateway_issuer text NOT NULL CHECK (length(gateway_issuer) > 0),
    subject text NOT NULL CHECK (length(subject) > 0),
    epoch bigint NOT NULL CHECK (epoch >= 0 AND epoch <= 9007199254740991),
    client_id text NOT NULL CHECK (length(client_id) > 0),
    grant_id text NOT NULL CHECK (length(grant_id) > 0),
    resource text NOT NULL CHECK (length(resource) > 0),
    token_scopes jsonb NOT NULL CHECK (jsonb_typeof(token_scopes) = 'array'),
    effective_authority jsonb NOT NULL CHECK (jsonb_typeof(effective_authority) = 'array'),
    revision integer NOT NULL CHECK (revision > 0),
    created_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    PRIMARY KEY (binding_id),
    FOREIGN KEY (repo_id, workspace_id, request_id)
        REFERENCES operator_request(repo_id, workspace_id, request_id) ON DELETE RESTRICT,
    FOREIGN KEY (repo_id, workspace_id, instance_id)
        REFERENCES agent_instance(repo_id, workspace_id, instance_id) ON DELETE RESTRICT
);
"""
