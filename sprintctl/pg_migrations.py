"""Deployment-owned PostgreSQL schema migrations and compatibility probes.

Normal sprintctl runtime startup calls :func:`require_compatible_schema`,
which issues SELECT statements only.  DDL is reachable through
:func:`migrate_schema` for a deployment migration job, or through the explicit
operator compatibility mode retained for rollout rollback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


WORK_API_VERSION = "sprintctl-work/v1"
CURRENT_SCHEMA_VERSION = 2
MINIMUM_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
MAXIMUM_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
STARTUP_MODE_ENV = "SPRINTCTL_REMOTE_SCHEMA_MODE"
READ_ONLY_STARTUP_MODE = "read-only"
OPERATOR_MIGRATE_STARTUP_MODE = "operator-migrate"

# One global schema is shared by every repository tenant.  The two-key lock is
# stable across releases and must be acquired before inspecting migration state.
SCHEMA_MIGRATION_LOCK_KEYS = (0x53505249, 0x4E544354)  # "SPRI", "NTCT"


class RemoteSchemaCompatibilityError(RuntimeError):
    """The remote schema cannot safely serve this work API."""


class RemoteSchemaMigrationError(RuntimeError):
    """The migration ledger is malformed or newer than this migrator."""


@dataclass(frozen=True, slots=True)
class SchemaState:
    version: int | None
    row_count: int


def _value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _read_schema_state(cur: Any) -> SchemaState:
    cur.execute("SELECT to_regclass('schema_version') AS relation")
    relation = _value(cur.fetchone(), "relation")
    if relation is None:
        return SchemaState(version=None, row_count=0)
    cur.execute(
        "SELECT COUNT(*) AS row_count, MIN(version) AS minimum_version, "
        "MAX(version) AS maximum_version FROM schema_version"
    )
    row = cur.fetchone()
    row_count = int(_value(row, "row_count") or 0)
    minimum = _value(row, "minimum_version", 1)
    maximum = _value(row, "maximum_version", 2)
    if row_count != 1 or minimum is None or maximum is None or minimum != maximum:
        raise RemoteSchemaMigrationError(
            "remote schema_version must contain exactly one unambiguous version row"
        )
    try:
        version = int(minimum)
    except (TypeError, ValueError) as exc:
        raise RemoteSchemaMigrationError(
            "remote schema_version contains a non-integer version"
        ) from exc
    return SchemaState(version=version, row_count=row_count)


def compatibility_handshake(store: Any) -> dict[str, Any]:
    """Return read-only work API/schema compatibility data for service startup."""
    with store.conn.cursor() as cur:
        state = _read_schema_state(cur)
    compatible = (
        state.version is not None
        and MINIMUM_SCHEMA_VERSION <= state.version <= MAXIMUM_SCHEMA_VERSION
    )
    if state.version is None:
        reason = "schema-version-table-missing"
    elif state.version < MINIMUM_SCHEMA_VERSION:
        reason = "schema-too-old"
    elif state.version > MAXIMUM_SCHEMA_VERSION:
        reason = "schema-too-new"
    else:
        reason = None
    return {
        "schema_version": "sprintctl-work-compatibility/v1",
        "work_api_version": WORK_API_VERSION,
        "remote_schema": {
            "actual": state.version,
            "minimum": MINIMUM_SCHEMA_VERSION,
            "maximum": MAXIMUM_SCHEMA_VERSION,
        },
        "compatible": compatible,
        "reason": reason,
    }


def require_compatible_schema(store: Any) -> dict[str, Any]:
    """Fail closed unless the remote schema is supported; never run DDL."""
    handshake = compatibility_handshake(store)
    if handshake["compatible"]:
        return handshake
    schema = handshake["remote_schema"]
    actual = "missing" if schema["actual"] is None else str(schema["actual"])
    raise RemoteSchemaCompatibilityError(
        "remote work schema is incompatible "
        f"(actual={actual}, supported={schema['minimum']}..{schema['maximum']}, "
        f"reason={handshake['reason']}); an authorized migration job must run first"
    )


def migrate_schema(store: Any) -> dict[str, Any]:
    """Apply idempotent schema migrations under the global transaction lock."""
    # Import lazily so importing the runtime compatibility probe never imports
    # or executes the DDL-bearing PostgreSQL backend module.
    from . import pg as _pg

    applied: list[int] = []
    starting_version: int | None = None
    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                SCHEMA_MIGRATION_LOCK_KEYS,
            )
            state = _read_schema_state(cur)
            starting_version = state.version
            if state.version is None:
                cur.execute(_pg.PG_DDL)
                state = _read_schema_state(cur)
            if state.version is None or state.version < 1:
                raise RemoteSchemaMigrationError(
                    "remote schema migration could not establish version 1"
                )
            if state.version > CURRENT_SCHEMA_VERSION:
                raise RemoteSchemaMigrationError(
                    "remote schema is newer than this migration package "
                    f"(actual={state.version}, current={CURRENT_SCHEMA_VERSION})"
                )
            if state.version < 2:
                # The legacy version-1 bootstrap accumulated idempotent DDL
                # without advancing its ledger.  Reapplying the canonical DDL
                # plus the bounded upgrade statements normalizes every known
                # v1 deployment before advancing the version exactly once.
                cur.execute(_pg.PG_DDL)
                _pg._apply_schema_version_2(cur)
                cur.execute("UPDATE schema_version SET version = %s", (2,))
                applied.append(2)
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise

    handshake = compatibility_handshake(store)
    # Release the read-only probe transaction before returning the connection
    # to a long-lived service or migration runner.
    store.conn.rollback()
    if not handshake["compatible"]:
        raise RemoteSchemaMigrationError(
            "migration committed but the resulting schema is not compatible"
        )
    return {
        "schema_version": "sprintctl-remote-migration-result/v1",
        "from_version": starting_version,
        "to_version": CURRENT_SCHEMA_VERSION,
        "applied_versions": applied,
        "compatibility": handshake,
    }


def startup_schema_handshake(
    store: Any,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Apply the explicit rollout mode, then require a compatible schema."""
    mode = environ.get(STARTUP_MODE_ENV, READ_ONLY_STARTUP_MODE)
    if mode == OPERATOR_MIGRATE_STARTUP_MODE:
        migrate_schema(store)
    elif mode != READ_ONLY_STARTUP_MODE:
        raise RemoteSchemaCompatibilityError(
            f"invalid {STARTUP_MODE_ENV}={mode!r}; expected "
            f"{READ_ONLY_STARTUP_MODE!r} or {OPERATOR_MIGRATE_STARTUP_MODE!r}"
        )
    handshake = require_compatible_schema(store)
    store.conn.rollback()
    return handshake
