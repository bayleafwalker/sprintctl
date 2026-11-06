"""Disposable PostgreSQL checks for schema33 recipient delivery storage."""
import uuid

import pytest

from sprintctl import pg, recipient_delivery_schema
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection

pytestmark = PG_MARKS


def test_schema33_catalog_is_exact_and_append_only_guards_are_installed():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema = "delivery_" + uuid.uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        store = pg.PgStore(conn, "recipient-delivery-test")
        # Install every prerequisite and schema33 through the real migrator.
        pg.init_db(store)
        with conn.cursor() as cur:
            catalog = recipient_delivery_schema.inspect_catalog(cur)
            assert catalog["available"], catalog
            cur.execute("SELECT pg_get_functiondef(p.oid) AS definition FROM pg_proc p "
                        "WHERE p.proname='sprintctl_request_delivery_append_only'")
            assert "append-only" in cur.fetchone()["definition"]
            cur.execute("SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s)", ([
                "request_delivery_append_only", "request_delivery_key_append_only",
                "request_delivery_no_truncate", "request_delivery_key_no_truncate",
            ],))
            assert {row["tgname"] for row in cur.fetchall()} == {
                "request_delivery_append_only", "request_delivery_key_append_only",
                "request_delivery_no_truncate", "request_delivery_key_no_truncate"}
        for table in ("operator_request_delivery_key",
                      "operator_request_delivery, operator_request_delivery_key"):
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(f"TRUNCATE {table}")
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()
