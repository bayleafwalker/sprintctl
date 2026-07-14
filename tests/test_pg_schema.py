"""
Validate the pg DDL without a real postgres connection.

These tests parse the DDL string and check that required tables, columns,
constraints, and indexes are present — purely by inspection of PG_DDL.
"""

import re

from sprintctl.pg import PG_DDL


def _tables_in_ddl(ddl: str) -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl))


def _indexes_in_ddl(ddl: str) -> set[str]:
    return set(re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", ddl))


def test_ddl_contains_required_tables():
    tables = _tables_in_ddl(PG_DDL)
    required = {
        "schema_version", "sprint", "track", "work_item", "event", "claim", "ref", "dep",
        "ingest_stream", "ingest_record",
    }
    assert required <= tables, f"Missing tables: {required - tables}"


def test_all_core_tables_have_repo_id():
    for table in ("sprint", "track", "work_item", "event", "claim", "ref", "dep"):
        pattern = rf"CREATE TABLE IF NOT EXISTS {table}\s*\("
        match = re.search(pattern, PG_DDL)
        assert match, f"Table {table} not found"
        # Find the column block: from CREATE TABLE through next CREATE TABLE or end
        start = match.start()
        rest = PG_DDL[start:]
        end = rest.find("CREATE TABLE IF NOT EXISTS", 10)
        block = rest[:end] if end != -1 else rest
        assert "repo_id" in block, f"Table {table} missing repo_id column"


def test_sprint_table_has_expected_columns():
    # Find the sprint table block
    m = re.search(r"CREATE TABLE IF NOT EXISTS sprint\s*\((.*?)\);", PG_DDL, re.DOTALL)
    assert m, "sprint table not found"
    block = m.group(1)
    for col in ("repo_id", "id", "name", "goal", "status", "kind", "created_at", "aggregate_uuid"):
        assert col in block, f"sprint table missing column: {col}"


def test_portable_aggregate_uuids_are_unique_in_pg_schema():
    for table in ("sprint", "work_item"):
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\);", PG_DDL, re.DOTALL)
        assert m, f"{table} DDL missing"
        assert re.search(r"aggregate_uuid\s+uuid\s+NOT NULL UNIQUE", m.group(1))


def test_claim_table_has_token_and_identity_columns():
    m = re.search(r"CREATE TABLE IF NOT EXISTS claim\s*\((.*?)\);", PG_DDL, re.DOTALL)
    assert m, "claim table not found"
    block = m.group(1)
    for col in ("claim_token", "instance_id", "runtime_session_id", "hostname", "pid"):
        assert col in block, f"claim table missing column: {col}"


def test_event_table_uses_jsonb_payload():
    m = re.search(r"CREATE TABLE IF NOT EXISTS event\s*\((.*?)\);", PG_DDL, re.DOTALL)
    assert m, "event table not found"
    block = m.group(1)
    assert "jsonb" in block, "event.payload should be jsonb"


def test_required_indexes_present():
    indexes = _indexes_in_ddl(PG_DDL)
    required = {
        "idx_claim_token",
        "idx_sprint_repo_archived",
        "idx_event_repo_sprint_type_ts",
        "idx_work_item_repo_sprint_status",
        "idx_track_repo_sprint",
        "idx_claim_repo_item_expires",
        "idx_ingest_record_repo_offset",
    }
    assert required <= indexes, f"Missing indexes: {required - indexes}"


def test_claim_token_index_is_partial():
    # idx_claim_token should be a partial unique index (WHERE claim_token IS NOT NULL)
    m = re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_token(.*?)(?:;|\n\n)", PG_DDL, re.DOTALL)
    assert m, "idx_claim_token not found"
    block = m.group(0)
    assert "WHERE" in block, "idx_claim_token should be a partial index"
    assert "NOT NULL" in block, "idx_claim_token should filter NULL tokens"


def test_foreign_keys_reference_repo_id():
    # All FKs in core tables should include repo_id for tenant safety
    fk_blocks = re.findall(r"FOREIGN KEY\s*\([^)]+\)\s*REFERENCES\s*\w+\s*\([^)]+\)", PG_DDL)
    for fk in fk_blocks:
        assert "repo_id" in fk, f"FK without repo_id: {fk}"


def test_dep_table_has_unique_constraint():
    m = re.search(r"CREATE TABLE IF NOT EXISTS dep\s*\((.*?)\);", PG_DDL, re.DOTALL)
    assert m, "dep table not found"
    block = m.group(1)
    # Should have unique(repo_id, item_id, blocked_item_id)
    assert "item_id" in block
    assert "blocked_item_id" in block


def test_ddl_is_idempotent_if_not_exists():
    # Every CREATE TABLE uses IF NOT EXISTS
    create_tables = re.findall(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)", PG_DDL)
    for t in create_tables:
        assert f"CREATE TABLE IF NOT EXISTS {t}" in PG_DDL, f"Table {t} missing IF NOT EXISTS"


def test_ingest_ledger_scopes_deduplication_and_cursor_to_the_repo():
    stream = re.search(r"CREATE TABLE IF NOT EXISTS ingest_stream\s*\((.*?)\);", PG_DDL, re.DOTALL)
    record = re.search(r"CREATE TABLE IF NOT EXISTS ingest_record\s*\((.*?)\);", PG_DDL, re.DOTALL)
    assert stream and record
    assert "PRIMARY KEY (repo_id, origin_stream_id)" in stream.group(1)
    assert "GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in record.group(1)
    assert "UNIQUE (repo_id, origin_stream_id, origin_seq)" in record.group(1)
    assert "UNIQUE (repo_id, event_id)" in record.group(1)
    assert "producer_created_at timestamptz NOT NULL" in record.group(1)


def test_server_guard_covers_every_repo_scoped_table():
    assert "CREATE OR REPLACE FUNCTION sprintctl_guard_integration_test_scope()" in PG_DDL
    assert "NEW.repo_id LIKE 'itest-%'" in PG_DDL
    assert "current_database() NOT LIKE 'sprintctl\\_test\\_%'" in PG_DDL
    assert "current_user NOT LIKE 'sprintctl\\_test\\_%'" in PG_DDL
    assert "sprintctl:disposable-integration-test" in PG_DDL
    for table in (
        "sprint", "track", "work_item", "event", "claim", "ref", "dep",
        "ingest_stream", "ingest_record",
    ):
        assert f"'{table}'" in PG_DDL
