from datetime import datetime, timedelta, timezone
from uuid import uuid4
import threading
import time

import pytest

from sprintctl import db
from sprintctl.maintenance_capability import MaintenanceCapabilityError, SQLiteMaintenanceCapabilityStore
from tests.test_maintenance_capability import AT, CAPABILITY_ID, envelope, shifted_envelope


@pytest.mark.parametrize("resource", [False, True])
def test_prepare_clock_after_sqlite_writer_wait(conn, db_path, resource):
    value = shifted_envelope(timedelta(hours=-1, seconds=3))
    state = {}
    waiting = threading.Event()

    def prepare():
        worker_conn = db.get_connection(db_path)
        worker_conn.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            SQLiteMaintenanceCapabilityStore(worker_conn).prepare(
                capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=value,
                actor="operator", at=AT, resource=resource)
            state["outcome"] = "accepted"
        except MaintenanceCapabilityError as error:
            state["outcome"] = str(error)
        finally:
            worker_conn.close()

    conn.execute("BEGIN IMMEDIATE")
    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        assert waiting.wait(2)
        clock = SQLiteMaintenanceCapabilityStore(conn)
        expires = datetime.fromisoformat(value["window"]["expires_at"].replace("Z", "+00:00"))
        assert clock._decision_time() < expires
        assert worker.is_alive() and "outcome" not in state
        limit = time.monotonic() + 4
        while clock._decision_time() < expires and time.monotonic() < limit:
            time.sleep(0.01)
        assert clock._decision_time() >= expires
    finally:
        conn.commit()
        worker.join(5)
    assert not worker.is_alive()
    assert state["outcome"] == "expired envelopes cannot be prepared"
    for table in ("maintenance_capability", "maintenance_capability_receipt", "maintenance_resource"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("resource", [False, True])
def test_expired_prepare_uses_database_clock_despite_stale_at(conn, resource):
    store = SQLiteMaintenanceCapabilityStore(conn)
    with pytest.raises(MaintenanceCapabilityError, match="expired envelopes cannot be prepared"):
        store.prepare(
            capability_id=CAPABILITY_ID, request_id=str(uuid4()),
            envelope=shifted_envelope(timedelta(hours=-3)), actor="operator", at=AT,
            resource=resource,
        )
    assert conn.execute("SELECT COUNT(*) FROM maintenance_capability").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM maintenance_capability_receipt").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM maintenance_resource").fetchone()[0] == 0


@pytest.mark.parametrize("resource", [False, True])
def test_future_audit_time_does_not_refuse_valid_prepare(conn, resource):
    store = SQLiteMaintenanceCapabilityStore(conn)
    future_at = "2099-01-01T00:00:00Z"
    result = store.prepare(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=envelope(),
        actor="operator", at=future_at, resource=resource,
    )
    assert result["state"] == "prepared"
    assert store.get(CAPABILITY_ID)["created_at"] == future_at


def test_exact_prepare_replay_after_expiry_is_unchanged(conn, monkeypatch):
    store = SQLiteMaintenanceCapabilityStore(conn)
    arguments = dict(capability_id=CAPABILITY_ID, request_id=str(uuid4()),
                     envelope=envelope(), actor="operator", at=AT, resource=True)
    original = store.prepare(**arguments)
    after_expiry = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(store, "_decision_time", lambda: after_expiry)
    replay = store.prepare(**{**arguments, "at": "2099-01-01T00:00:00Z"})
    assert replay == {**original, "duplicate": True}
    assert conn.execute("SELECT COUNT(*) FROM maintenance_capability_receipt").fetchone()[0] == 1


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_prepare_expiry_boundary_uses_database_instant(conn, monkeypatch, offset):
    store = SQLiteMaintenanceCapabilityStore(conn)
    value = envelope()
    expires = datetime.fromisoformat(value["window"]["expires_at"].replace("Z", "+00:00"))
    monkeypatch.setattr(store, "_decision_time", lambda: expires + timedelta(seconds=offset))
    arguments = dict(capability_id=CAPABILITY_ID, request_id=str(uuid4()),
                     envelope=value, actor="operator", at=AT)
    if offset < 0:
        assert store.prepare(**arguments)["state"] == "prepared"
    else:
        with pytest.raises(MaintenanceCapabilityError, match="expired envelopes cannot be prepared"):
            store.prepare(**arguments)
        assert store.get(CAPABILITY_ID) is None
