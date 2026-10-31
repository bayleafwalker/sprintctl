"""
Shared fixtures, helpers, and marker for the tests/pg/ PostgreSQL integration test package.

Requires a disposable PostgreSQL database owned by a dedicated, unprivileged
test role. See docs/guides/postgres-integration-tests.md for the contract.
Set SPRINTCTL_TEST_PG_URL to run:

    SPRINTCTL_TEST_PG_URL=postgresql://localhost/testdb pytest tests/pg/ -v

All tests are automatically skipped when the variable is unset or psycopg is unavailable.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import threading
import time
import uuid

import pytest

_PG_URL: str | None = os.environ.get("SPRINTCTL_TEST_PG_URL")

try:
    import psycopg
    from psycopg.rows import dict_row
    _PSYCOPG_AVAILABLE = True
except ImportError:
    # Split modules import these names during collection even without the
    # optional remote extra. PG_MARKS skips them before runtime use.
    psycopg = None
    dict_row = None
    _PSYCOPG_AVAILABLE = False

_SKIP = not _PG_URL or not _PSYCOPG_AVAILABLE
_SKIP_REASON = (
    "SPRINTCTL_TEST_PG_URL not set"
    if not _PG_URL
    else "psycopg not installed — run: pip install 'sprintctl[remote]'"
)

PG_MARKS = [
    pytest.mark.pg,
    pytest.mark.skipif(_SKIP, reason=_SKIP_REASON),
]

# Safe unconditional imports: pg.py handles missing psycopg gracefully.
from sprintctl import authority, contracts, db, maintain, observations, pg, projection, sync
from sprintctl import outbox
from sprintctl import pg_migrations
from sprintctl.cli import cli
from sprintctl.db import InvalidTransition, ReservationConflict
from sprintctl.pg_testing import (
    assert_disposable_connection,
    FixtureCleanup,
    new_test_repo_id,
    new_test_repo_uuid,
)
from sprintctl.maintenance_capability import (
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
)
from tests.test_maintenance_capability import (
    CAPABILITY_ID, AT, envelope, shifted_envelope, _stamp,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_test_scope(request):
    """Register test scopes, then clean and report them from one finalizer."""
    if _SKIP:
        pytest.skip(_SKIP_REASON)
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    evidence = FixtureCleanup("pg_test_scope", request.node.nodeid)
    repo_ids: set[str] = set()

    def register(label: str = "scope", *, canonical_uuid: bool = False) -> str:
        repo_id = new_test_repo_uuid() if canonical_uuid else new_test_repo_id(label)
        repo_ids.add(repo_id)
        return repo_id

    try:
        yield register
    finally:
        try:
            evidence.cleanup(conn, repo_ids)
        finally:
            conn.close()


@pytest.fixture(scope="module")
def store(pg_test_scope):
    """Create the module store only after the disposable-target preflight."""
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = pg_test_scope("module")
    s = pg.PgStore(
        conn=conn,
        repo_id=repo_id,
        authority_repo_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{repo_id}")),
    )
    pg.init_db(s)
    try:
        yield s
    finally:
        conn.close()


def drop_schema30(cur) -> None:
    """Remove schema31's one new allowed delegation operation for old fixtures."""
    cur.execute("ALTER TABLE work_run_delegation DROP CONSTRAINT work_run_delegation_operations_check")
    cur.execute("""ALTER TABLE work_run_delegation ADD CONSTRAINT work_run_delegation_operations_check
        CHECK(jsonb_typeof(operations)='array' AND jsonb_array_length(operations)>0
        AND operations <@ '["work.run.resolve-v1","work.run.predecessor-context-v1",
        "work.session-note.write-v1","work.evidence.append-v1"]'::jsonb)""")


def drop_schema29(cur, *, reset_currentness: bool = True) -> None:
    if reset_currentness:
        drop_schema30(cur)
    cur.execute("DROP TABLE IF EXISTS operator_continuation_receipt")
    cur.execute("DROP FUNCTION IF EXISTS sprintctl_continuation_receipt_append_only()")
    """Remove schema 29 lease-basis bindings before rebuilding an older fixture."""
    cur.execute("DROP TRIGGER IF EXISTS work_lease_basis_insert_guard ON work_lease")
    cur.execute("DROP TRIGGER IF EXISTS work_decision_lease_basis_guard ON work_decision")
    cur.execute("DROP FUNCTION IF EXISTS sprintctl_work_lease_basis_insert_guard()")
    cur.execute("DROP FUNCTION IF EXISTS sprintctl_work_decision_lease_basis_guard()")
    cur.execute(
        "ALTER TABLE work_lease DROP COLUMN IF EXISTS basis_item_revision, "
        "DROP COLUMN IF EXISTS basis_release_digest"
    )


