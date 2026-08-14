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
from sprintctl.db import InvalidTransition
from sprintctl.pg_testing import (
    assert_disposable_connection,
    cleanup_test_repositories,
    new_test_repo_id,
    new_test_repo_uuid,
    write_cleanup_report,
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
def pg_test_scope():
    """Register test scopes, then clean and report them from one finalizer."""
    if _SKIP:
        pytest.skip(_SKIP_REASON)
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_ids: set[str] = set()

    def register(label: str = "scope", *, canonical_uuid: bool = False) -> str:
        repo_id = new_test_repo_uuid() if canonical_uuid else new_test_repo_id(label)
        repo_ids.add(repo_id)
        return repo_id

    try:
        yield register
    finally:
        report_path = os.environ.get("SPRINTCTL_TEST_PG_CLEANUP_REPORT")
        try:
            report = cleanup_test_repositories(conn, repo_ids)
        except Exception as exc:
            if report_path:
                write_cleanup_report(
                    report_path,
                    {
                        "schema_version": "sprintctl-pg-cleanup/v1",
                        "cleanup_completed": False,
                        "error_type": type(exc).__name__,
                        "repo_ids": sorted(repo_ids),
                    },
                )
            raise
        else:
            if report_path:
                write_cleanup_report(report_path, report)
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


def _receipt_bytes(store, sprint_id, boundary_event_id, **overrides):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    receipt = {
        "schema_version": "capability-receipt/v1",
        "id": receipt_id,
        "project": store.repo_id,
        "status": "draft",
        "publication": "private",
        "boundary": {
            "kind": "sprint-close",
            "ref": {
                "kind": "sprint-event",
                "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                "revision": f"event:{boundary_event_id}",
            },
        },
    }
    receipt.update(overrides)
    return json.dumps(receipt, sort_keys=True).encode()


def _receipt_payload(store, receipt_bytes):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    return {
        "project": store.repo_id,
        "receipt_id": receipt_id,
        "receipt_path": (
            f"/projects/dev/_artifacts/{store.repo_id}/capability/receipts/"
            f"{receipt_id}.json"
        ),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }


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
