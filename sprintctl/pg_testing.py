"""Safety helpers for destructive PostgreSQL integration tests.

The integration suite is intentionally stricter than the production client:
it only accepts a dedicated, non-privileged role that owns a database whose
name and server-side comment explicitly identify it as disposable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import UUID, uuid4


TEST_REPO_PREFIX = "itest-"
TEST_ROLE_PREFIX = "sprintctl_test_"
TEST_DATABASE_PREFIX = "sprintctl_test_"
DISPOSABLE_DATABASE_COMMENT = "sprintctl:disposable-integration-test"
REPO_TABLES = (
    "maintenance_capability_recovery",
    "maintenance_capability_receipt",
    "maintenance_capability",
    "terminal_recovery_audit",
    "terminal_recovery_ledger",
    "authority_decision",
    "ingest_record",
    "ingest_stream",
    "ingest_repo_cursor",
    "dep",
    "ref",
    "claim",
    "event",
    "work_item",
    "track",
    "sprint",
)


class UnsafePostgresTestTarget(RuntimeError):
    """The configured PostgreSQL target is not unquestionably disposable."""


@dataclass(frozen=True)
class PostgresTestIdentity:
    database_name: str
    role_name: str
    database_owner: str
    database_comment: str | None
    role_superuser: bool
    role_create_db: bool
    role_create_role: bool
    role_replication: bool
    role_bypass_rls: bool


def validate_disposable_identity(identity: PostgresTestIdentity) -> None:
    """Reject identities that do not meet the dedicated test-role contract."""
    violations: list[str] = []
    if not _has_test_name(identity.database_name, TEST_DATABASE_PREFIX):
        violations.append(
            "database name must be 'sprintctl_test' or start with "
            f"{TEST_DATABASE_PREFIX!r}"
        )
    if not _has_test_name(identity.role_name, TEST_ROLE_PREFIX):
        violations.append(
            "role name must be 'sprintctl_test' or start with "
            f"{TEST_ROLE_PREFIX!r}"
        )
    if identity.database_owner != identity.role_name:
        violations.append("the test role must own the test database")
    if identity.database_comment != DISPOSABLE_DATABASE_COMMENT:
        violations.append(
            "database comment must be " + repr(DISPOSABLE_DATABASE_COMMENT)
        )
    elevated = [
        name
        for name, enabled in (
            ("SUPERUSER", identity.role_superuser),
            ("CREATEDB", identity.role_create_db),
            ("CREATEROLE", identity.role_create_role),
            ("REPLICATION", identity.role_replication),
            ("BYPASSRLS", identity.role_bypass_rls),
        )
        if enabled
    ]
    if elevated:
        violations.append("test role has elevated attributes: " + ", ".join(elevated))
    if violations:
        raise UnsafePostgresTestTarget(
            "refusing destructive PostgreSQL integration tests: " + "; ".join(violations)
        )


def _has_test_name(value: str, prefix: str) -> bool:
    return value == prefix.removesuffix("_") or value.startswith(prefix)


def inspect_disposable_identity(conn: Any) -> PostgresTestIdentity:
    """Read role and database safety facts from the connected PostgreSQL server."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                current_database() AS database_name,
                current_user AS role_name,
                pg_get_userbyid(database_row.datdba) AS database_owner,
                shobj_description(database_row.oid, 'pg_database') AS database_comment,
                role_row.rolsuper AS role_superuser,
                role_row.rolcreatedb AS role_create_db,
                role_row.rolcreaterole AS role_create_role,
                role_row.rolreplication AS role_replication,
                role_row.rolbypassrls AS role_bypass_rls
            FROM pg_database AS database_row
            JOIN pg_roles AS role_row ON role_row.rolname = current_user
            WHERE database_row.datname = current_database()
            """
        )
        row = cur.fetchone()
    if row is None:
        raise UnsafePostgresTestTarget(
            "refusing destructive PostgreSQL integration tests: server identity is unavailable"
        )
    return PostgresTestIdentity(**dict(row))


def assert_disposable_connection(conn: Any) -> PostgresTestIdentity:
    """Validate server-side identity before any schema or test-data mutation."""
    identity = inspect_disposable_identity(conn)
    validate_disposable_identity(identity)
    return identity


def new_test_repo_id(label: str = "scope") -> str:
    """Mint a recognizable repository scope that the server guard protects."""
    normalized = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "scope"
    return f"{TEST_REPO_PREFIX}{normalized}-{uuid4().hex[:12]}"


def new_test_repo_uuid() -> str:
    """Return a canonical tenant id for contracts that intentionally require UUIDs."""
    return str(uuid4())


def cleanup_test_repositories(conn: Any, repo_ids: Iterable[str]) -> dict[str, Any]:
    """Delete registered test scopes and prove that no rows remain.

    The connection is revalidated immediately before deletion so this helper
    cannot be reused as an arbitrary production cleanup primitive.
    """
    identity = assert_disposable_connection(conn)
    scopes = sorted(set(repo_ids))
    def safe_scope(repo_id: str) -> bool:
        if repo_id.startswith(TEST_REPO_PREFIX):
            return True
        try:
            UUID(repo_id)
        except ValueError:
            return False
        return True
    invalid = [repo_id for repo_id in scopes if not safe_scope(repo_id)]
    if invalid:
        raise UnsafePostgresTestTarget(
            "refusing cleanup of non-test repository scopes: " + ", ".join(invalid)
        )

    deleted_rows = {table: 0 for table in REPO_TABLES}
    remaining_rows = {table: 0 for table in REPO_TABLES}
    try:
        with conn.cursor() as cur:
            for table in REPO_TABLES:
                cur.execute(
                    f"DELETE FROM {table} WHERE repo_id = ANY(%s)",  # noqa: S608
                    (scopes,),
                )
                deleted_rows[table] = cur.rowcount
            for table in REPO_TABLES:
                cur.execute(
                    f"SELECT count(*) AS count FROM {table} WHERE repo_id = ANY(%s)",  # noqa: S608
                    (scopes,),
                )
                remaining_rows[table] = int(cur.fetchone()["count"])
        if any(remaining_rows.values()):
            raise RuntimeError("PostgreSQL integration-test cleanup left repository rows behind")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "schema_version": "sprintctl-pg-cleanup/v1",
        "cleanup_completed": True,
        "database": identity.database_name,
        "role": identity.role_name,
        "repo_ids": scopes,
        "deleted_rows": deleted_rows,
        "remaining_rows": remaining_rows,
    }


def write_cleanup_report(path: str | Path, report: dict[str, Any]) -> None:
    """Persist non-secret cleanup evidence for CI artifact collection."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def identity_as_dict(identity: PostgresTestIdentity) -> dict[str, Any]:
    """Return a JSON-friendly identity for diagnostics without connection data."""
    return asdict(identity)
