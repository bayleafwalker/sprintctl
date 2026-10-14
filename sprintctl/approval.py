"""Structured transition approvals (schema 26): contract and storage.

An approval is an operator's immutable statement "transition T may happen for
run R of repository P until E".  It is recorded with
``work.approval.record-v1`` and read back by id with
``work.approval.get-v1``.  It is a separate record rather than a field set on
``work.decision.record``: decisions are item outcomes (accept, reject,
withdraw, ...) and that operation's catalog descriptor is byte-frozen, while
an approval names a run and a transition and expires.

Recording needs the ``work:approve`` authority, which the default agent
grant does not carry; a principal that may change work state does not
inherit it.  Reading needs ``work:read``.  The approver (principal, OAuth
client and grant), the repository and the workspace come from the
authenticated call only, never from arguments.  The approved run must be a
run of this repository in the approver's workspace, and must not already be
superseded; an approval never widens what the run's own binding resolves to.

Nothing here enforces an approval: the consumer (for example agentops
``scripts/execution_authority.py``) compares ``transition``, ``repo_id``,
``run_id``, the approver and ``expires_at`` with the effect it is about to
dispatch.  ``get`` adds the owner's ``evaluated_at`` and ``expired`` so the
consumer need not trust its own clock for expiry.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .approval_schema import MAX_LIFETIME_SECONDS

OPERATION_RECORD = "work.approval.record-v1"
OPERATION_GET = "work.approval.get-v1"
OPERATIONS = frozenset({OPERATION_RECORD, OPERATION_GET})
AUTHORITY_RECORD = "work:approve"
AUTHORITY_GET = "work:read"
OPERATION_AUTHORITIES = {OPERATION_RECORD: AUTHORITY_RECORD, OPERATION_GET: AUTHORITY_GET}
SCHEMA_VERSION = 26
LEDGER_TOOL = "record_approval"

APPROVAL_ID_PATTERN = "^apr_[0-9A-HJKMNP-TV-Z]{26}$"
TRANSITION_PATTERN = "^[a-z0-9][a-z0-9._-]{0,127}$"
RATIONALE_MAX_BYTES = 2048
_APPROVAL_ID_RE = re.compile(APPROVAL_ID_PATTERN[:-1] + r"\Z")
_TRANSITION_RE = re.compile(TRANSITION_PATTERN[:-1] + r"\Z")
_RFC3339_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})\Z"
)
RECORD_ARGUMENTS = frozenset({"transition", "run_id", "expires_at", "rationale", "idempotency_key"})
GET_ARGUMENTS = frozenset({"approval_id"})


class ApprovalRefused(Exception):
    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _invalid(message: str) -> ApprovalRefused:
    return ApprovalRefused("invalid-arguments", message, 422)


def _only(arguments: Mapping[str, Any], allowed: frozenset[str]) -> None:
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise _invalid(f"unknown argument(s): {', '.join(extra)}")


def parse_expires_at(value: Any) -> datetime:
    """An RFC 3339 timestamp with an explicit offset, as an aware UTC datetime."""
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        raise _invalid("expires_at must be an RFC 3339 timestamp with a UTC offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid("expires_at is not a valid timestamp") from exc
    return parsed.astimezone(timezone.utc)


def record_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``work.approval.record-v1`` arguments (the catalog schema
    already did on the served path; a direct ``invoke`` is checked here)."""
    _only(arguments, RECORD_ARGUMENTS)
    transition = arguments.get("transition")
    if not isinstance(transition, str) or _TRANSITION_RE.fullmatch(transition) is None:
        raise _invalid("transition must match " + TRANSITION_PATTERN)
    run_id = arguments.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _invalid("run_id must be a non-empty string")
    rationale = arguments.get("rationale")
    if rationale is not None and (
        not isinstance(rationale, str) or len(rationale.encode("utf-8")) > RATIONALE_MAX_BYTES
    ):
        raise _invalid(f"rationale must be a string of at most {RATIONALE_MAX_BYTES} bytes")
    return {
        "transition": transition,
        "run_id": run_id,
        "expires_at": parse_expires_at(arguments.get("expires_at")),
        "rationale": rationale,
    }


def get_arguments(arguments: Mapping[str, Any]) -> str:
    _only(arguments, GET_ARGUMENTS)
    approval_id = arguments.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise _invalid("approval_id must be a non-empty string")
    return approval_id


