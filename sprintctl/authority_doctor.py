"""Read-only producer preflight, with an explicitly selected activity probe.

Observations are diagnostic evidence, never permission to submit a command.
No local database is opened through the initializing outbox API.
"""

from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID

from . import outbox, pg, served

MAX_RECEIPTS = 10_000
MAX_RECEIPT_BYTES = 1_048_576
MAX_RECORD_BYTES = 1_048_576
MAX_TOTAL_RECORD_BYTES = 64 * 1_048_576
MAX_REMOTE_ROWS = 10_000
_TEXT_COLUMNS = (
    "origin_stream_id", "event_id", "record_class", "event_type", "actor",
    "runtime_session_id", "occurred_at", "basis_revision", "correlation_id",
    "causation_id", "payload", "payload_sha256", "record_sha256", "created_at",
)


def _check(status: str, reason: str, **evidence) -> dict:
    return {"status": status, "reason": reason, **evidence}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def local_snapshot(path: Path) -> dict:
    """Read bounded metadata and canonical records in one transaction."""
    try:
        if not path.exists():
            return _check("unknown", "outbox-missing")
        if not stat.S_ISREG(path.lstat().st_mode):
            return _check("fail", "outbox-not-regular")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            # Bound SQLite work even for malformed legacy databases.
            budget = iter(range(10_000))
            conn.set_progress_handler(lambda: next(budget, None) is None, 1000)
            conn.execute("BEGIN")
            shape = conn.execute(
                "SELECT COUNT(*) n,MAX(length(CAST(origin_stream_id AS BLOB))) id_bytes,"
                "SUM(CASE WHEN typeof(origin_stream_id)<>'text' OR typeof(next_origin_seq)<>'integer' THEN 1 ELSE 0 END) invalid_types "
                "FROM outbox_stream"
            ).fetchone()
            if shape["n"] != 1:
                return _check("unknown" if not shape["n"] else "fail", "stream-identity-unavailable")
            if shape["invalid_types"] or shape["id_bytes"] != 36:
                return _check("fail", "stream-metadata-type-or-size-invalid")
            streams = conn.execute("SELECT origin_stream_id,next_origin_seq FROM outbox_stream").fetchmany(2)
            if len(streams) != 1:
                return _check("unknown" if not streams else "fail", "stream-identity-unavailable")
            stream, next_seq = tuple(streams[0])
            if not isinstance(stream, str) or str(UUID(stream)) != stream:
                return _check("fail", "stream-identity-invalid")
            if type(next_seq) is not int or next_seq < 1:
                return _check("fail", "next-sequence-invalid")
            row = conn.execute(
                "SELECT COUNT(*) n,MIN(origin_seq) lo,MAX(origin_seq) hi,"
                "COUNT(DISTINCT origin_seq) distinct_n,"
                "SUM(CASE WHEN typeof(origin_stream_id)<>'text' OR origin_stream_id<>? OR typeof(origin_seq)<>'integer' OR typeof(schema_version)<>'integer' THEN 1 ELSE 0 END) foreign_n "
                "FROM outbox_record", (stream,),
            ).fetchone()
            n, lo, hi = row["n"], row["lo"], row["hi"] or 0
            if row["foreign_n"] or n != row["distinct_n"] or n != hi or (n and lo != 1) or next_seq != hi + 1:
                return _check("fail", "local-sequence-gap-or-mismatch")
            if n > MAX_RECEIPTS:
                return _check("unknown", "receipt-inspection-limit")
            byte_size = "+".join(f"COALESCE(length(CAST({column} AS BLOB)),0)" for column in _TEXT_COLUMNS)
            sizes = conn.execute(f"SELECT MAX({byte_size}),SUM({byte_size}) FROM outbox_record").fetchone()
            if (sizes[0] or 0) > MAX_RECORD_BYTES or (sizes[1] or 0) > MAX_TOTAL_RECORD_BYTES:
                return _check("unknown", "record-inspection-byte-limit")
            columns = ",".join((*_TEXT_COLUMNS, "origin_seq", "schema_version"))
            records = conn.execute(f"SELECT {columns} FROM outbox_record ORDER BY origin_seq").fetchall()
            digests = {}
            receipt_event_ids = []
            for value in records:
                json.loads(value["payload"], object_pairs_hook=_unique_object)
                record = outbox._record_from_row(value)
                prepared = pg._prepare_ingest_record(record, allowed_classes=frozenset({"observation", "authority-command"}))
                digests[record.origin_seq] = {"event_id": record.event_id, "digest": prepared.record_sha256}
                try:
                    canonical_id = str(UUID(record.event_id))
                except ValueError:
                    # Generic observation identities predate UUID-only command
                    # receipts; preserve those records without using IDs as paths.
                    if record.record_class == "authority-command":
                        return _check("fail", "authority-event-identity-invalid")
                    continue
                if canonical_id != record.event_id:
                    if record.record_class == "authority-command":
                        return _check("unknown", "noncanonical-receipt-event-identity")
                    continue
                receipt_event_ids.append(record.event_id)
            return _check("pass", "contiguous-local-snapshot", stream_id=stream,
                          next_origin_seq=next_seq, record_count=n,
                          receipt_event_ids=receipt_event_ids, record_digests=digests)
    except (OSError, sqlite3.Error, ValueError, TypeError, LookupError, RecursionError):
        return _check("unknown", "outbox-inspection-unavailable")


