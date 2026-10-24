"""Disposable PostgreSQL checks for the schema-28 request owner."""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest

from sprintctl import operator_request as contract
from sprintctl import operator_request_pg as storage
from sprintctl import pg
from sprintctl.pg import PgStore
from sprintctl.pg_testing import (
    assert_disposable_connection,
    cleanup_test_repositories,
    new_test_repo_id,
)

_PG_URL = os.environ.get("SPRINTCTL_TEST_PG_URL")
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only without the PG extra
    psycopg = None
    dict_row = None

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        not _PG_URL or psycopg is None,
        reason="disposable SPRINTCTL_TEST_PG_URL and psycopg are required",
    ),
]


def _packet(reason: str = "The checked operator action remains outstanding.") -> dict:
    return {
        "schema": "operator-request/v1",
        "kind": "perform",
        "required_outcome": "The operator runs the checked preflight.",
        "reason": reason,
        "execution": {
            "host": "ops.example.invalid",
            "directory": "/srv/service",
            "intended_identity": "deployment-operator",
            "target": "staging/service-a",
            "inputs": [{"name": "operator session", "acquire_from": "approved terminal login"}],
        },
        "procedure": {
            "version": "1.0.0",
            "steps": [{
                "step_id": "check",
                "instruction": "Run the documented read-only preflight command.",
                "expected_result": "The expected release digest is returned.",
                "preconditions": ["The operator is on the approved host."],
                "verification": "Compare the digest with the pinned release.",
                "partial_progress": "Record the observed step before continuing.",
                "recovery": "Stop and refresh the request if the digest differs.",
            }],
        },
        "blocker": {
            "condition": "A checked operator-only preflight remains outstanding.",
            "checked_at": "2026-10-24T10:00:00Z",
            "fresh_until": "2026-10-24T11:00:00Z",
            "source_revisions": [{"source": "release", "revision": "sha256:abc"}],
        },
    }


@pytest.fixture(scope="module")
def request_owner():
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    repo_id = new_test_repo_id("operator-request")
    store = PgStore(
        conn=connection,
        repo_id=repo_id,
        authority_repo_uuid=str(uuid4()),
        connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row),
    )
    pg.init_db(store)
    sprint_id = pg.create_sprint(store, "operator-request-db-tests", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "request-tests")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Operator request fixture")
    try:
        yield store, item_id
    finally:
        connection.commit()
        cleanup_test_repositories(connection, [repo_id])
        connection.close()


