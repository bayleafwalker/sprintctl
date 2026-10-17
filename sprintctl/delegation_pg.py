"""PostgreSQL owner storage for run-scoped delegated credentials (schema 27).

Every mutator receives the application transaction cursor. Delegation rows
store token digests only; a caller-visible token is appended to the response
after the idempotent ledger has committed its redacted result.
"""

from __future__ import annotations

import hashlib
import base64
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import delegation
from .delegation import DelegatedRunContext, DelegationRefused, ResolvedRunDelegation
from .agent_inbox_pg import owner_now

_ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
_ID_RE = re.compile(rf"\Adlg_{_ULID}\Z")
_TOKEN_RE = re.compile(r"\Avrd_[A-Z2-7]{52}\Z")
_KEY_RE = re.compile(r"\A[A-Za-z0-9._:-]{8,128}\Z")
_REASON_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")
_OP_AUTHORITY = {
    "work.run.resolve-v1": None,
    "work.run.predecessor-context-v1": "work:read",
    "work.session-note.write-v1": "work:evidence",
    "work.evidence.append-v1": "work:evidence",
}


def _refuse(code: str, message: str, status: int = 409) -> None:
    raise DelegationRefused(code, message, status)


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or _KEY_RE.fullmatch(value) is None:
        _refuse("invalid-arguments", "idempotency_key must be 8-128 permitted characters", 422)
    return value


def validate_ttl_seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= delegation.MAX_TTL_SECONDS:
        _refuse(
            "invalid-arguments",
            f"ttl_seconds must be an integer from 1 to {delegation.MAX_TTL_SECONDS}",
            422,
        )
    return value


