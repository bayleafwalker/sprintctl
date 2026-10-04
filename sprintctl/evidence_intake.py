"""Durable producer requests for the existing native evidence append owner.

This is an intent carrier, not an authority or an evidence ledger. The two
append-only tables share the ordinary producer outbox database but do not
consume its observation origin sequence. No credentials belong in either table.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import outbox
from .contracts import reject_credential_shaped_values

OPERATION = "work.evidence.append-v1"
BINDING_FIELDS = frozenset(
    {"repo_id", "run_id", "principal_id", "workspace_id", "client_id", "grant_id"}
)
REQUIRED = frozenset(
    {
        "run_id",
        "item_id",
        "kind",
        "ref",
        "digest",
        "collector",
        "validity",
        "chain_seq",
        "chain_prev_digest",
        "idempotency_key",
    }
)
OPTIONAL = frozenset({"claims", "provenance"})
TAIL_FIELDS = frozenset({"chain_seq", "chain_prev_digest"})


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse(raw: bytes) -> dict:
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON object key")
            obj[key] = value
        return obj

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
    )
    if not isinstance(value, dict):
        raise ValueError("request and binding must be JSON objects")  # noqa: TRY004 - public capture validation contract
    reject_credential_shaped_values(value, "evidence-intake")
    return value


def _validate(arguments: dict, binding: dict) -> None:
    if not REQUIRED <= arguments.keys() or arguments.keys() - REQUIRED - OPTIONAL:
        raise ValueError("unsupported evidence append arguments")
    if binding.keys() != BINDING_FIELDS:
        raise ValueError("registered run binding must contain exactly six owner fields")
    for key in ("repo_id", "run_id", "principal_id", "workspace_id"):
        if not isinstance(binding[key], str) or not binding[key]:
            raise ValueError("registered run binding is incomplete")
    for key in ("client_id", "grant_id"):
        if binding[key] is not None and (
            not isinstance(binding[key], str) or not binding[key]
        ):
            raise ValueError("invalid registered grant binding")
    if arguments["run_id"] != binding["run_id"] or not re.fullmatch(
        r"run_[0-9A-HJKMNP-TV-Z]{26}", binding["run_id"]
    ):
        raise ValueError("request must name the registered run")
    for key in ("item_id", "kind", "ref", "digest", "collector", "idempotency_key"):
        if not isinstance(arguments[key], str) or not arguments[key]:
            raise ValueError("evidence text fields must be nonempty strings")
    if type(arguments["chain_seq"]) is not int or arguments["chain_seq"] < 0:
        raise ValueError("chain_seq must be a nonnegative integer")
    if arguments["chain_prev_digest"] is not None and not isinstance(
        arguments["chain_prev_digest"], str
    ):
        raise ValueError("invalid predecessor digest")
    if (
        not isinstance(arguments["validity"], dict)
        or not isinstance(arguments.get("claims", []), list)
        or not isinstance(arguments.get("provenance", {}), dict)
    ):
        raise ValueError("invalid evidence structures")  # noqa: TRY004 - capture validation contract
    # The served catalog remains the complete schema/authorization validator.
    _json(arguments)


def _semantic(arguments: dict) -> str:
    return _json(
        {key: value for key, value in arguments.items() if key not in TAIL_FIELDS}
    )


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS native_evidence_request (
        request_id TEXT PRIMARY KEY, source BLOB NOT NULL, source_sha256 TEXT NOT NULL,
        binding_source BLOB NOT NULL, binding_sha256 TEXT NOT NULL,
        binding_json TEXT NOT NULL, arguments_json TEXT NOT NULL,
        owner_key TEXT NOT NULL, supersedes TEXT UNIQUE REFERENCES native_evidence_request(request_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS native_evidence_attempt (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES native_evidence_request(request_id),
        attempt_id TEXT NOT NULL, operation TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('started','confirmed','rejected','unknown')),
        result_json TEXT, code TEXT, http_status INTEGER
    )""")
    for table in ("native_evidence_request", "native_evidence_attempt"):
        for verb in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'native evidence intake is append-only'); END"
            )
    conn.commit()


