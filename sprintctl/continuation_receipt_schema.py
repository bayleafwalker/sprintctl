"""Deployment-only schema 30: immutable principal continuation receipts."""
from __future__ import annotations

from typing import Any

from .pg_migrations import RemoteSchemaMigrationError

TABLES = ("operator_continuation_receipt",)
RELATIONS = TABLES + ("operator_continuation_receipt_pkey",)
FUNCTIONS = ("sprintctl_continuation_receipt_append_only",)
EXPECTED_CATALOG_FINGERPRINT = "6e7bcbfd6f1467fa1af4e75c8dfc1586c381fb28567209d21b1d57075782ac86"


def install(cur: Any) -> None:
    cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=ANY(%s)", (list(RELATIONS),))
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema30 refuses pre-existing receipt relations")
    cur.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema() AND p.proname=ANY(%s)", (list(FUNCTIONS),))
    if cur.fetchone() is not None:
        raise RemoteSchemaMigrationError("schema30 refuses pre-existing receipt guard")
    cur.execute(DDL)


def inspect_catalog(cur: Any) -> dict[str, Any]:
    from .agent_inbox_schema import inspect_relations
    return inspect_relations(cur, relations=RELATIONS, functions=FUNCTIONS,
                             table_total=1, expected=EXPECTED_CATALOG_FINGERPRINT,
                             trigger_tables=TABLES)


DDL = """
CREATE TABLE operator_continuation_receipt (
    repo_id text NOT NULL,
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    resolution_revision integer NOT NULL CHECK (resolution_revision > 0),
    work_item_id bigint NOT NULL,
    work_revision text NOT NULL,
    principal_id text NOT NULL,
    material_revision integer NOT NULL CHECK (material_revision > 0),
    packet_digest text NOT NULL,
    sessions jsonb NOT NULL CHECK (jsonb_typeof(sessions) = 'array' AND jsonb_array_length(sessions) > 0),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (repo_id, workspace_id, request_id, resolution_revision, work_item_id, work_revision),
    FOREIGN KEY (repo_id, workspace_id, request_id) REFERENCES operator_request(repo_id, workspace_id, request_id) ON DELETE RESTRICT
);
CREATE FUNCTION sprintctl_continuation_receipt_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514'; END; $$;
CREATE TRIGGER sprintctl_continuation_receipt_append_only BEFORE UPDATE OR DELETE ON operator_continuation_receipt
FOR EACH ROW EXECUTE FUNCTION sprintctl_continuation_receipt_append_only();
CREATE TRIGGER sprintctl_continuation_receipt_no_truncate BEFORE TRUNCATE ON operator_continuation_receipt
FOR EACH STATEMENT EXECUTE FUNCTION sprintctl_continuation_receipt_append_only();
"""
