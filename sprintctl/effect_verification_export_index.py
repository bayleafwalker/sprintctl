"""Explicit schema24 proposal-associated verification lookup index; no runtime DDL."""
from __future__ import annotations

INDEX_NAME = "idx_evidence_item_effect_verification_subject"
DDL = ("CREATE INDEX " + INDEX_NAME + " ON evidence_item USING gin (claims pg_catalog.jsonb_path_ops) "
       "WHERE kind='protected-artifact-verification'")


def inspect_catalog(cur):
    cur.execute(
        "SELECT am.amname, t.relname, i.indisunique, i.indisvalid, i.indisready, "
        "i.indnatts, i.indnkeyatts, pg_get_expr(i.indpred,i.indrelid) AS predicate, "
        "pg_get_expr(i.indexprs,i.indrelid) AS expressions, "
        "ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,seq) "
        "LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum ORDER BY seq) AS keys, "
        "ARRAY(SELECT ns.nspname || '.' || o.opcname FROM unnest(i.indclass) WITH ORDINALITY k(oid,seq) "
        "JOIN pg_opclass o ON o.oid=k.oid JOIN pg_namespace ns ON ns.oid=o.opcnamespace ORDER BY seq) AS opclasses "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_class t ON t.oid=i.indrelid "
        "JOIN pg_namespace tn ON tn.oid=t.relnamespace JOIN pg_am am ON am.oid=c.relam "
        "WHERE n.nspname=current_schema() AND tn.nspname=current_schema() AND c.relname=%s",
        (INDEX_NAME,),
    )
    row = cur.fetchone()
    if row is None:
        # A same-named non-index relation is also a collision.
        cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND c.relname=%s", (INDEX_NAME,))
        return {"available": False, "complete": cur.fetchone() is None}
    values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
    expected = ("gin", "evidence_item", False, True, True, 1, 1,
                "(kind = 'protected-artifact-verification'::text)", None,
                ["claims"], ["pg_catalog.jsonb_path_ops"])
    return {"available": True, "complete": values == expected}


def install(cur):
    from .pg_migrations import RemoteSchemaMigrationError
    state = inspect_catalog(cur)
    if not state["complete"]:
        raise RemoteSchemaMigrationError("schema24 verification index collision")
    if not state["available"]:
        cur.execute(DDL)
    state = inspect_catalog(cur)
    if not state["available"] or not state["complete"]:
        raise RemoteSchemaMigrationError("schema24 verification index invalid")