@contextmanager
def _producer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize capture/revision and invocation across processes. A killed
    # process releases flock; a persisted started event remains uncertain.
    with path.with_name(path.name + ".native-evidence.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("native evidence intake is busy; retry later") from exc
        conn = outbox.open_outbox(path)
        try:
            _init(conn)
            yield conn
        finally:
            conn.close()


def _latest(conn, request_id):
    return conn.execute(
        "SELECT * FROM native_evidence_attempt WHERE request_id=? ORDER BY sequence DESC LIMIT 1",
        (request_id,),
    ).fetchone()


def capture(
    path: Path,
    source: bytes,
    binding_source: bytes,
    *,
    repo_id: str,
    supersedes: str | None = None,
) -> dict:
    """Capture original bytes offline. No identity/credential/transport lookup."""
    arguments, binding = _parse(source), _parse(binding_source)
    _validate(arguments, binding)
    if binding["repo_id"] != repo_id:
        raise ValueError("registered run belongs to another repository")
    source_digest = hashlib.sha256(source).hexdigest()
    binding_digest = hashlib.sha256(binding_source).hexdigest()
    with _producer(path) as conn:
        rows = conn.execute(
            "SELECT * FROM native_evidence_request WHERE owner_key=? ORDER BY rowid",
            (arguments["idempotency_key"],),
        ).fetchall()
        rows = [
            row
            for row in rows
            if all(
                json.loads(row["binding_json"])[key] == binding[key]
                for key in ("repo_id", "principal_id", "workspace_id")
            )
        ]
        if rows and any(row["binding_json"] != _json(binding) for row in rows):
            raise ValueError(
                "native owner key already captured under another run or grant"
            )
        for row in rows:
            if (
                row["arguments_json"] == _json(arguments)
                and row["supersedes"] == supersedes
            ):
                return {
                    "request_id": row["request_id"],
                    "source_sha256": row["source_sha256"],
                    "submitted_source_sha256": source_digest,
                    "binding_sha256": row["binding_sha256"],
                    "submitted_binding_sha256": binding_digest,
                    "duplicate": True,
                }
        if rows:
            predecessor = rows[-1]
            outcome = _latest(conn, predecessor["request_id"])
            if (
                supersedes != predecessor["request_id"]
                or outcome is None
                or outcome["phase"] != "rejected"
                or outcome["code"] != "evidence-chain-conflict"
                or outcome["http_status"] != 409
                or outcome["operation"] != OPERATION
            ):
                raise ValueError(
                    "key already captured; correction requires a confirmed chain-conflict predecessor"
                )
            if _semantic(arguments) != _semantic(
                json.loads(predecessor["arguments_json"])
            ):
                raise ValueError("correction may change only the expected chain tail")
        elif supersedes is not None:
            raise ValueError(
                "correction predecessor does not match this owner key and binding"
            )
        request_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO native_evidence_request VALUES (?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                source,
                source_digest,
                binding_source,
                binding_digest,
                _json(binding),
                _json(arguments),
                arguments["idempotency_key"],
                supersedes,
            ),
        )
        conn.commit()
        return {
            "request_id": request_id,
            "source_sha256": source_digest,
            "binding_sha256": binding_digest,
            "duplicate": False,
        }


def _pending(conn):
    rows = conn.execute(
        """WITH RECURSIVE lineage(request_id,root_position) AS (
            SELECT request_id,rowid FROM native_evidence_request WHERE supersedes IS NULL
            UNION ALL
            SELECT r.request_id,l.root_position FROM native_evidence_request r
            JOIN lineage l ON r.supersedes=l.request_id
        ) SELECT r.* FROM native_evidence_request r JOIN lineage l ON r.request_id=l.request_id
        WHERE NOT EXISTS (SELECT 1 FROM native_evidence_request n WHERE n.supersedes=r.request_id)
        ORDER BY l.root_position"""
    ).fetchall()
    return [
        row
        for row in rows
        if (last := _latest(conn, row["request_id"])) is None
        or last["phase"] != "confirmed"
    ]


def status(path: Path) -> dict:
    if not path.exists():
        return {"pending_evidence_request_ids": [], "evidence_request_states": []}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('native_evidence_request','native_evidence_attempt')"
            )
        }
        if not tables:
            return {"pending_evidence_request_ids": [], "evidence_request_states": []}
        if len(tables) != 2:
            raise ValueError("native evidence intake schema incomplete")
        requests = []
        for row in conn.execute("SELECT * FROM native_evidence_request ORDER BY rowid"):
            last = _latest(conn, row["request_id"])
            requests.append(
                {
                    "request_id": row["request_id"],
                    "source_sha256": row["source_sha256"],
                    "binding_sha256": row["binding_sha256"],
                    "supersedes": row["supersedes"],
                    "latest_attempt": None
                    if last is None
                    else {
                        "phase": last["phase"],
                        "operation": last["operation"],
                        "code": last["code"],
                        "http_status": last["http_status"],
                    },
                }
            )
        return {
            "pending_evidence_request_ids": [
                row["request_id"] for row in _pending(conn)
            ],
            "evidence_request_states": requests,
        }

    finally:
        conn.close()


