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
CURRENT_SCHEMA_VERSION = 6
MINIMUM_SCHEMA_VERSION = 5
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


MAINTENANCE_BRIDGE_RELATIONS = (
    "maintenance_capability",
    "maintenance_capability_receipt",
    "maintenance_capability_recovery",
    "sprintctl_schema_capability",
)


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


def _read_maintenance_bridge(cur: Any) -> dict[str, Any]:
    """Fingerprint the additive maintenance storage using read-only probes."""
    cur.execute(
        "SELECT "
        + ", ".join(
            f"to_regclass('{relation}') IS NOT NULL AS {relation}"
            for relation in MAINTENANCE_BRIDGE_RELATIONS
        )
    )
    row = cur.fetchone()
    present = {
        relation: bool(_value(row, relation, index))
        for index, relation in enumerate(MAINTENANCE_BRIDGE_RELATIONS)
    }
    cur.execute(
        "SELECT COUNT(*) AS trigger_count FROM pg_trigger "
        "WHERE tgname IN ('maintenance_capability_receipt_immutable', "
        "'maintenance_capability_recovery_immutable') AND NOT tgisinternal"
    )
    trigger_count = int(_value(cur.fetchone(), "trigger_count") or 0)
    marker_version = None
    if present["sprintctl_schema_capability"]:
        cur.execute(
            "SELECT version FROM sprintctl_schema_capability "
            "WHERE capability = 'maintenance-storage'"
        )
        marker_version = _value(cur.fetchone(), "version")
    count = sum(present.values())
    fully_present = (
        count == len(MAINTENANCE_BRIDGE_RELATIONS)
        and trigger_count == 2
        and marker_version == 1
    )
    return {
        "schema_version": "sprintctl-maintenance-storage/v1",
        "available": fully_present,
        "complete": (count == 0 and trigger_count == 0) or (
            fully_present
        ),
        "relations": present,
        "immutable_trigger_count": trigger_count,
        "marker_version": marker_version,
    }


def _is_legacy_schema6_maintenance_layout(layout: Mapping[str, Any]) -> bool:
    """Recognize only the exact 0.2.15 schema-6 layout lacking our marker."""
    relations = layout["relations"]
    return (
        all(relations[name] for name in MAINTENANCE_BRIDGE_RELATIONS[:3])
        and not relations["sprintctl_schema_capability"]
        and layout["immutable_trigger_count"] == 2
        and layout["marker_version"] is None
    )


def compatibility_handshake(store: Any) -> dict[str, Any]:
    """Return read-only work API/schema compatibility data for service startup."""
    with store.conn.cursor() as cur:
        state = _read_schema_state(cur)
        maintenance = _read_maintenance_bridge(cur)
    compatible = (
        state.version is not None
        and MINIMUM_SCHEMA_VERSION <= state.version <= MAXIMUM_SCHEMA_VERSION
        and maintenance["available"]
        and maintenance["complete"]
    )
    if state.version is None:
        reason = "schema-version-table-missing"
    elif state.version < MINIMUM_SCHEMA_VERSION:
        reason = "schema-too-old"
    elif state.version > MAXIMUM_SCHEMA_VERSION:
        reason = "schema-too-new"
    elif not maintenance["complete"]:
        reason = "maintenance-storage-partial"
    elif not maintenance["available"]:
        reason = "maintenance-storage-missing"
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
        "capabilities": {
            "maintenance_storage": maintenance,
            "repository_ingest_cursor": {
                "schema_version": "sprintctl-repository-ingest-cursor/v1",
                "scope": "repository",
                "contiguous": True,
            }
        },
    }


def stage_schema5_maintenance_bridge(store: Any) -> dict[str, Any]:
    """Install the additive v5 maintenance store without advancing the ledger.

    This is deployment-owned DDL.  Runtime startup never calls this function.
    """
    from . import pg as _pg

    try:
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                SCHEMA_MIGRATION_LOCK_KEYS,
            )
            state = _read_schema_state(cur)
            if state.version != 5:
                raise RemoteSchemaMigrationError(
                    "maintenance bridge staging requires exact schema version 5"
                )
            before = _read_maintenance_bridge(cur)
            if not before["complete"]:
                raise RemoteSchemaMigrationError(
                    "refusing to repair a partial maintenance storage bridge"
                )
            if not before["available"]:
                _pg._apply_schema_version_6(cur)
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    handshake = compatibility_handshake(store)
    store.conn.rollback()
    if not handshake["compatible"]:
        raise RemoteSchemaMigrationError("staged maintenance bridge is incomplete")
    return {
        "schema_version": "sprintctl-schema5-maintenance-bridge-result/v1",
        "remote_schema": 5,
        "installed": not before["available"],
        "compatibility": handshake,
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
            if state.version == 6:
                layout = _read_maintenance_bridge(cur)
                if _is_legacy_schema6_maintenance_layout(layout):
                    # 0.2.15 installed the complete maintenance store before
                    # the explicit capability marker existed. The deployment
                    # migrator may add only that mechanically recognized marker;
                    # runtime startup still never repairs schema state.
                    _pg._apply_schema_version_6(cur)
                elif not layout["complete"]:
                    raise RemoteSchemaMigrationError(
                        "schema 6 has an unrecognized partial maintenance layout"
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
                state = SchemaState(version=2, row_count=1)
            if state.version < 3:
                _pg._apply_schema_version_3(cur)
                cur.execute("UPDATE schema_version SET version = %s", (3,))
                applied.append(3)
            if state.version < 4:
                _pg._apply_schema_version_4(cur)
                cur.execute("UPDATE schema_version SET version = %s", (4,))
                applied.append(4)
                state = SchemaState(version=4, row_count=1)
            if state.version < 5:
                _pg._apply_schema_version_5(cur)
                cur.execute("UPDATE schema_version SET version = %s", (5,))
                applied.append(5)
                state = SchemaState(version=5, row_count=1)
            if state.version < 6:
                _pg._apply_schema_version_6(cur)
                cur.execute("UPDATE schema_version SET version = %s", (6,))
                applied.append(6)
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
    store.remote_schema_version = handshake["remote_schema"]["actual"]
    store.conn.rollback()
    return handshake
