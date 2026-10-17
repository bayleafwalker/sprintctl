"""Run-scoped delegated credential contract (schema 27).

The owner stores only a digest of the bearer token. This module defines the
immutable context passed from the identity resolver to the owner adapter; the
owner re-reads and checks the delegation record before every allowed action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

OP_DELEGATE = "work.run.delegate-v1"
OP_REVOKE = "work.run.delegation-revoke-v1"
OP_GET = "work.run.delegation-get-v1"
INTERNAL_RESOLVE = "work.run.delegation-resolve-internal"
PUBLIC_OPERATIONS = frozenset({OP_DELEGATE, OP_REVOKE, OP_GET})
DELEGATED_OPERATIONS = frozenset({
    "work.run.resolve-v1",
    "work.run.predecessor-context-v1",
    "work.session-note.write-v1",
    "work.evidence.append-v1",
})
MAX_TTL_SECONDS = 14_400
SCHEMA_VERSION = 27
DELEGATION_ID_PATTERN = r"^dlg_[0-9A-HJKMNP-TV-Z]{26}$"
RUN_ID_PATTERN = r"^run_[0-9A-HJKMNP-TV-Z]{26}$"
TOKEN_PREFIX = "vrd_"


@dataclass(frozen=True, slots=True)
class DelegatedRunContext:
    """Typed immutable resolver context; never accepted as proof by itself."""

    delegation_id: str
    run_id: str
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedRunDelegation:
    """Owner resolution result used to construct a narrowed request identity."""

    delegation: DelegatedRunContext
    repo_id: str
    principal_id: str
    workspace_id: str
    client_id: str | None
    grant_id: str | None
    expires_at: str
    authorities: frozenset[str]

    @property
    def expires_at_datetime(self) -> datetime:
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))


class DelegationRefused(ValueError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = status


def validate_delegated_context(value: Any) -> DelegatedRunContext:
    """Validate the owner-shared type before trusting any context fields."""

    if not isinstance(value, DelegatedRunContext):
        raise DelegationRefused("delegation-invalid", "delegated context must use the owner type", 403)
    if (
        not isinstance(value.delegation_id, str)
        or re.fullmatch(DELEGATION_ID_PATTERN, value.delegation_id) is None
        or not isinstance(value.run_id, str)
        or re.fullmatch(RUN_ID_PATTERN, value.run_id) is None
        or not isinstance(value.operations, tuple)
    ):
        raise DelegationRefused("delegation-invalid", "delegated context is malformed", 403)
    if not value.operations or any(
        not isinstance(op, str) or op not in DELEGATED_OPERATIONS for op in value.operations
    ):
        raise DelegationRefused("delegation-invalid", "delegated context has an invalid operation set", 403)
    if len(value.operations) != len(set(value.operations)):
        raise DelegationRefused("delegation-invalid", "delegated context operations must be unique", 403)
    return value