def remote_prefix(profile, *, repo_id: str, snapshot: dict) -> dict:
    """Inspect a bounded, offset-ordered owner ledger, not just its cursor."""
    after = total = total_bytes = 0
    matching = {}
    high_water = None
    while True:
        response = served.read_records(profile, repo_id=repo_id, after_offset=after, limit=250)
        if response.get("repo_id") != repo_id:
            return _check("fail", "server-repository-mismatch")
        page = response.get("records")
        cursors = response.get("stream_high_water")
        cursor = cursors.get(snapshot["stream_id"]) if isinstance(cursors, dict) else None
        if type(cursor) is not int or cursor < 0:
            return _check("unknown", "server-stream-cursor-unavailable")
        if high_water is not None and high_water != cursor:
            return _check("unknown", "server-stream-changed-during-read")
        high_water = cursor
        if not isinstance(page, list) or len(page) > 250:
            return _check("unknown", "server-record-page-invalid")
        for entry in page:
            total += 1
            if total > MAX_REMOTE_ROWS:
                return _check("unknown", "server-record-inspection-limit")
            offset = entry.get("ingest_offset")
            if type(offset) is not int or offset <= after:
                return _check("unknown", "server-record-offset-invalid")
            after = offset
            raw = entry.get("record")
            if not isinstance(raw, dict):
                return _check("unknown", "server-record-invalid")
            size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
            total_bytes += size
            if size > MAX_RECORD_BYTES or total_bytes > MAX_TOTAL_RECORD_BYTES:
                return _check("unknown", "server-record-inspection-byte-limit")
            if raw.get("origin_stream_id") != snapshot["stream_id"]:
                continue
            record = outbox.OutboxRecord(**raw)
            prepared = pg._prepare_ingest_record(record, allowed_classes=frozenset({"observation", "authority-command"}))
            if record.origin_seq in matching:
                return _check("fail", "server-stream-duplicate-sequence")
            matching[record.origin_seq] = {"event_id": record.event_id, "digest": prepared.record_sha256}
        if len(page) < 250:
            break
    if high_water + 1 != snapshot["next_origin_seq"]:
        return _check("fail", "server-client-sequence-mismatch", server_next_origin_seq=high_water + 1)
    if len(matching) != high_water:
        return _check("unknown", "server-committed-prefix-incomplete")
    if matching != snapshot["record_digests"]:
        return _check("fail", "server-client-record-divergence")
    return _check("pass", "server-client-committed-prefix-aligned", server_next_origin_seq=high_water + 1,
                  compared_records=len(matching))


def terminal_permissions(path: Path) -> dict:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _check("unknown", "terminal-directory-missing")
    except OSError:
        return _check("unknown", "terminal-directory-unavailable")
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode) or mode != 0o700 or metadata.st_uid != os.geteuid():
        return _check("fail", "terminal-directory-not-owned-0700", observed_mode=f"{mode:04o}")
    return _check("pass", "terminal-directory-owned-0700")