def _same_json(left: Any, right: Any) -> bool:
    """Compare owner JSONB content, keeping booleans distinct from numbers.

    JSONB may normalize an integral float to an integer. This comparison is
    only for receipt content; source bytes and native key identity stay exact.
    """
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _receipt(result: Any, arguments: dict, binding: dict) -> None:
    if (
        not isinstance(result, dict)
        or result.get("repo_id") != binding["repo_id"]
        or result.get("run_id") != arguments["run_id"]
    ):
        raise ValueError("uncorrelated native receipt")
    item = result.get("item")
    if not isinstance(item, dict):
        raise ValueError("native receipt has no evidence item")  # noqa: TRY004 - receipt validation contract
    for key in ("item_id", "kind", "ref", "digest", "collector", "validity"):
        if not _same_json(item.get(key), arguments[key]):
            raise ValueError("native receipt changed evidence identity or content")
    if not _same_json(
        item.get("claims"), arguments.get("claims", [])
    ) or not _same_json(item.get("provenance"), arguments.get("provenance", {})):
        raise ValueError("native receipt changed evidence claims or provenance")
    if (
        type(item.get("chain_seq")) is not int
        or item["chain_seq"] < 0
        or (
            item.get("chain_prev_digest") is not None
            and not isinstance(item["chain_prev_digest"], str)
        )
    ):
        raise ValueError("invalid native receipt chain")
    # Native key identity excludes tail fields. If the first observed replay
    # differs from our captured tail, it can be valid owner state but cannot
    # establish this producer's expected-tail proof. Do not silently amend it.
    if any(item[key] != arguments[key] for key in TAIL_FIELDS):
        raise ValueError("native receipt tail needs explicit owner reconciliation")
    reject_credential_shaped_values(result, "native-receipt")
    _json(result)


def synchronize(
    path: Path,
    *,
    repo_id: str,
    invoke: Callable[[str, dict], Any],
    rejection_type: type[Exception],
) -> dict:
    """Retry exact requests through native owner operations, stopping at a gap.

    A successful result is durably correlated before confirmation. Lost replies
    and malformed results stay pending. Rejections are attempts, not Decisions.
    """
    confirmed, attempts = [], []
    with _producer(path) as conn:
        for row in _pending(conn):
            arguments, binding = (
                _parse(bytes(row["source"])),
                _parse(bytes(row["binding_source"])),
            )
            _validate(arguments, binding)
            if (
                hashlib.sha256(bytes(row["source"])).hexdigest() != row["source_sha256"]
                or hashlib.sha256(bytes(row["binding_source"])).hexdigest()
                != row["binding_sha256"]
                or _json(arguments) != row["arguments_json"]
                or _json(binding) != row["binding_json"]
                or binding["repo_id"] != repo_id
            ):
                raise ValueError("stored request integrity or repository mismatch")
            attempt = str(uuid.uuid4())
            operation = "work.run.resolve-v1"
            conn.execute(
                "INSERT INTO native_evidence_attempt(request_id,attempt_id,operation,phase) VALUES (?,?,?,'started')",
                (row["request_id"], attempt, operation),
            )
            conn.commit()
            phase, result, code, http_status = "unknown", None, None, None
            try:
                resolved = invoke(
                    "work.run.resolve-v1", {"run_id": arguments["run_id"]}
                )
                if resolved != binding:
                    raise ValueError("registered run binding changed")
                operation = OPERATION
                conn.execute(
                    "INSERT INTO native_evidence_attempt(request_id,attempt_id,operation,phase) VALUES (?,?,?,'started')",
                    (row["request_id"], attempt, operation),
                )
                conn.commit()
                result = invoke(OPERATION, arguments)
                _receipt(result, arguments, binding)
                phase = "confirmed"
            except rejection_type as exc:
                phase = "rejected"
                code = getattr(exc, "code", None)
                http_status = getattr(
                    exc, "status_code", getattr(exc, "http_status", None)
                )
                # Retain only owner codes/status, never raw exception text.
                if not isinstance(code, str) or not re.fullmatch(
                    r"[a-z0-9-]{1,100}", code
                ):
                    code = "unclassified-refusal"
                if type(http_status) is not int:
                    http_status = None
                result = None
            except Exception:  # noqa: BLE001 - every unclassified invocation failure leaves an uncertain durable attempt
                result = None
                code = "outcome-unconfirmed"
            conn.execute(
                "INSERT INTO native_evidence_attempt(request_id,attempt_id,operation,phase,result_json,code,http_status) VALUES (?,?,?,?,?,?,?)",
                (
                    row["request_id"],
                    attempt,
                    operation,
                    phase,
                    _json(result) if phase == "confirmed" else None,
                    code,
                    http_status,
                ),
            )
            conn.commit()
            attempts.append(
                {
                    "request_id": row["request_id"],
                    "phase": phase,
                    "code": code,
                    "operation": operation,
                }
            )
            if phase != "confirmed":
                break
            confirmed.append(row["request_id"])
        return {
            "confirmed_evidence_request_ids": confirmed,
            "evidence_attempts": attempts,
            "pending_evidence_request_ids": [
                row["request_id"] for row in _pending(conn)
            ],
        }
