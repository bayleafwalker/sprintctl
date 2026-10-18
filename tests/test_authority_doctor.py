"""A preflight observes producer state; it never repairs or initializes it."""

import json
from dataclasses import asdict
import os
import sqlite3
import stat
import sys
from uuid import uuid4

import pytest

from sprintctl import authority_config, outbox
from sprintctl import authority_doctor as doctor
from sprintctl.cli import cli
from tests.test_served_routes import _configure_served_repo


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served requires Python 3.12+")
def test_missing_outbox_is_unknown_and_never_initialized(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    import sprintctl.served as served
    monkeypatch.setattr(served, "identity_current", lambda *a, **k: {"actor": "owner"})
    monkeypatch.setattr(served, "read_records", lambda *a, **k: {"records": [], "stream_high_water": {}})
    result = runner.invoke(cli, ["authority", "doctor", "--served", "--check-sequence", "--json"])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["checks"]["local_sequence"]["status"] == "unknown"
    assert not (tmp_path / ".sprintctl" / "authority-command-outbox.db").exists()


def make_producer(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    conn = outbox.open_outbox(paths.outbox_path)
    record = outbox.append_observation(conn, event_type="work.completed", actor="owner",
                                      payload={"private": "never-render-this"})
    conn.close()
    paths.terminal_dir.mkdir(mode=0o700)
    paths.terminal_dir.chmod(0o700)
    return paths, record


@pytest.fixture
def producer(tmp_path):
    return make_producer(tmp_path)


def test_snapshot_reads_committed_metadata_without_initializing(producer, monkeypatch):
    paths, record = producer
    monkeypatch.setattr(outbox, "open_outbox", lambda _: pytest.fail("initializing open"))
    before = paths.outbox_path.read_bytes()
    snapshot = doctor.local_snapshot(paths.outbox_path)
    assert snapshot["status"] == "pass"
    assert snapshot["stream_id"] == record.origin_stream_id
    assert snapshot["next_origin_seq"] == 2
    assert snapshot["record_count"] == 1
    assert paths.outbox_path.read_bytes() == before


@pytest.mark.parametrize("mode", [0o2700, 0o1700, 0o755])
def test_permission_check_sees_special_bits_without_repair(producer, mode):
    paths, _ = producer
    paths.terminal_dir.chmod(mode)
    assert doctor.terminal_permissions(paths.terminal_dir)["status"] == "fail"
    assert stat.S_IMODE(paths.terminal_dir.stat().st_mode) == mode


def test_runtime_setgid_policy_is_preserved(producer):
    paths, _ = producer
    paths.terminal_dir.chmod(0o2700)
    authority_config._require_private(paths.terminal_dir, directory=True)


@pytest.mark.parametrize("mutation", [
    "UPDATE outbox_stream SET next_origin_seq=8",
    "UPDATE outbox_stream SET origin_stream_id='00000000-0000-4000-8000-000000000001'",
])
def test_sequence_and_stream_disagreements_refuse(producer, mutation):
    paths, _ = producer
    with sqlite3.connect(paths.outbox_path) as conn:
        conn.execute(mutation)
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "fail"


def test_symlink_outbox_refuses(producer):
    paths, _ = producer
    renamed = paths.outbox_path.with_suffix(".saved")
    paths.outbox_path.rename(renamed)
    paths.outbox_path.symlink_to(renamed)
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "fail"


@pytest.mark.parametrize("suffix", ["quarantined.db", "rollover.json"])
def test_rolled_over_current_stream_fails(producer, suffix):
    paths, record = producer
    (paths.state_dir / f"authority-command-outbox.{record.origin_stream_id}.{suffix}").touch()
    assert doctor.quarantine_check(paths, doctor.local_snapshot(paths.outbox_path))["status"] == "fail"


@pytest.mark.parametrize("value", [
    {"outcome": "accepted", "event_id": "wrong"},
    {"outcome": "quarantined-divergent-stream"},
    {"outcome": "invented"},
])
def test_current_receipts_are_checked_without_content_disclosure(producer, value):
    paths, record = producer
    value = {"event_id": record.event_id, "secret": "never-render-this", **value}
    (paths.terminal_dir / f"{record.event_id}.json").write_text(json.dumps(value))
    (paths.terminal_dir / f"{record.event_id}.json").chmod(0o600)
    result = doctor.quarantine_check(paths, doctor.local_snapshot(paths.outbox_path))
    assert result["status"] == "fail"
    assert "never-render-this" not in json.dumps(result)


def test_receipt_limit_remains_unknown(producer, monkeypatch):
    paths, _ = producer
    monkeypatch.setattr(doctor, "MAX_RECEIPTS", 0)
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "unknown"


def _owner(monkeypatch, producer, *, cursor=None, reservation=None, repo_id="repo"):
    paths, record = producer
    monkeypatch.setattr(doctor.served, "identity_current", lambda *a, **k: {"repo_id": repo_id, "actor": "owner"})
    monkeypatch.setattr(doctor.served, "read_records", lambda *a, **k: {
        "repo_id": repo_id, "records": [{"ingest_offset": 1, "record": asdict(record)}],
        "stream_high_water": {record.origin_stream_id: cursor} if cursor is not None else {},
    })
    calls = []
    def operation(profile, operation, **kwargs):
        calls.append(operation)
        return {"repo_id": repo_id, "reservation": reservation}
    monkeypatch.setattr(doctor.served, "reservation_operation", operation)
    return calls


@pytest.mark.parametrize("cursor", [None, True, -1, "1", 1.0])
def test_missing_or_untyped_cursor_never_means_zero(producer, monkeypatch, cursor):
    calls = _owner(monkeypatch, producer, cursor=cursor)
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert report["checks"]["server_sequence"]["status"] == "unknown"
    assert report["status"] == "unknown"
    assert calls == []


@pytest.mark.parametrize("cursor", [0, 2])
def test_unaligned_cursor_fails_without_probe(producer, monkeypatch, cursor):
    calls = _owner(monkeypatch, producer, cursor=cursor)
    report = doctor.diagnose(producer[0], None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["checks"]["server_sequence"]["status"] == "fail"
    assert calls == []


def test_matching_reads_do_not_prove_write_authorization(producer, monkeypatch):
    calls = _owner(monkeypatch, producer, cursor=1)
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert report["checks"]["server_sequence"]["status"] == "pass"
    assert report["checks"]["write_probe"]["status"] == "unknown"
    assert report["authorizes_command_submission"] is False
    assert calls == []


@pytest.mark.parametrize("changes", [{"session_id": "other"}, {"actor": "other"}, {"state": "released"}, {"id": 8}])
def test_probe_requires_exact_active_actor_session_reservation(producer, monkeypatch, changes):
    reservation = {"id": 7, "actor": "owner", "session_id": "session", "state": "active", **changes}
    calls = _owner(monkeypatch, producer, cursor=1, reservation=reservation)
    report = doctor.diagnose(producer[0], None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["checks"]["write_probe"]["status"] == "fail"
    assert calls == ["work.read.reservation"]


def test_explicit_probe_reports_only_the_operations_actually_exercised(producer, monkeypatch):
    calls = _owner(monkeypatch, producer, cursor=1, reservation={
        "id": 7, "actor": "owner", "session_id": "session", "state": "active",
    })
    report = doctor.diagnose(producer[0], None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["status"] == "pass"
    assert calls == ["work.read.reservation", "work.reservation.touch"]
    assert report["operations_exercised"] == ["work.identity.current", "work.read.records", *calls]
    assert report["authorizes_command_submission"] is False
    assert "never-render-this" not in json.dumps(report)


def test_read_failure_is_redacted_and_never_runs_write(producer, monkeypatch):
    calls = _owner(monkeypatch, producer, cursor=1)
    def denied(*args, **kwargs):
        raise RuntimeError("private-token-never-render")
    monkeypatch.setattr(doctor.served, "identity_current", denied)
    report = doctor.diagnose(producer[0], None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["status"] == "unknown"
    assert "private-token" not in json.dumps(report)
    assert calls == []


@pytest.mark.parametrize("lost_response", [False, True])
def test_reassignment_or_response_loss_never_proves_probe_binding(producer, monkeypatch, lost_response):
    _owner(monkeypatch, producer, cursor=1)
    calls = []
    def operation(profile, operation, **kwargs):
        calls.append(operation)
        row = {"id": 7, "actor": "owner", "session_id": "session", "state": "active"}
        if operation == "work.reservation.touch":
            if lost_response:
                raise TimeoutError("private response loss detail")
            row["actor"] = "reassigned"
        return {"repo_id": "repo", "reservation": row}
    monkeypatch.setattr(doctor.served, "reservation_operation", operation)
    report = doctor.diagnose(producer[0], None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["checks"]["write_probe"]["status"] == "unknown"
    assert report["checks"]["write_probe"]["write_attempted"] is True
    assert report["checks"]["write_probe"]["reservation_id"] == 7
    assert calls == ["work.read.reservation", "work.reservation.touch"]
    assert "private response" not in json.dumps(report)


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "absent-from-served-ledger"])
def test_valid_terminal_receipt_does_not_quarantine_current_stream(producer, outcome):
    paths, record = producer
    authority_config.mark_terminal_authority_decision(paths, event_id=record.event_id, outcome=outcome)
    assert doctor.quarantine_check(paths, doctor.local_snapshot(paths.outbox_path))["status"] == "pass"


def test_receipt_cumulative_budget_never_reports_clear(producer, monkeypatch):
    paths, first = producer
    writer = outbox.open_outbox(paths.outbox_path)
    try:
        second = outbox.append_observation(writer, event_type="work.completed", actor="owner", payload={})
    finally:
        writer.close()
    for record in (first, second):
        authority_config.mark_terminal_authority_decision(paths, event_id=record.event_id, outcome="accepted")
    snapshot = doctor.local_snapshot(paths.outbox_path)
    first_size = (paths.terminal_dir / f"{first.event_id}.json").stat().st_size
    monkeypatch.setattr(doctor, "MAX_TOTAL_RECORD_BYTES", first_size + 1)
    report = doctor.quarantine_check(paths, snapshot)
    assert report["status"] == "unknown"


def test_mutation_during_server_read_is_unknown(producer, monkeypatch):
    paths, record = producer
    calls = _owner(monkeypatch, producer, cursor=1)
    def read(*args, **kwargs):
        writer = outbox.open_outbox(paths.outbox_path)
        try:
            outbox.append_observation(writer, event_type="work.completed", actor="owner", payload={})
        finally:
            writer.close()
        return {"repo_id": "repo", "records": [], "stream_high_water": {record.origin_stream_id: 1}}
    monkeypatch.setattr(doctor.served, "read_records", read)
    report = doctor.diagnose(paths, None, repo_id="repo", probe_reservation_id=7, session_id="session")
    assert report["checks"]["server_sequence"]["reason"] == "local-stream-changed-during-read"
    assert calls == []


def test_independent_writer_commit_between_metadata_queries_keeps_one_snapshot(producer, monkeypatch):
    paths, _ = producer
    writer = outbox.open_outbox(paths.outbox_path)
    original = sqlite3.connect
    history = []
    class Reader:
        def __init__(self, conn):
            object.__setattr__(self, "conn", conn)
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def __setattr__(self, name, value):
            setattr(self.conn, name, value)
        def execute(self, sql, *args):
            if sql.startswith("SELECT COUNT(*) n,MIN(origin_seq)"):
                history.append("reader-stream-read")
                outbox.append_observation(writer, event_type="work.completed", actor="owner", payload={})
                history.append("writer-committed")
            return self.conn.execute(sql, *args)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Reader(original(*a, **k)))
    try:
        report = doctor.local_snapshot(paths.outbox_path)
        history.append("reader-completed")
        assert report["status"] == "pass"
        assert report["record_count"] == 1
        assert report["next_origin_seq"] == 2
        assert writer.execute("SELECT next_origin_seq FROM outbox_stream").fetchone()[0] == 3
        assert history == ["reader-stream-read", "writer-committed", "reader-completed"]
    finally:
        writer.close()


def test_equal_cursor_with_missing_records_stays_unknown(producer, monkeypatch):
    _owner(monkeypatch, producer, cursor=1)
    record = producer[1]
    monkeypatch.setattr(doctor.served, "read_records", lambda *a, **k: {
        "repo_id": "repo", "records": [], "stream_high_water": {record.origin_stream_id: 1},
    })
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert report["checks"]["server_sequence"]["reason"] == "server-committed-prefix-incomplete"


def test_equal_cursor_with_divergent_record_fails(producer, monkeypatch):
    _owner(monkeypatch, producer, cursor=1)
    record = producer[1]
    raw = {**asdict(record), "actor": "different"}
    monkeypatch.setattr(doctor.served, "read_records", lambda *a, **k: {
        "repo_id": "repo", "records": [{"ingest_offset": 1, "record": raw}],
        "stream_high_water": {record.origin_stream_id: 1},
    })
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert report["checks"]["server_sequence"]["reason"] == "server-client-record-divergence"


@pytest.mark.parametrize("change_cursor", [False, True])
def test_owner_pages_are_complete_ordered_and_cursor_stable(producer, monkeypatch, change_cursor):
    _owner(monkeypatch, producer, cursor=1)
    record = producer[1]
    other_stream = str(uuid4())
    first = [{"ingest_offset": n, "record": {**asdict(record), "origin_stream_id": other_stream,
              "event_id": str(uuid4()), "origin_seq": n}} for n in range(1, 251)]
    offsets = []
    def read(*args, after_offset, limit, **kwargs):
        offsets.append(after_offset)
        return {"repo_id": "repo", "records": first if after_offset == 0 else [
            {"ingest_offset": 251, "record": asdict(record)}],
            "stream_high_water": {record.origin_stream_id: 2 if after_offset and change_cursor else 1}}
    monkeypatch.setattr(doctor.served, "read_records", read)
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert offsets == [0, 250]
    assert report["checks"]["server_sequence"]["status"] == ("unknown" if change_cursor else "pass")


def test_remote_inspection_limit_is_unknown_not_clear(producer, monkeypatch):
    _owner(monkeypatch, producer, cursor=1)
    monkeypatch.setattr(doctor, "MAX_REMOTE_ROWS", 0)
    report = doctor.diagnose(producer[0], None, repo_id="repo")
    assert report["checks"]["server_sequence"]["reason"] == "server-record-inspection-limit"


def test_corrupt_nullable_stream_row_fails(tmp_path):
    path = tmp_path / "legacy.db"
    stream = str(uuid4())
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE outbox_stream(origin_stream_id,next_origin_seq)")
        conn.execute("INSERT INTO outbox_stream VALUES(?,2)", (stream,))
        conn.execute("CREATE TABLE outbox_record(origin_stream_id,origin_seq,schema_version)")
        conn.execute("INSERT INTO outbox_record VALUES(NULL,1,1)")
    assert doctor.local_snapshot(path)["status"] == "fail"


@pytest.mark.parametrize("event_id", ["legacy-event-2", "../../outside", str(uuid4()).upper()])
def test_legacy_observation_ids_are_preserved_without_filesystem_lookup(producer, event_id):
    paths, _ = producer
    writer = outbox.open_outbox(paths.outbox_path)
    try:
        outbox.append_observation(writer, event_type="work.completed", actor="owner", payload={}, event_id=event_id)
    finally:
        writer.close()
    report = doctor.local_snapshot(paths.outbox_path)
    assert report["status"] == "pass"
    assert report["record_digests"][2]["event_id"] == event_id
    assert event_id not in report["receipt_event_ids"]
    assert doctor.quarantine_check(paths, report)["status"] == "pass"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served requires Python3.12+")
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("write_probe", [False, True])
def test_cli_preflight_scope_and_explicit_activity(runner, tmp_path, monkeypatch, as_json, write_probe):
    import sprintctl.cli as cli_module
    _configure_served_repo(tmp_path, monkeypatch)
    producer = make_producer(tmp_path)
    calls = _owner(monkeypatch, producer, cursor=1, repo_id="served-guard-test", reservation={
        "id": 7, "actor": "owner", "session_id": "session", "state": "active",
    })
    monkeypatch.setenv("SPRINTCTL_RUNTIME_SESSION_ID", "session")
    monkeypatch.setattr(cli_module, "_get_store", lambda _: pytest.fail("opened normal backend"))
    args = ["authority", "doctor", "--served", "--check-permissions", "--check-sequence"]
    if as_json:
        args.append("--json")
    if write_probe:
        args += ["--write-probe-reservation-id", "7"]
    result = runner.invoke(cli, args)
    assert result.exit_code == (0 if write_probe else 1), result.output
    if as_json:
        report = json.loads(result.output)
        assert report["status"] == ("pass" if write_probe else "unknown")
        assert report["authorizes_command_submission"] is False
    else:
        assert "server-client-committed-prefix-aligned" in result.output
    assert calls == (["work.read.reservation", "work.reservation.touch"] if write_probe else [])
    assert "never-render-this" not in result.output


def test_local_mode_refuses_without_initializing_authority(runner, tmp_path):
    result = runner.invoke(cli, ["authority", "doctor", "--json"])
    assert result.exit_code == 1
    assert "requires a configured served backend" in result.output
    assert not (tmp_path / ".sprintctl").exists()


@pytest.mark.parametrize("column,value", [("origin_stream_id", "x" * 10000), ("next_origin_seq", "x" * 10000)])
def test_stream_cells_are_guarded_before_materialization(producer, monkeypatch, column, value):
    paths, _ = producer
    with sqlite3.connect(paths.outbox_path) as conn:
        conn.execute(f"UPDATE outbox_stream SET {column}=?", (value,))
    statements = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "fail"
    assert not any(sql.startswith("SELECT origin_stream_id,next_origin_seq") for sql in statements)


def test_unknown_large_columns_are_not_materialized(producer, monkeypatch):
    paths, _ = producer
    with sqlite3.connect(paths.outbox_path) as conn:
        conn.execute("ALTER TABLE outbox_record ADD COLUMN future_large BLOB")
        conn.execute("DROP TRIGGER outbox_record_is_immutable_update")
        conn.execute("UPDATE outbox_record SET future_large=zeroblob(2000000)")
    statements = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "pass"
    assert not any("SELECT *" in sql for sql in statements)


def test_deep_payload_is_controlled_unknown(producer):
    paths, _ = producer
    with sqlite3.connect(paths.outbox_path) as conn:
        conn.execute("DROP TRIGGER outbox_record_is_immutable_update")
        conn.execute("UPDATE outbox_record SET payload=?", ("[" * 2000 + "0" + "]" * 2000,))
    assert doctor.local_snapshot(paths.outbox_path)["status"] == "unknown"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_deep_or_duplicate_receipt_is_never_clear(producer, ambiguous):
    paths, record = producer
    raw = ('{"event_id":"' + record.event_id + '","outcome":"quarantined-divergent-stream","outcome":"accepted"}') if ambiguous else (
        '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}")
    path = paths.terminal_dir / f"{record.event_id}.json"
    path.write_text(raw)
    path.chmod(0o600)
    assert doctor.quarantine_check(paths, doctor.local_snapshot(paths.outbox_path))["status"] in {"fail", "unknown"}


def test_receipt_decoder_recursion_is_controlled_unknown(producer, monkeypatch):
    paths, record = producer
    authority_config.mark_terminal_authority_decision(paths, event_id=record.event_id, outcome="accepted")
    snapshot = doctor.local_snapshot(paths.outbox_path)
    def recurse(pairs):
        raise RecursionError("injected decoder recursion")
    monkeypatch.setattr(doctor, "_unique_object", recurse)
    assert doctor.quarantine_check(paths, snapshot)["status"] == "unknown"


@pytest.mark.parametrize("fault_table,fault_operation", [("outbox_record", "INSERT"), ("outbox_stream", "UPDATE")])
def test_failed_enqueue_cannot_consume_sequence(producer, fault_table, fault_operation):
    paths, _ = producer
    writer = outbox.open_outbox(paths.outbox_path)
    try:
        writer.execute(f"CREATE TRIGGER inject BEFORE {fault_operation} ON {fault_table} BEGIN SELECT RAISE(ABORT,'injected'); END")
        writer.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            outbox.append_observation(writer, event_type="work.completed", actor="owner", payload={})
        report = doctor.local_snapshot(paths.outbox_path)
        assert report["status"] == "pass"
        assert report["record_count"] == 1
        assert report["next_origin_seq"] == 2
    finally:
        writer.close()