def mint_token() -> str:
    """Return a cryptographically random 256-bit base32 bearer token."""
    return "vrd_" + base64.b32encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def validate_operations(value: Any, authorities: set[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _refuse("invalid-arguments", "operations must be a non-empty array", 422)
    if any(not isinstance(operation, str) for operation in value):
        _refuse("invalid-arguments", "operations entries must be strings", 422)
    if len(value) != len(set(value)):
        _refuse("invalid-arguments", "operations entries must be unique", 422)
    unknown = set(value) - delegation.DELEGATED_OPERATIONS
    if unknown:
        _refuse("delegation-operation-not-allowed", "operations include a non-allowlisted operation", 403)
    missing = {
        required for operation in value
        if (required := _OP_AUTHORITY[operation]) is not None and required not in authorities
    }
    if missing:
        _refuse("authority-required", f"parent identity lacks {', '.join(sorted(missing))}", 403)
    return tuple(value)


def parse_parent_expiry(value: Any) -> datetime | None:
    """Read an optional trusted `expires_at` identity field, never wire data."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DelegationRefused("identity-invalid", "parent expires_at is invalid", 403) from exc
    else:
        _refuse("identity-invalid", "parent expires_at must be an aware timestamp", 403)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _refuse("identity-invalid", "parent expires_at must include a UTC offset", 403)
    return parsed.astimezone(timezone.utc)


def mint(
    cur: Any,
    store: Any,
    *,
    principal_id: str,
    workspace_id: str,
    client_id: str | None,
    grant_id: str | None,
    authorities: set[str],
    run_id: str,
    creator_instance_id: str,
    operations: tuple[str, ...],
    ttl_seconds: int,
    parent_expires_at: datetime | None,
    idempotency_key: str,
    request_digest: str,
    token_sha256: str,
) -> dict[str, Any]:
    from . import pg

    cur.execute(
        "SELECT principal_id, workspace_id, client_id, grant_id FROM run "
        "WHERE repo_id=%s AND run_id=%s FOR UPDATE",
        (store.repo_id, run_id),
    )
    run = cur.fetchone()
    if run is None or (
        run["principal_id"], run["workspace_id"], run["client_id"], run["grant_id"]
    ) != (principal_id, workspace_id, client_id, grant_id):
        _refuse("run-not-found", f"no run {run_id!r} belongs to the caller", 404)
    cur.execute(
        "SELECT 1 FROM agent_message_receipt linked "
        "JOIN agent_message_receipt resulted ON resulted.repo_id=linked.repo_id "
        "AND resulted.workspace_id=linked.workspace_id AND resulted.message_id=linked.message_id "
        "WHERE linked.repo_id=%s AND linked.workspace_id=%s AND linked.kind='run-linked' "
        "AND linked.fields->>'run_id'=%s AND resulted.kind='result-linked' LIMIT 1",
        (store.repo_id, workspace_id, run_id),
    )
    if cur.fetchone() is not None:
        _refuse("run-settled", "a run with an explicit result-linked receipt cannot be delegated", 409)

    # A caller cannot bind automatic revocation to another principal's
    # directory instance or to an already-retired instance.
    cur.execute(
        "SELECT principal_id, retired_at FROM agent_instance "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s FOR SHARE",
        (store.repo_id, workspace_id, creator_instance_id),
    )
    instance = cur.fetchone()
    if instance is None or instance["principal_id"] != principal_id:
        _refuse("instance-not-owned", "creator instance is not owned by the parent principal", 403)
    if instance["retired_at"] is not None:
        _refuse("instance-retired", "creator instance is retired", 409)

    now = owner_now(cur)
    validate_ttl_seconds(ttl_seconds)
    if not operations or any(
        operation not in delegation.DELEGATED_OPERATIONS
        or (_OP_AUTHORITY[operation] is not None and _OP_AUTHORITY[operation] not in authorities)
        for operation in operations
    ):
        _refuse("delegation-operation-not-allowed", "delegated operations exceed parent authority", 403)
    expires_at = now + timedelta(seconds=min(ttl_seconds, delegation.MAX_TTL_SECONDS))
    if parent_expires_at is not None:
        if parent_expires_at <= now:
            _refuse("identity-expired", "parent identity is expired", 403)
        expires_at = min(expires_at, parent_expires_at)
    if expires_at <= now:
        _refuse("invalid-arguments", "delegation lifetime must be positive", 422)

    # The run row lock serializes concurrent mints. Expired-but-unrevoked rows
    # are history, not live delegations, and do not prevent replacement.
    cur.execute(
        "SELECT delegation_id FROM work_run_delegation WHERE repo_id=%s AND run_id=%s "
        "AND revoked_at IS NULL AND expires_at > %s FOR UPDATE",
        (store.repo_id, run_id, now),
    )
    if cur.fetchone() is not None:
        _refuse("delegation-exists", "the run already has a live delegation", 409)

    delegation_id = pg._mint_prefixed_id("dlg_")
    parent_authorities = sorted(authorities & {"work:read", "work:evidence"})
    cur.execute(
        "INSERT INTO work_run_delegation(repo_id, delegation_id, run_id, workspace_id, "
        "principal_id, client_id, grant_id, creator_instance_id, operations, parent_authorities, "
        "idempotency_key, request_digest, token_sha256, created_at, expires_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s) "
        "ON CONFLICT (repo_id, workspace_id, principal_id, idempotency_key) DO NOTHING",
        (
            store.repo_id, delegation_id, run_id, workspace_id, principal_id, client_id,
            grant_id, creator_instance_id, json.dumps(list(operations)),
            json.dumps(parent_authorities), idempotency_key, request_digest,
            token_sha256, now, expires_at,
        ),
    )
    inserted = cur.rowcount == 1
    cur.execute(
        "SELECT * FROM work_run_delegation WHERE repo_id=%s AND workspace_id=%s "
        "AND principal_id=%s AND idempotency_key=%s",
        (store.repo_id, workspace_id, principal_id, idempotency_key),
    )
    row = cur.fetchone()
    assert row is not None
    pg._replay_or_conflict(row["request_digest"], request_digest, "run delegation")
    # The generated token may not be used when the row was an owner-table
    # replay. The surrounding ledger's after_result callback only releases
    # it to the first caller that actually performed this insert.
    return {"delegation": _public_row(row), "_owner_created": inserted}


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    now = row.get("evaluated_at")
    return {
        "delegation_id": row["delegation_id"],
        "run_id": row["run_id"],
        "creator_instance_id": row["creator_instance_id"],
        "operations": list(row["operations"]),
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(row["expires_at"]),
        "revoked_at": _iso(row.get("revoked_at")),
        "revoked_reason": row.get("revoked_reason"),
        "state": _state(row, now),
    }


def _state(row: Mapping[str, Any], now: datetime | None) -> str:
    if row.get("revoked_at") is not None:
        return "revoked"
    if now is not None and row["expires_at"] <= now:
        return "expired"
    return "live"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    from .rows import iso_timestamp

    return iso_timestamp(value) or str(value)


def get(
    cur: Any, store: Any, *, principal_id: str, workspace_id: str,
    client_id: str | None, grant_id: str | None, delegation_id: str,
) -> dict[str, Any]:
    _validate_delegation_id(delegation_id)
    cur.execute(
        "SELECT * FROM work_run_delegation WHERE repo_id=%s AND delegation_id=%s "
        "AND principal_id=%s AND workspace_id=%s AND client_id IS NOT DISTINCT FROM %s "
        "AND grant_id IS NOT DISTINCT FROM %s",
        (store.repo_id, delegation_id, principal_id, workspace_id, client_id, grant_id),
    )
    row = cur.fetchone()
    if row is None:
        _refuse("delegation-not-found", "no delegation belongs to the caller", 404)
    evaluated_at = owner_now(cur)
    return {**_public_row({**row, "evaluated_at": evaluated_at}), "evaluated_at": _iso(evaluated_at)}


def revoke(
    cur: Any, store: Any, *, principal_id: str, workspace_id: str,
    client_id: str | None, grant_id: str | None, delegation_id: str, reason: str,
) -> dict[str, Any]:
    _validate_delegation_id(delegation_id)
    if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
        _refuse("invalid-arguments", "reason must be a lowercase reason code", 422)
    cur.execute(
        "SELECT * FROM work_run_delegation WHERE repo_id=%s AND delegation_id=%s "
        "AND principal_id=%s AND workspace_id=%s AND client_id IS NOT DISTINCT FROM %s "
        "AND grant_id IS NOT DISTINCT FROM %s FOR UPDATE",
        (store.repo_id, delegation_id, principal_id, workspace_id, client_id, grant_id),
    )
    row = cur.fetchone()
    if row is None:
        _refuse("delegation-not-found", "no delegation belongs to the caller", 404)
    if row["revoked_at"] is None:
        now = owner_now(cur)
        cur.execute(
            "UPDATE work_run_delegation SET revoked_at=%s, revoked_reason=%s "
            "WHERE repo_id=%s AND delegation_id=%s RETURNING *",
            (now, reason, store.repo_id, delegation_id),
        )
        row = cur.fetchone()
        already_revoked = False
    else:
        already_revoked = True
    return {**_public_row(row), "already_revoked": already_revoked}


def revoke_run(cur: Any, store: Any, *, workspace_id: str, run_id: str, reason: str) -> int:
    """Revoke every live delegation bound to an explicitly settled run."""
    now = owner_now(cur)
    cur.execute(
        "UPDATE work_run_delegation SET revoked_at=%s, revoked_reason=%s "
        "WHERE repo_id=%s AND workspace_id=%s AND run_id=%s AND revoked_at IS NULL",
        (now, reason, store.repo_id, workspace_id, run_id),
    )
    return cur.rowcount


def revoke_creator_instance(
    cur: Any, store: Any, *, workspace_id: str, creator_instance_id: str,
) -> int:
    now = owner_now(cur)
    cur.execute(
        "UPDATE work_run_delegation SET revoked_at=%s, revoked_reason='creator-instance-retired' "
        "WHERE repo_id=%s AND workspace_id=%s AND creator_instance_id=%s AND revoked_at IS NULL",
        (now, store.repo_id, workspace_id, creator_instance_id),
    )
    return cur.rowcount


def require_live(
    cur: Any, store: Any, *, context: DelegatedRunContext, operation: str,
    run_id: str, principal_id: str, workspace_id: str,
    client_id: str | None, grant_id: str | None, authorities: set[str],
) -> None:
    context = delegation.validate_delegated_context(context)
    if operation not in delegation.DELEGATED_OPERATIONS or operation not in context.operations:
        _refuse("delegation-operation-not-allowed", "operation is outside the delegation allowlist", 403)
    if run_id != context.run_id:
        _refuse("delegation-run-mismatch", "operation run_id differs from the delegated run", 403)
    cur.execute(
        "SELECT * FROM work_run_delegation WHERE repo_id=%s AND delegation_id=%s FOR SHARE",
        (store.repo_id, context.delegation_id),
    )
    row = cur.fetchone()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= _owner_now(cur):
        _refuse("credential-invalid", "delegation is unknown, revoked or expired", 401)
    if (row["run_id"], row["principal_id"], row["workspace_id"], row["client_id"], row["grant_id"]) != (
        context.run_id, principal_id, workspace_id, client_id, grant_id,
    ):
        _refuse("credential-invalid", "delegation binding does not match the caller", 401)
    if tuple(row["operations"]) != context.operations:
        _refuse("credential-invalid", "delegation operations do not match owner state", 401)
    allowed_authorities = set(row["parent_authorities"])
    if not authorities <= allowed_authorities:
        _refuse("credential-invalid", "delegated authorities exceed the parent grant", 401)
    required = _OP_AUTHORITY[operation]
    if required is not None and required not in allowed_authorities:
        _refuse("authority-required", f"delegation parent lacks {required}", 403)


def resolve_token_digest(store: Any, token_sha256: str) -> ResolvedRunDelegation:
    """Internal owner-only digest lookup across repo scopes; never catalogued.

    The returned repo and parent binding are authoritative. The service must
    not substitute the repository used to construct its shared store.
    """
    if not isinstance(token_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", token_sha256) is None:
        _refuse("credential-invalid", "delegation token digest is invalid", 401)
    connection_info = getattr(store.conn, "info", None)
    owns_transaction = (
        connection_info is not None and connection_info.transaction_status == 0
    )
    try:
        with store.conn.cursor() as cur:
            now = owner_now(cur)
            cur.execute(
                "SELECT * FROM work_run_delegation WHERE token_sha256=%s "
                "AND revoked_at IS NULL AND expires_at > %s",
                (token_sha256, now),
            )
            row = cur.fetchone()
    finally:
        if owns_transaction:
            store.conn.rollback()
    if row is None:
        _refuse("credential-invalid", "delegation token digest is unknown, revoked or expired", 401)
    return ResolvedRunDelegation(
        delegation=DelegatedRunContext(
            delegation_id=row["delegation_id"], run_id=row["run_id"],
            operations=tuple(row["operations"]),
        ),
        repo_id=row["repo_id"], principal_id=row["principal_id"],
        workspace_id=row["workspace_id"], client_id=row["client_id"],
        grant_id=row["grant_id"], expires_at=_iso(row["expires_at"]) or "",
        authorities=frozenset(row["parent_authorities"]),
    )


def resolve_token(store: Any, token: str) -> ResolvedRunDelegation:
    """Hash a bearer token, then use the internal digest-only resolver."""
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        _refuse("credential-invalid", "delegation token is invalid", 401)
    return resolve_token_digest(store, hashlib.sha256(token.encode("ascii")).hexdigest())


def _validate_delegation_id(value: Any) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _refuse("invalid-arguments", "delegation_id is malformed", 422)


def _owner_now(cur: Any) -> datetime:
    return owner_now(cur)
