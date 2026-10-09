"""Append-only offline reserve intent; only the native owner makes it effective.

Run binding is producer provenance, not a new run association on reservations.
This first carrier deliberately has no corrections or cross-stream ordering.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
import uuid

from . import evidence_intake as evidence, outbox, releases, reservation
from .contracts import reject_credential_shaped_values

OPERATION = "work.reservation.reserve-v1"
RESOLVE = "work.run.resolve-v1"
RELEASE = "work.read.release"
SCHEMA = "native-reserve-request/v1"
TABLES = {"native_reserve_request", "native_reserve_attempt"}
ARGUMENTS = {"item_id", "actor", "session_id", "role", "correlation_ref",
             "interrupt_existing", "expected_revision", "acceptance_contract"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize(raw: bytes, binding_raw: bytes, repo_id: str) -> tuple[dict, dict]:
    request, binding = evidence._parse(raw), evidence._parse(binding_raw)
    if set(request) != {"schema_version", "operation", "idempotency_key", "arguments"}:
        raise ValueError("reserve request must have exactly four envelope fields")
    if request["schema_version"] != SCHEMA or request["operation"] != OPERATION:
        raise ValueError("unsupported reserve request schema or operation")
    key = request["idempotency_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise ValueError("reserve key must match the native owner key contract")
    if set(binding) != evidence.BINDING_FIELDS or binding.get("repo_id") != repo_id:
        raise ValueError("reserve requires the exact registered run binding and repository")
    for name in ("repo_id", "run_id", "principal_id", "workspace_id"):
        if not isinstance(binding[name], str) or not binding[name]:
            raise ValueError("incomplete run binding")
    if not re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", binding["run_id"]):
        raise ValueError("invalid registered run reference")
    for name in ("client_id", "grant_id"):
        if binding[name] is not None and (not isinstance(binding[name], str) or not binding[name]):
            raise ValueError("invalid registered grant binding")
    args = request["arguments"]
    required = {"item_id", "actor", "session_id", "expected_revision"}
    if not isinstance(args, dict) or not required <= args.keys() or args.keys() - ARGUMENTS:
        raise ValueError("invalid closed reserve arguments")
    if type(args["item_id"]) is not int or args["item_id"] <= 0:
        raise ValueError("reserve item_id must be a positive integer")
    for name in ("actor", "session_id"):
        if not isinstance(args[name], str) or not args[name].strip():
            raise ValueError("reserve actor and session must be nonempty text")
    basis = releases.validate_basis(args["expected_revision"])
    if not releases._RELEASE_REVISION_RE.fullmatch(basis):
        raise ValueError("offline reserve requires the full owner Release revision")
    role = args.get("role", "execution")
    if not isinstance(role, str) or role not in reservation.ROLES:
        raise ValueError("invalid reserve role")
    interrupt, correlation = args.get("interrupt_existing", False), args.get("correlation_ref")
    if type(interrupt) is not bool or (correlation is not None and not isinstance(correlation, str)):
        raise ValueError("invalid reserve interruption or correlation")
    if "acceptance_contract" in args and (role != "execution" or not isinstance(args["acceptance_contract"], dict)):
        raise ValueError("explicit acceptance contract requires execution and an object")
    normalized = {**args, "role": role, "interrupt_existing": interrupt, "correlation_ref": correlation}
    # A non-execution request does not pass an explicit default contract to the owner.
    if role == "execution":
        normalized["acceptance_contract"] = releases.normalize_acceptance_contract(args.get("acceptance_contract"))
    return {**request, "arguments": normalized}, binding


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _init(conn):
    present = _tables(conn) & TABLES
    if present and present != TABLES:
        raise ValueError("native reserve schema incomplete; do not repair by initialization")
    conn.execute("""CREATE TABLE IF NOT EXISTS native_reserve_request (
        request_id TEXT PRIMARY KEY, source BLOB NOT NULL, source_sha256 TEXT NOT NULL,
        binding_source BLOB NOT NULL, binding_sha256 TEXT NOT NULL,
        request_json TEXT NOT NULL, binding_json TEXT NOT NULL, owner_key TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS native_reserve_attempt (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES native_reserve_request(request_id),
        attempt_id TEXT NOT NULL, operation TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('started','confirmed','rejected','unknown')),
        result_json TEXT, result_sha256 TEXT, code TEXT, http_status INTEGER)""")
    for table in TABLES:
        for verb in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'native reserve intake is append-only'); END")
    conn.commit()


@contextmanager
def _producer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".native-reserve.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("native reserve intake busy; retry later") from exc
        conn = outbox.open_outbox(path)
        try:
            _init(conn)
            _validate_history(conn)
            yield conn
        finally:
            conn.close()


def _latest(conn, request_id):
    return conn.execute("SELECT * FROM native_reserve_attempt WHERE request_id=? ORDER BY sequence DESC LIMIT 1", (request_id,)).fetchone()


def _pending(conn):
    _validate_history(conn)
    return [r for r in conn.execute("SELECT * FROM native_reserve_request ORDER BY rowid")
            if (last := _latest(conn, r["request_id"])) is None or last["phase"] != "confirmed"]


def capture(path: Path, source: bytes, binding_source: bytes, *, repo_id: str) -> dict:
    request, binding = _normalize(source, binding_source, repo_id)
    with _producer(path) as conn:
        for row in conn.execute("SELECT * FROM native_reserve_request WHERE owner_key=? ORDER BY rowid", (request["idempotency_key"],)):
            prior_binding = json.loads(row["binding_json"])
            if any(prior_binding[k] != binding[k] for k in ("repo_id", "principal_id", "workspace_id")):
                continue
            if row["request_json"] != evidence._json(request) or row["binding_json"] != evidence._json(binding):
                raise ValueError("reserve owner key already captured with different content or binding")
            return {"request_id": row["request_id"], "duplicate": True,
                    "source_sha256": row["source_sha256"], "binding_sha256": row["binding_sha256"],
                    "submitted_source_sha256": _digest(source), "submitted_binding_sha256": _digest(binding_source)}
        identity = str(uuid.uuid4())
        conn.execute("INSERT INTO native_reserve_request VALUES (?,?,?,?,?,?,?,?)",
                     (identity, source, _digest(source), binding_source, _digest(binding_source),
                      evidence._json(request), evidence._json(binding), request["idempotency_key"]))
        conn.commit()
        return {"request_id": identity, "duplicate": False, "source_sha256": _digest(source),
                "binding_sha256": _digest(binding_source)}


def status(path: Path) -> dict:
    if not path.exists():
        return {"pending_reserve_request_ids": [], "reserve_request_states": []}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = _tables(conn)
        if not {"outbox_stream", "outbox_record"} <= tables:
            raise ValueError("not a producer outbox database")
        present = tables & TABLES
        if not present:
            return {"pending_reserve_request_ids": [], "reserve_request_states": []}
        if present != TABLES:
            raise ValueError("native reserve schema incomplete")
        _validate_history(conn)
        states = []
        for row in conn.execute("SELECT * FROM native_reserve_request ORDER BY rowid"):
            last = _latest(conn, row["request_id"])
            states.append({"request_id": row["request_id"], "source_sha256": row["source_sha256"],
                           "binding_sha256": row["binding_sha256"], "latest_attempt": None if last is None else
                           {k: last[k] for k in ("phase", "operation", "code", "http_status", "result_sha256")}})
        return {"pending_reserve_request_ids": [r["request_id"] for r in _pending(conn)], "reserve_request_states": states}
    finally:
        conn.close()


def _receipt(result: Any, args: dict, binding: dict) -> str | None:
    if not isinstance(result, dict) or result.get("repo_id") != binding["repo_id"]:
        raise ValueError("uncorrelated reserve repository")
    current = result.get("reservation")
    if not isinstance(current, dict) or type(current.get("replayed")) is not bool:
        raise ValueError("invalid reserve receipt")
    snapshot = current.get("admission_snapshot")
    if not isinstance(snapshot, dict) or type(snapshot.get("id")) is not int or snapshot["id"] <= 0:
        raise ValueError("invalid admission snapshot")
    for field, expected in {"repo_id": binding["repo_id"], "work_item_id": args["item_id"],
                            "actor": args["actor"], "session_id": args["session_id"],
                            "role": args["role"], "correlation_ref": args["correlation_ref"], "state": "active"}.items():
        if not evidence._same_json(snapshot.get(field), expected):
            raise ValueError("reserve admission changed captured identity")
    for field in ("id", "repo_id", "work_item_id", "role", "release_digest", "created_at"):
        if not evidence._same_json(current.get(field), snapshot.get(field)):
            raise ValueError("reserve current row changed immutable identity")
    if current.get("state") not in {"active", "released", "interrupted"}:
        raise ValueError("invalid current reservation state")
    for name in ("actor", "session_id", "created_at", "last_activity_at"):
        if not isinstance(current.get(name), str) or not current[name]:
            raise ValueError("invalid current reservation fields")
    if current.get("correlation_ref") is not None and not isinstance(current["correlation_ref"], str):
        raise ValueError("invalid current correlation reference")
    for row in (snapshot, current):
        if type(row.get("conflict")) is not bool or not isinstance(row.get("conflicting_reservations"), list):
            raise ValueError("invalid reserve overlap annotation")
        overlaps = row["conflicting_reservations"]
        for overlap in overlaps:
            if (not isinstance(overlap, dict) or type(overlap.get("id")) is not int or
                overlap["id"] <= 0 or overlap["id"] == row["id"] or
                not evidence._same_json(overlap.get("work_item_id"), args["item_id"]) or
                not isinstance(overlap.get("role"), str) or
                overlap["role"] not in reservation.ROLES or overlap.get("state") != "active"):
                raise ValueError("invalid reserve overlap identity")
        severity = "warning" if row["role"] == "execution" and any(o["role"] == "execution" for o in overlaps) else ("informational" if overlaps else "none")
        if row["conflict"] != bool(overlaps) or row.get("conflict_severity") != severity:
            raise ValueError("inconsistent reserve overlap annotation")
    digest = snapshot.get("release_digest")
    if args["role"] == "execution":
        releases.validate_digest(digest)
    elif digest is not None:
        raise ValueError("non-execution receipt unexpectedly freezes a Release")
    reject_credential_shaped_values(result, "reserve-receipt")
    evidence._json(result)
    return digest


def _release_receipt(result: Any, digest: str, args: dict, binding: dict):
    if not isinstance(result, dict) or result.get("repo_id") != binding["repo_id"]:
        raise ValueError("uncorrelated Release response")
    release = result.get("release")
    if not isinstance(release, dict):
        raise ValueError("missing Release response")
    expected = {"repo_id": binding["repo_id"], "work_item_id": args["item_id"],
                "release_digest": digest, "item_revision": args["expected_revision"],
                "acceptance_contract": args["acceptance_contract"]}
    if any(not evidence._same_json(release.get(k), v) for k, v in expected.items()):
        raise ValueError("Release does not match captured basis and contract")
    # The public Release row omits aggregate_uuid; the full observed revision
    # already names it. Do not invent another owner field.
    aggregate = args["expected_revision"].split("@")[0].removeprefix("item:")
    if releases.release_digest(aggregate, release["item_revision"],
                               release["acceptance_contract"], release["context_refs"]) != digest:
        raise ValueError("Release content digest mismatch")
    reject_credential_shaped_values(result, "reserve-release-receipt")
    evidence._json(result)


def _validate_history(conn):
    """Do not let a damaged confirmation hide a pending owner outcome."""
    for row in conn.execute("SELECT * FROM native_reserve_request ORDER BY rowid"):
        binding_raw = bytes(row["binding_source"])
        repo_id = evidence._parse(binding_raw).get("repo_id")
        request, binding = _normalize(bytes(row["source"]), binding_raw, repo_id)
        if (_digest(bytes(row["source"])) != row["source_sha256"] or
            _digest(binding_raw) != row["binding_sha256"] or evidence._json(request) != row["request_json"] or
            evidence._json(binding) != row["binding_json"] or request["idempotency_key"] != row["owner_key"]):
            raise ValueError("reserve intent integrity mismatch")
        for result in conn.execute("SELECT result_json,result_sha256 FROM native_reserve_attempt WHERE request_id=? AND phase='confirmed'", (row["request_id"],)):
            content = result["result_json"]
            if not isinstance(content, str) or _digest(content.encode()) != result["result_sha256"]:
                raise ValueError("reserve confirmation integrity mismatch")
            receipt = evidence._parse(content.encode())
            digest = _receipt(receipt.get("reservation_response"), request["arguments"], binding)
            if digest is not None:
                _release_receipt(receipt.get("release_response"), digest, request["arguments"], binding)


def _attempt(conn, request_id, attempt, operation, phase, *, result=None, code=None, http_status=None):
    content = evidence._json(result) if result is not None else None
    conn.execute("INSERT INTO native_reserve_attempt(request_id,attempt_id,operation,phase,result_json,result_sha256,code,http_status) VALUES (?,?,?,?,?,?,?,?)",
                 (request_id, attempt, operation, phase, content, None if content is None else _digest(content.encode()), code, http_status))
    conn.commit()


def synchronize(path: Path, *, repo_id: str, invoke: Callable, rejection_type: type[Exception]) -> dict:
    confirmed, attempts = [], []
    with _producer(path) as conn:
        for row in _pending(conn):
            request, binding = _normalize(bytes(row["source"]), bytes(row["binding_source"]), repo_id)
            if (_digest(bytes(row["source"])) != row["source_sha256"] or
                _digest(bytes(row["binding_source"])) != row["binding_sha256"] or
                evidence._json(request) != row["request_json"] or evidence._json(binding) != row["binding_json"] or
                request["idempotency_key"] != row["owner_key"]):
                raise ValueError("reserve intent integrity mismatch")
            attempt, operation = str(uuid.uuid4()), RESOLVE
            phase, code, http_status, receipt = "unknown", None, None, None
            try:
                _attempt(conn, row["request_id"], attempt, operation, "started")
                resolved = invoke(operation, {"run_id": binding["run_id"]}, None)
                if not evidence._same_json(resolved, binding):
                    raise ValueError("registered run binding changed")
                operation = OPERATION
                _attempt(conn, row["request_id"], attempt, operation, "started")
                result = invoke(operation, request["arguments"], request["idempotency_key"])
                digest = _receipt(result, request["arguments"], binding)
                receipt = {"reservation_response": result, "release_response": None}
                if digest is not None:
                    operation = RELEASE
                    _attempt(conn, row["request_id"], attempt, operation, "started")
                    release = invoke(operation, {"release_digest": digest}, None)
                    _release_receipt(release, digest, request["arguments"], binding)
                    receipt["release_response"] = release
                phase = "confirmed"
            except rejection_type as exc:
                phase, receipt = "rejected", None
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", code):
                    code = "unclassified-refusal"
                http_status = getattr(exc, "status_code", getattr(exc, "http_status", None))
                if type(http_status) is not int:
                    http_status = None
            except Exception:
                receipt, code = None, "outcome-unconfirmed"
            _attempt(conn, row["request_id"], attempt, operation, phase, result=receipt, code=code, http_status=http_status)
            attempts.append({"request_id": row["request_id"], "phase": phase, "operation": operation, "code": code})
            if phase != "confirmed":
                break
            confirmed.append(row["request_id"])
        return {"confirmed_reserve_request_ids": confirmed, "reserve_attempts": attempts,
                "pending_reserve_request_ids": [r["request_id"] for r in _pending(conn)]}
