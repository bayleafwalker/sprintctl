"""Backend-neutral row normalisation and serializers for sprintctl.

Both storage backends (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) return
the same public value contract: plain dicts with ISO-8601 string timestamps.
This module is the single home for the helpers that produce that contract, so
the two backends cannot drift apart in serialization behaviour.

Nothing in this module opens a connection or executes SQL; it only transforms
rows already fetched by a backend.
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

# Identity status values for the public claim contract.  These live here (and
# are re-exported by ``db.py``) because both backends serialise claims through
# ``serialize_claim`` below.
CLAIM_IDENTITY_STATUS_PROVEN = "proven"
CLAIM_IDENTITY_STATUS_LEGACY = "legacy_ambiguous"


def iso_timestamp(value: Any) -> str | None:
    """Return an ISO-8601 string for a timestamp-like value, or None.

    Fractional seconds are preserved: producer ledger hashes cover these
    timestamps, so truncating them on a read turns an otherwise identical
    served record into a different record when a client audits it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def normalize_row(row: dict) -> dict:
    """Normalise a backend row to the SQLite-compatible public value contract.

    PostgreSQL (psycopg) returns ``datetime``/``date``/``UUID`` objects;
    SQLite returns strings.  Normalising here lets every downstream consumer
    treat both backends identically.  For SQLite rows this is a no-op.
    """
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = iso_timestamp(value)
        elif isinstance(value, _dt.date):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def claim_identity_status(row: Any) -> str:
    return (
        CLAIM_IDENTITY_STATUS_PROVEN
        if row["claim_token"]
        else CLAIM_IDENTITY_STATUS_LEGACY
    )


def claim_event_identity(row: Any) -> dict:
    """Identity block embedded in claim lifecycle events.

    Rows passed here must already be normalised (see ``normalize_row``) so the
    emitted event payload is byte-identical across backends.
    """
    return {
        "claim_id": row["id"],
        "actor": row["agent"],
        "runtime_session_id": row["runtime_session_id"],
        "instance_id": row["instance_id"],
        "branch": row["branch"],
        "worktree_path": row["worktree_path"],
        "commit_sha": row["commit_sha"],
        "pr_ref": row["pr_ref"],
        "hostname": row["hostname"],
        "pid": row["pid"],
        "claim_token_present": bool(row["claim_token"]),
        "identity_status": claim_identity_status(row),
        "status": row["status"],
        "lease_epoch": row["lease_epoch"],
    }


def claim_attempt_identity(
    *,
    actor: str | None = None,
    claim_id: int | None = None,
    claim_token_present: bool = False,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> dict:
    return {
        "claim_id": claim_id,
        "actor": actor,
        "runtime_session_id": runtime_session_id,
        "instance_id": instance_id,
        "branch": branch,
        "worktree_path": worktree_path,
        "commit_sha": commit_sha,
        "pr_ref": pr_ref,
        "hostname": hostname,
        "pid": pid,
        "claim_token_present": claim_token_present,
    }


def serialize_claim(row: Any, *, include_secret: bool = False) -> dict:
    """Serialize a claim row to the public claim contract.

    Backend-neutral: the caller passes a normalised row dict (SQLite rows are
    already in the public shape; PostgreSQL callers apply ``normalize_row``
    first).
    """
    raw = dict(row)
    claim_token = raw.get("claim_token")
    identity_status = claim_identity_status(raw)
    if not include_secret:
        raw.pop("claim_token", None)
    claim = {
        **raw,
        "claim_id": raw["id"],
        "actor": raw["agent"],
        "claim_token_present": bool(claim_token),
        "claim_token_redacted": bool(claim_token) and not include_secret,
        "identity_status": identity_status,
        "identity": {
            "claim_id": raw["id"],
            "actor": raw["agent"],
            "runtime_session_id": raw.get("runtime_session_id"),
            "instance_id": raw.get("instance_id"),
            "advisory": {
                "branch": raw.get("branch"),
                "worktree_path": raw.get("worktree_path"),
                "commit_sha": raw.get("commit_sha"),
                "pr_ref": raw.get("pr_ref"),
                "hostname": raw.get("hostname"),
                "pid": raw.get("pid"),
            },
        },
        "ownership_proof": {
            "type": "claim_id+claim_token",
            "claim_id": raw["id"],
            "claim_token_required": bool(raw["exclusive"]),
            "claim_token_present": bool(claim_token),
            "status": (
                "verified-capable" if claim_token else "ambiguous-legacy-claim"
            ),
        },
    }
    if include_secret:
        claim["claim_token"] = claim_token
        claim["ownership_proof"]["claim_token"] = claim_token
    return claim