def _publish(store: PgStore, item_id: int, request_id: str, *, sessions=("session-a",)) -> dict:
    arguments = contract.publish_arguments({
        "request_id": request_id,
        "packet": _packet(),
        "dependants": [
            {"work_item_id": item_id, "session_id": session, "runtime_id": "codex"}
            for session in sessions
        ],
        "idempotency_key": f"publish-{request_id}",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        result = storage.publish(cur, store, "principal-a", "workspace-a", arguments)
    store.conn.commit()
    return result


def _read(store: PgStore, request_id: str) -> dict:
    with store.conn.cursor() as cur:
        result = storage.get(cur, store, "workspace-a", request_id)
    store.conn.commit()
    return result


def test_publish_is_durable_scoped_and_safe_to_retry_after_lost_response(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRS"
    args = contract.publish_arguments({
        "request_id": request_id,
        "packet": _packet(),
        "dependants": [
            {"work_item_id": item_id, "session_id": "session-a", "runtime_id": "codex"},
            {"work_item_id": item_id, "session_id": "session-b", "runtime_id": "claude"},
        ],
        "idempotency_key": "publish-lost-response",
    })

    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.publish(cur, store, "principal-a", "workspace-a", args) == {
            "request_id": request_id, "revision": 1, "replayed": False,
        }

    retry_connection = store.connection_factory()
    retry_store = PgStore(retry_connection, store.repo_id, store.authority_repo_uuid)
    try:
        with retry_connection.transaction(), retry_connection.cursor() as cur:
            assert storage.publish(
                cur, retry_store, "principal-a", "workspace-a", args
            ) == {"request_id": request_id, "revision": 1, "replayed": True}
        snapshot = _read(retry_store, request_id)
        assert [item["session_id"] for item in snapshot["dependants"]] == [
            "session-a", "session-b",
        ]
        with retry_connection.cursor() as cur:
            assert storage.events(cur, retry_store, "workspace-a", {
                "request_id": request_id, "after_revision": 0, "limit": 100,
            })["events"][0]["action"] == "published"
            with pytest.raises(contract.RequestRefused, match="no such request"):
                storage.get(cur, retry_store, "workspace-b", request_id)
            cur.execute(
                "SELECT count(*) AS count FROM operator_request_event "
                "WHERE repo_id=%s AND workspace_id='workspace-a' AND request_id=%s",
                (store.repo_id, request_id),
            )
            assert cur.fetchone()["count"] == 1
        retry_connection.commit()

        conflicting = contract.publish_arguments(args | {"packet": _packet("changed")})
        with pytest.raises(contract.RequestRefused, match="already bound"), retry_connection.transaction(), retry_connection.cursor() as cur:
            storage.publish(cur, retry_store, "principal-a", "workspace-a", conflicting)
    finally:
        retry_connection.close()


def test_progress_and_performed_report_do_not_resolve_or_change_work_item(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRT"
    _publish(store, item_id, request_id)
    before = pg.get_work_item(store, item_id)["status"]

    progress = contract.progress_arguments({
        "request_id": request_id, "expected_revision": 1,
        "work_item_id": item_id, "session_id": "session-a", "step_id": "check",
        "status": "completed", "note": "The preflight returned the pinned digest.",
        "idempotency_key": "progress-check-1",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.progress(cur, store, "principal-a", "workspace-a", progress)["revision"] == 2

    report = contract.report_performed_arguments({
        "request_id": request_id, "expected_revision": 2,
        "outcome": "performed", "note": "The operator reports the preflight ran.",
        "idempotency_key": "performed-check-1",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.report_performed(
            cur, store, "principal-a", "workspace-a", report
        )["revision"] == 3
    snapshot = _read(store, request_id)

    assert snapshot["state"] == "reported-performed"
    assert snapshot["revision"] == 3
    assert pg.get_work_item(store, item_id)["status"] == before


def test_withdrawing_one_shared_dependant_keeps_request_open_until_last(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRY"
    _publish(store, item_id, request_id, sessions=("session-a", "session-b"))

    first = contract.dependant_withdraw_arguments({
        "request_id": request_id, "expected_revision": 1,
        "work_item_id": item_id, "session_id": "session-a",
        "idempotency_key": "withdraw-first-shared-dependent",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.withdraw_dependant(
            cur, store, "principal-a", "workspace-a", first
        )["revision"] == 2

    snapshot = _read(store, request_id)
    assert snapshot["state"] == "open"
    assert [(item["session_id"], item["state"]) for item in snapshot["dependants"]] == [
        ("session-a", "withdrawn"), ("session-b", "active"),
    ]

    last = contract.dependant_withdraw_arguments({
        "request_id": request_id, "expected_revision": 2,
        "work_item_id": item_id, "session_id": "session-b",
        "idempotency_key": "withdraw-last-shared-dependent",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.withdraw_dependant(
            cur, store, "principal-a", "workspace-a", last
        )["revision"] == 3

    assert _read(store, request_id)["state"] == "withdrawn"


def test_supersession_transfers_only_live_dependants_and_keeps_history(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQSA"
    replacement_id = "req_01JBC3D4E5F6G7H8J9K0MNPQSB"
    _publish(store, item_id, request_id, sessions=("session-a", "session-b"))

    withdrawn = contract.dependant_withdraw_arguments({
        "request_id": request_id, "expected_revision": 1,
        "work_item_id": item_id, "session_id": "session-a",
        "idempotency_key": "withdraw-before-supersession",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.withdraw_dependant(cur, store, "principal-a", "workspace-a", withdrawn)

    replacement_packet = _packet("The target changed; use this refreshed instruction.")
    replacement_packet["execution"]["target"] = "staging/service-b"
    replacement_packet["blocker"]["source_revisions"] = [
        {"source": "release", "revision": "sha256:def"},
    ]
    supersede = contract.supersede_arguments({
        "request_id": request_id, "expected_revision": 2,
        "replacement_request_id": replacement_id, "packet": replacement_packet,
        "idempotency_key": "supersede-after-target-change",
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.supersede(
            cur, store, "principal-a", "workspace-a", supersede
        )["revision"] == 3

    old = _read(store, request_id)
    replacement = _read(store, replacement_id)
    assert old["state"] == "superseded"
    assert [(item["session_id"], item["state"]) for item in old["dependants"]] == [
        ("session-a", "withdrawn"), ("session-b", "withdrawn"),
    ]
    assert replacement["state"] == "open"
    assert replacement["packet"]["execution"]["target"] == "staging/service-b"
    assert [(item["session_id"], item["state"]) for item in replacement["dependants"]] == [
        ("session-b", "active"),
    ]
    with store.conn.cursor() as cur:
        old_events = storage.events(cur, store, "workspace-a", {
            "request_id": request_id, "after_revision": 0, "limit": 100,
        })["events"]
    store.conn.commit()
    assert [event["action"] for event in old_events] == [
        "published", "dependant-withdrawn", "superseded",
    ]


def test_concurrent_revisions_allow_one_writer_and_reject_the_stale_writer(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRV"
    _publish(store, item_id, request_id)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def revise(reason: str) -> None:
        connection = store.connection_factory()
        contender = PgStore(connection, store.repo_id, store.authority_repo_uuid)
        arguments = contract.revise_arguments({
            "request_id": request_id, "expected_revision": 1,
            "packet": _packet(reason),
            "idempotency_key": f"revise-{reason.replace(' ', '-')}",
        })
        barrier.wait(timeout=5)
        try:
            with connection.transaction(), connection.cursor() as cur:
                result = storage.revise(cur, contender, "principal-a", "workspace-a", arguments)
            outcomes.append(("ok", result["revision"]))
        except contract.RequestRefused as exc:
            outcomes.append(("refused", exc.code))
        finally:
            connection.close()

    threads = [
        threading.Thread(target=revise, args=("first concurrent revision",)),
        threading.Thread(target=revise, args=("second concurrent revision",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == [("ok", 2), ("refused", "revision-conflict")]
    assert _read(store, request_id)["revision"] == 2


def test_request_events_reject_update_delete_and_truncate(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRW"
    _publish(store, item_id, request_id)

    for statement in (
        "UPDATE operator_request_event SET action='rewritten' WHERE repo_id=%s AND request_id=%s",
        "DELETE FROM operator_request_event WHERE repo_id=%s AND request_id=%s",
        "TRUNCATE operator_request_event",
    ):
        with pytest.raises(psycopg.Error) as refused, store.conn.transaction(), store.conn.cursor() as cur:
            if "%s" in statement:
                cur.execute(statement, (store.repo_id, request_id))
            else:
                cur.execute(statement)
        assert refused.value.sqlstate == "23514"
    assert _read(store, request_id)["revision"] == 1


def test_caller_rollback_removes_request_and_event_together(request_owner):
    store, item_id = request_owner
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRX"
    args = contract.publish_arguments({
        "request_id": request_id, "packet": _packet(),
        "dependants": [{"work_item_id": item_id, "session_id": "rollback", "runtime_id": "codex"}],
        "idempotency_key": "rollback-publish",
    })
    with pytest.raises(RuntimeError, match="discard caller transaction"), store.conn.transaction(), store.conn.cursor() as cur:
        storage.publish(cur, store, "principal-a", "workspace-a", args)
        raise RuntimeError("discard caller transaction")

    with pytest.raises(contract.RequestRefused, match="no such request"), store.conn.cursor() as cur:
        storage.get(cur, store, "workspace-a", request_id)
    store.conn.commit()