# --------------------------------------------------------------------------
# PostgreSQL storage.  Every function takes the caller's cursor; record runs
# inside the write-tool ledger transaction (pg.idempotent_write).
# --------------------------------------------------------------------------
def _now(cur: Any) -> datetime:
    # clock_timestamp(), not now(): the moment after the run lock is held.
    cur.execute("SELECT clock_timestamp() AS now")
    return cur.fetchone()["now"]


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    from .rows import iso_timestamp

    return {
        "approval_id": row["approval_id"],
        "repo_id": row["repo_id"],
        "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "transition": row["transition"],
        "rationale": row["rationale"],
        "approver": {
            "principal_id": row["approver_principal_id"],
            "client_id": row["approver_client_id"],
            "grant_id": row["approver_grant_id"],
        },
        "recorded_at": iso_timestamp(row["recorded_at"]),
        "expires_at": iso_timestamp(row["expires_at"]),
    }


def record(
    cur: Any,
    store: Any,
    *,
    principal_id: str,
    workspace_id: str,
    client_id: str | None,
    grant_id: str | None,
    idempotency_key: str,
    request_digest: str,
    transition: str,
    run_id: str,
    expires_at: datetime,
    rationale: str | None,
) -> dict[str, Any]:
    """Insert one approval and return it.

    The run is locked ``FOR KEY SHARE`` (it cannot be deleted, and a
    successor's link insert does not block) and must be in this repository
    and the approver's workspace (``run-not-found`` otherwise, one code for
    unknown and foreign).  A run that already has a registered successor is
    ``run-superseded``.  ``expires_at`` must be after the owner's clock and
    within :data:`MAX_LIFETIME_SECONDS` of it (``invalid-expiry``).
    """
    from .pg import _mint_prefixed_id, _replay_or_conflict

    cur.execute(
        "SELECT workspace_id FROM run WHERE repo_id = %s AND run_id = %s FOR KEY SHARE",
        (store.repo_id, run_id),
    )
    run = cur.fetchone()
    if run is None or run["workspace_id"] != workspace_id:
        raise ApprovalRefused("run-not-found", f"no run {run_id!r} in this workspace", 404)
    cur.execute(
        "SELECT run_id FROM run_predecessor WHERE repo_id = %s AND predecessor_run_id = %s "
        "ORDER BY created_at, run_id LIMIT 1",
        (store.repo_id, run_id),
    )
    successor = cur.fetchone()
    if successor is not None:
        raise ApprovalRefused(
            "run-superseded", f"run {run_id!r} is superseded by {successor['run_id']!r}", 409
        )
    now = _now(cur)
    if expires_at <= now:
        raise ApprovalRefused("invalid-expiry", "expires_at must be in the future", 422)
    if expires_at > now + timedelta(seconds=MAX_LIFETIME_SECONDS):
        raise ApprovalRefused(
            "invalid-expiry",
            f"expires_at must be at most {MAX_LIFETIME_SECONDS} seconds after recording",
            422,
        )
    approval_id = _mint_prefixed_id("apr_")
    cur.execute(
        "INSERT INTO work_approval(repo_id, approval_id, workspace_id, run_id, transition, "
        "rationale, approver_principal_id, approver_client_id, approver_grant_id, "
        "idempotency_key, request_digest, recorded_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (repo_id, workspace_id, approver_principal_id, idempotency_key) DO NOTHING",
        (
            store.repo_id, approval_id, workspace_id, run_id, transition, rationale,
            principal_id, client_id, grant_id, idempotency_key, request_digest, now, expires_at,
        ),
    )
    cur.execute(
        "SELECT * FROM work_approval WHERE repo_id = %s AND workspace_id = %s "
        "AND approver_principal_id = %s AND idempotency_key = %s",
        (store.repo_id, workspace_id, principal_id, idempotency_key),
    )
    row = cur.fetchone()
    assert row is not None
    _replay_or_conflict(row["request_digest"], request_digest, "approval")
    return _record(row)


def get(cur: Any, store: Any, *, workspace_id: str, approval_id: str) -> dict[str, Any]:
    """The approval with the owner's ``evaluated_at`` and ``expired``.

    Scoped to this repository and the caller's workspace;
    ``approval-not-found`` (404) for an unknown, malformed or out-of-scope
    id alike.
    """
    if _APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise ApprovalRefused("approval-not-found", "no such approval", 404)
    cur.execute(
        "SELECT a.*, clock_timestamp() AS evaluated_at FROM work_approval a "
        "WHERE a.repo_id = %s AND a.workspace_id = %s AND a.approval_id = %s",
        (store.repo_id, workspace_id, approval_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ApprovalRefused("approval-not-found", "no such approval", 404)
    from .rows import iso_timestamp

    return {
        **_record(row),
        "evaluated_at": iso_timestamp(row["evaluated_at"]),
        "expired": row["evaluated_at"] >= row["expires_at"],
    }
