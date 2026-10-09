"""Append-only offline proposal intent; only the native owner admits an effect intent.

Registered run binding is producer provenance. Content and its whitespace are immutable.
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

from . import evidence_intake as evidence, outbox, effect_intent, effect_causal
from .contracts import reject_credential_shaped_values

OPERATION = "work.effect.propose-v1"
RESOLVE = "work.run.resolve-v1"
SCHEMA = "native-proposal-request/v1"
BOUND_SCHEMA = "native-bound-proposal-request/v1"
BOUND_OPERATION = effect_intent.OPERATION_BOUND_PROPOSE
TABLES = {"native_proposal_request", "native_proposal_attempt"}
ARGUMENTS = {"run_id", "item_id", "repository", "base_commit", "title", "rationale",
             "unified_diff", "idempotency_key"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize(raw: bytes, binding_raw: bytes, repo_id: str) -> tuple[dict, dict]:
    request, binding = evidence._parse(raw), evidence._parse(binding_raw)
    if set(request) != {"schema_version", "operation", "arguments"}:
        raise ValueError("proposal request must have exactly three envelope fields")
    if not all(isinstance(request[name], str) for name in ("schema_version", "operation")):
        raise ValueError("proposal schema and operation must be strings")
    pair = (request["schema_version"], request["operation"])
    if pair not in {(SCHEMA, OPERATION), (BOUND_SCHEMA, BOUND_OPERATION)}:
        raise ValueError("unsupported proposal request schema or operation")
    bound = pair == (BOUND_SCHEMA, BOUND_OPERATION)
    if set(binding) != evidence.BINDING_FIELDS or binding.get("repo_id") != repo_id:
        raise ValueError("proposal requires the exact registered run binding and repository")
    for name in ("repo_id", "run_id", "principal_id", "workspace_id"):
        if not isinstance(binding[name], str) or not binding[name]:
            raise ValueError("incomplete run binding")
    if not re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", binding["run_id"]):
        raise ValueError("invalid registered run reference")
    for name in ("client_id", "grant_id"):
        if binding[name] is not None and (not isinstance(binding[name], str) or not binding[name]):
            raise ValueError("invalid registered grant binding")
    args = request["arguments"]
    expected = ARGUMENTS | {"causal_basis"} if bound else ARGUMENTS
    if not isinstance(args, dict) or set(args) != expected:
        raise ValueError("invalid closed proposal arguments")
    if bound:
        effect_causal.validate_basis(args["causal_basis"])
    if not evidence._same_json(args["run_id"], binding["run_id"]):
        raise ValueError("proposal run differs from registered binding")
    if type(args["item_id"]) is not int or args["item_id"] <= 0:
        raise ValueError("proposal item_id must be a positive integer")
    if not isinstance(args["idempotency_key"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", args["idempotency_key"]):
        raise ValueError("proposal key must match the native owner key contract")
    if not isinstance(args["base_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", args["base_commit"]):
        raise ValueError("invalid proposal base commit")
    limits = {"repository": effect_intent.MAX_REPOSITORY, "title": effect_intent.MAX_TITLE,
              "rationale": effect_intent.MAX_RATIONALE, "unified_diff": effect_intent.MAX_UNIFIED_DIFF}
    for name, limit in limits.items():
        value = args[name]
        if (not isinstance(value, str) or not value or len(value) > limit or "\0" in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("invalid proposal text: " + name)
    # Preserve every content character: diff whitespace is part of the intent.
    return request, binding


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _init(conn):
    present = _tables(conn) & TABLES
    if present and present != TABLES:
        raise ValueError("native proposal schema incomplete; do not repair by initialization")
    conn.execute("""CREATE TABLE IF NOT EXISTS native_proposal_request (
        request_id TEXT PRIMARY KEY, source BLOB NOT NULL, source_sha256 TEXT NOT NULL,
        binding_source BLOB NOT NULL, binding_sha256 TEXT NOT NULL,
        request_json TEXT NOT NULL, binding_json TEXT NOT NULL, owner_key TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS native_proposal_attempt (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES native_proposal_request(request_id),
        attempt_id TEXT NOT NULL, operation TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('started','confirmed','rejected','unknown')),
        result_json TEXT, result_sha256 TEXT, code TEXT, http_status INTEGER)""")
    for table in TABLES:
        for verb in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'native proposal intake is append-only'); END")
    conn.commit()


@contextmanager
def _producer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".native-proposal.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("native proposal intake busy; retry later") from exc
        conn = outbox.open_outbox(path)
        try:
            _init(conn)
            _validate_history(conn)
            yield conn
        finally:
            conn.close()


def _latest(conn, request_id):
    return conn.execute("SELECT * FROM native_proposal_attempt WHERE request_id=? ORDER BY sequence DESC LIMIT 1", (request_id,)).fetchone()


def _pending(conn):
    _validate_history(conn)
    return [r for r in conn.execute("SELECT * FROM native_proposal_request ORDER BY rowid")
            if (last := _latest(conn, r["request_id"])) is None or last["phase"] != "confirmed"]


def capture(path: Path, source: bytes, binding_source: bytes, *, repo_id: str) -> dict:
    request, binding = _normalize(source, binding_source, repo_id)
    with _producer(path) as conn:
        for row in conn.execute("SELECT * FROM native_proposal_request WHERE owner_key=? ORDER BY rowid", (request["arguments"]["idempotency_key"],)):
            prior_binding = json.loads(row["binding_json"])
            if any(prior_binding[k] != binding[k] for k in ("repo_id", "principal_id", "workspace_id")):
                continue
            if row["request_json"] != evidence._json(request) or row["binding_json"] != evidence._json(binding):
                raise ValueError("proposal owner key already captured with different content or binding")
            return {"request_id": row["request_id"], "duplicate": True,
                    "source_sha256": row["source_sha256"], "binding_sha256": row["binding_sha256"],
                    "submitted_source_sha256": _digest(source), "submitted_binding_sha256": _digest(binding_source)}
        identity = str(uuid.uuid4())
        conn.execute("INSERT INTO native_proposal_request VALUES (?,?,?,?,?,?,?,?)",
                     (identity, source, _digest(source), binding_source, _digest(binding_source),
                      evidence._json(request), evidence._json(binding), request["arguments"]["idempotency_key"]))
        conn.commit()
        return {"request_id": identity, "duplicate": False, "source_sha256": _digest(source),
                "binding_sha256": _digest(binding_source)}


def status(path: Path) -> dict:
    if not path.exists():
        return {"pending_proposal_request_ids": [], "proposal_request_states": []}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = _tables(conn)
        if not {"outbox_stream", "outbox_record"} <= tables:
            raise ValueError("not a producer outbox database")
        present = tables & TABLES
        if not present:
            return {"pending_proposal_request_ids": [], "proposal_request_states": []}
        if present != TABLES:
            raise ValueError("native proposal schema incomplete")
        _validate_history(conn)
        states = []
        for row in conn.execute("SELECT * FROM native_proposal_request ORDER BY rowid"):
            last = _latest(conn, row["request_id"])
            states.append({"request_id": row["request_id"], "source_sha256": row["source_sha256"],
                           "binding_sha256": row["binding_sha256"], "latest_attempt": None if last is None else
                           {k: last[k] for k in ("phase", "operation", "code", "http_status", "result_sha256")}})
        return {"pending_proposal_request_ids": [r["request_id"] for r in _pending(conn)], "proposal_request_states": states}
    finally:
        conn.close()


def _receipt(result: Any, args: dict, binding: dict) -> None:
    bound = "causal_basis" in args
    fields = {"repo_id", "intent", "admission"} if bound else {"repo_id", "intent"}
    if not isinstance(result, dict) or set(result) != fields or result.get("repo_id") != binding["repo_id"]:
        raise ValueError("uncorrelated proposal repository")
    if bound:
        admission = result["admission"]
        if (not isinstance(admission, dict)
            or set(admission) != {"schema_version", "causal_basis", "run_binding", "reservation_id"}
            or admission["schema_version"] != effect_causal.ADMISSION_SCHEMA
            or not evidence._same_json(admission["causal_basis"], args["causal_basis"])
            or not evidence._same_json(admission["run_binding"], binding)
            or type(admission["reservation_id"]) is not int or admission["reservation_id"] <= 0):
            raise ValueError("uncorrelated bound proposal admission")
    intent = result["intent"]
    if not isinstance(intent, dict):
        raise ValueError("invalid proposal intent")
    if bound and not evidence._same_json(intent.get("release_digest"), args["causal_basis"]["release_digest"]):
        raise ValueError("bound proposal intent differs from captured Release")
    for name in (*effect_intent.DIGEST_FIELDS, "run_id"):
        if not evidence._same_json(intent.get(name), args[name]):
            raise ValueError("proposal receipt differs from captured content")
    if intent.get("proposer_principal") != binding["principal_id"]:
        raise ValueError("proposal receipt differs from captured principal")
    if not isinstance(intent.get("intent_id"), str) or not re.fullmatch(r"intent_[0-9A-HJKMNP-TV-Z]{26}", intent["intent_id"]):
        raise ValueError("invalid proposal intent identifier")
    if type(intent.get("revision")) is not int or intent["revision"] < 1 or intent.get("state") not in effect_intent.EFFECT_STATES:
        raise ValueError("invalid current proposal lifecycle")
    if intent.get("canonical_intent_digest") != effect_intent.canonical_intent_digest(intent):
        raise ValueError("proposal content digest mismatch")
    reject_credential_shaped_values(result, "proposal-receipt")
    evidence._json(result)


def _validate_history(conn):
    """Do not let a damaged confirmation hide a pending owner outcome."""
    for row in conn.execute("SELECT * FROM native_proposal_request ORDER BY rowid"):
        binding_raw = bytes(row["binding_source"])
        repo_id = evidence._parse(binding_raw).get("repo_id")
        request, binding = _normalize(bytes(row["source"]), binding_raw, repo_id)
        if (_digest(bytes(row["source"])) != row["source_sha256"] or
            _digest(binding_raw) != row["binding_sha256"] or evidence._json(request) != row["request_json"] or
            evidence._json(binding) != row["binding_json"] or request["arguments"]["idempotency_key"] != row["owner_key"]):
            raise ValueError("proposal intent integrity mismatch")
        for result in conn.execute("SELECT operation,result_json,result_sha256 FROM native_proposal_attempt WHERE request_id=? AND phase='confirmed'", (row["request_id"],)):
            if result["operation"] != request["operation"]:
                raise ValueError("proposal confirmation operation mismatch")
            content = result["result_json"]
            if not isinstance(content, str) or _digest(content.encode()) != result["result_sha256"]:
                raise ValueError("proposal confirmation integrity mismatch")
            receipt = evidence._parse(content.encode())
            _receipt(receipt, request["arguments"], binding)


def _attempt(conn, request_id, attempt, operation, phase, *, result=None, code=None, http_status=None):
    content = evidence._json(result) if result is not None else None
    conn.execute("INSERT INTO native_proposal_attempt(request_id,attempt_id,operation,phase,result_json,result_sha256,code,http_status) VALUES (?,?,?,?,?,?,?,?)",
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
                request["arguments"]["idempotency_key"] != row["owner_key"]):
                raise ValueError("proposal intent integrity mismatch")
            attempt, operation = str(uuid.uuid4()), RESOLVE
            phase, code, http_status, receipt = "unknown", None, None, None
            try:
                _attempt(conn, row["request_id"], attempt, operation, "started")
                resolved = invoke(operation, {"run_id": binding["run_id"]})
                if not evidence._same_json(resolved, binding):
                    raise ValueError("registered run binding changed")
                operation = request["operation"]
                _attempt(conn, row["request_id"], attempt, operation, "started")
                result = invoke(operation, request["arguments"])
                _receipt(result, request["arguments"], binding)
                receipt = result
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
        return {"confirmed_proposal_request_ids": confirmed, "proposal_attempts": attempts,
                "pending_proposal_request_ids": [r["request_id"] for r in _pending(conn)]}