def _receipt(path: Path, *, remaining_bytes: int) -> tuple[dict, int] | None:
    """No-follow bounded receipt read; never render any receipt content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        limit = min(MAX_RECEIPT_BYTES, remaining_bytes)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
            raise ValueError("invalid receipt")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("invalid receipt")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError("invalid receipt")
        return value, len(raw)
    finally:
        os.close(fd)


def quarantine_check(paths, snapshot: dict) -> dict:
    if snapshot["status"] != "pass":
        return _check("unknown", "current-stream-unavailable")
    stream = snapshot["stream_id"]
    for suffix in ("quarantined.db", "rollover.json"):
        path = paths.state_dir / f"authority-command-outbox.{stream}.{suffix}"
        if path.exists() or path.is_symlink():
            return _check("fail", "current-stream-was-rolled-over")
    try:
        if paths.terminal_dir.is_symlink():
            return _check("fail", "terminal-directory-not-regular")
        remaining_bytes = MAX_TOTAL_RECORD_BYTES
        for event_id in snapshot["receipt_event_ids"]:
            if not isinstance(event_id, str) or str(UUID(event_id)) != event_id:
                return _check("fail", "event-identity-invalid")
            receipt = _receipt(paths.terminal_dir / f"{event_id}.json", remaining_bytes=remaining_bytes)
            if receipt is None:
                continue
            value, consumed = receipt
            remaining_bytes -= consumed
            if value.get("event_id") != event_id or value.get("outcome") not in {
                "accepted", "rejected", "absent-from-served-ledger", "quarantined-divergent-stream",
            }:
                return _check("fail", "terminal-receipt-invalid")
            if value["outcome"] != "quarantined-divergent-stream" and "quarantine_reason" in value:
                return _check("fail", "terminal-receipt-invalid")
            if value["outcome"] == "quarantined-divergent-stream":
                return _check("fail", "current-stream-quarantined")
    except (OSError, ValueError, TypeError, RecursionError):
        return _check("unknown", "terminal-receipt-inspection-unavailable")
    return _check("pass", "no-current-stream-quarantine-observed")


def diagnose(paths, profile, *, repo_id: str, check_permissions: bool = True,
             check_sequence: bool = True, probe_reservation_id: int | None = None,
             session_id: str | None = None) -> dict:
    checks = {}
    exercised = []
    actor = None
    try:
        identity = served.identity_current(profile, repo_id=repo_id)
        if identity.get("repo_id") != repo_id or not isinstance(identity.get("actor"), str) or not identity["actor"].strip():
            raise ValueError("invalid identity")
        actor = identity["actor"]
        checks["authenticated_identity"] = _check("pass", "authenticated-owner-read")
        exercised.append("work.identity.current")
    except Exception:
        checks["authenticated_identity"] = _check("unknown", "authenticated-owner-read-unavailable")
    if check_permissions:
        checks["terminal_permissions"] = terminal_permissions(paths.terminal_dir)
    if check_sequence:
        snapshot = local_snapshot(paths.outbox_path)
        checks["local_sequence"] = {key: value for key, value in snapshot.items() if key not in {"receipt_event_ids", "record_digests"}}
        checks["stream_quarantine"] = quarantine_check(paths, snapshot)
        checks["server_sequence"] = _check("unknown", "local-stream-unavailable")
        if snapshot["status"] == "pass" and actor is not None:
            try:
                checks["server_sequence"] = remote_prefix(profile, repo_id=repo_id, snapshot=snapshot)
                if local_snapshot(paths.outbox_path) != snapshot:
                    checks["server_sequence"] = _check("unknown", "local-stream-changed-during-read")
                checks["stream_quarantine"] = quarantine_check(paths, snapshot)
                exercised.append("work.read.records")
            except Exception:
                checks["server_sequence"] = _check("unknown", "server-stream-read-unavailable")
    checks["write_probe"] = _check("unknown", "explicit-own-reservation-probe-required")
    if probe_reservation_id is not None:
        write_attempted = False
        prerequisites = all(row["status"] == "pass" for key, row in checks.items() if key != "write_probe")
        if not prerequisites or not session_id or actor is None:
            checks["write_probe"] = _check("unknown", "probe-prerequisites-unverified")
        else:
            try:
                def owned(response):
                    row = response.get("reservation", {})
                    return (response.get("repo_id") == repo_id and row.get("id") == probe_reservation_id
                            and row.get("actor") == actor and row.get("session_id") == session_id
                            and row.get("state") == "active")
                row = served.reservation_operation(profile, "work.read.reservation", repo_id=repo_id,
                    arguments={"reservation_id": probe_reservation_id})
                exercised.append("work.read.reservation")
                if not owned(row):
                    checks["write_probe"] = _check("fail", "probe-reservation-not-current-own-session")
                else:
                    write_attempted = True
                    updated = served.reservation_operation(profile, "work.reservation.touch", repo_id=repo_id,
                        arguments={"reservation_id": probe_reservation_id, "session_id": session_id})
                    exercised.append("work.reservation.touch")
                    checks["write_probe"] = _check("pass" if owned(updated) else "unknown",
                        "authenticated-reservation-activity-recorded" if owned(updated) else "probe-result-binding-unverified")
            except Exception:
                checks["write_probe"] = _check("unknown", "probe-failed-or-outcome-unknown")
        checks["write_probe"].update(reservation_id=probe_reservation_id, write_attempted=write_attempted)
    status = "fail" if any(row["status"] == "fail" for row in checks.values()) else (
        "unknown" if any(row["status"] == "unknown" for row in checks.values()) else "pass")
    return {"repo_id": repo_id, "status": status, "checks": checks,
            "operations_exercised": exercised, "authorizes_command_submission": False,
            "scope": "Selected preflight observations; other operations still require their own authorization.",
            "write_probe_scope": "Selected reservation activity; actor/session metadata checks are advisory and not atomic ownership proof.",
            "retired_paths": ["authority-credentials", "claim-recovery"]}