def drop_schema26(cur) -> None:
    """Remove schema 26+ owner additions before rebuilding an older fixture."""
    # The complete delegation relation is removed below. Reinstating its old
    # CHECK first is both unnecessary and invalid if another fixture issued a
    # currentness-only scope, which the historical schema cannot represent.
    drop_schema29(cur, reset_currentness=False)
    # Remove newer additive layers first when reconstructing a schema-25
    # fixture for upgrade coverage.
    cur.execute("DROP TABLE IF EXISTS operator_request_event, operator_request_dependant, operator_request")
    # The schema-28 append-only trigger function is not owned by the tables
    # after they are dropped. Remove it when reconstructing an older schema so
    # the forward migration tests exercise a clean historical catalog.
    cur.execute("DROP FUNCTION IF EXISTS sprintctl_operator_request_event_append_only()")
    cur.execute("DROP TABLE IF EXISTS work_run_delegation")
    cur.execute("DROP FUNCTION IF EXISTS sprintctl_run_delegation_guard()")
    cur.execute("DROP TRIGGER sprintctl_run_binding_immutable ON run")
    cur.execute("DROP TRIGGER sprintctl_run_predecessor_append_only ON run_predecessor")
    cur.execute("DROP TRIGGER sprintctl_run_predecessor_no_truncate ON run_predecessor")
    cur.execute("DROP TABLE work_approval")
    cur.execute("DROP FUNCTION sprintctl_approval_append_only(), sprintctl_run_binding_immutable()")


def drop_schema25(cur) -> None:
    """Remove schema 25's agent inbox storage (and schema 26 above it), for
    fixtures that rebuild an older schema in place before migrating it
    forward again."""
    drop_schema26(cur)
    cur.execute("DROP TABLE agent_message_receipt, agent_message, agent_inbox_counter, "
                "agent_observation, agent_declaration_revision, agent_instance")
    cur.execute("DROP FUNCTION sprintctl_agent_append_only(), sprintctl_agent_instance_guard()")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _anchor_capability_window_to_db_clock(conn, repo_id, capability_id) -> None:
    """Align a prepared capability's window with the database clock.

    Claim admission filters live capabilities on the database clock
    (``expires_at > now()``), while capability transitions evaluate the window
    against the caller-supplied ``at``. In production those track each other;
    the fixture envelope instead pins ``at`` to a fixed AT and its window to a
    fixed instant, so once wall-clock time passes that instant the two
    arbitration paths disagree and repo-wide mutual exclusion silently stops
    being exercised. Anchoring the stored window to the database's own clock
    keeps these races meaningful at any future date.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE maintenance_capability SET expires_at = now() + interval '1 hour' "
            "WHERE repo_id = %s AND capability_id = %s",
            (repo_id, capability_id),
        )
    conn.commit()


def _authority_repo_uuid(store) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{store.repo_id}"))


def _append_authority_command(
    conn,
    store,
    *,
    record_type,
    aggregate_type,
    basis_revision,
    payload,
    aggregate_uuid=None,
    claim_id=None,
    actor="authority-test",
):
    refs = {
        "repo_id": _authority_repo_uuid(store),
        "aggregate_type": aggregate_type,
    }
    if aggregate_uuid is not None:
        refs["aggregate_uuid"] = aggregate_uuid
    if claim_id is not None:
        refs["claim_id"] = claim_id
    command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()),
        record_type=record_type,
        schema_version="1",
        actor=actor,
        authored_at="2026-07-14T18:00:00Z",
        refs=refs,
        payload=payload,
        basis_revision=basis_revision,
        correlation_id=str(uuid.uuid4()),
    )
    return outbox.append_authority_command(conn, command)


@pytest.fixture
def sprint_id(store):
    return pg.create_sprint(store, f"S-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")


@pytest.fixture
def track_id(store, sprint_id):
    return pg.get_or_create_track(store, sprint_id, "eng")


@pytest.fixture
def work_item_id(store, sprint_id, track_id):
    return pg.create_work_item(store, sprint_id, track_id, f"Item-{_uid()}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
