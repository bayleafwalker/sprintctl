"""Deployment-only schema31: admit one new read-only delegation operation.

The old schema27 admission remains frozen. No table, ledger, lease field,
credential or existing delegation record is created or rewritten here.
"""
from __future__ import annotations

from . import delegation_schema
from .pg_migrations import RemoteSchemaMigrationError

EXPECTED_CATALOG_FINGERPRINT = '7a2e3feb4e7b0eaab04c3d633a57206566810b9782b87f7fd08828a510cab8b0'
DDL = """
ALTER TABLE work_run_delegation DROP CONSTRAINT work_run_delegation_operations_check;
ALTER TABLE work_run_delegation ADD CONSTRAINT work_run_delegation_operations_check
CHECK (jsonb_typeof(operations)='array' AND jsonb_array_length(operations)>0
 AND operations <@ '["work.run.resolve-v1","work.run.predecessor-context-v1",
 "work.session-note.write-v1","work.evidence.append-v1","work.lease.currentness-v1"]'::jsonb);
"""


def install(cur):
    before=delegation_schema.inspect_catalog(cur)
    if not before['available']:
        raise RemoteSchemaMigrationError('schema31 requires the exact released schema27 delegation storage')
    cur.execute(DDL)


def inspect_catalog(cur):
    from .agent_inbox_schema import inspect_relations
    return inspect_relations(cur,relations=delegation_schema.RELATIONS,functions=delegation_schema.FUNCTIONS,
        table_total=len(delegation_schema.TABLES),expected=EXPECTED_CATALOG_FINGERPRINT,
        trigger_tables=delegation_schema.TABLES)
