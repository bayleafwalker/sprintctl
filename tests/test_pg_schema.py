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
    required = {"schema_version", "sprint", "track", "work_item", "event", "claim", "ref", "dep"}
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
