"""Private data boundary for a future verified Cloud recipient invocation.

This type does not verify credentials. Only a trusted invocation adapter may
construct it after verifying that every field belongs to the same assertion.
There is deliberately no conversion from public operation arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


def _validate_identity(value: object) -> None:
    for name in ("principal_id", "workspace_id", "gateway_issuer", "subject"):
        field = getattr(value, name)
        if not isinstance(field, str) or not field.strip():
            raise ValueError(f"{name} must be a nonempty string")
    if re.fullmatch(r"[A-Za-z0-9._-]+", value.subject) is None:
        raise ValueError("subject must match gateway subject grammar")
    if isinstance(value.epoch, bool) or not isinstance(value.epoch, int) or not 0 <= value.epoch <= 2**53 - 1:
        raise ValueError("epoch must be a nonnegative safe integer")
    if value.principal_id != f"{value.gateway_issuer}:{value.subject}:{value.epoch}":
        raise ValueError("principal_id must match gateway issuer, subject, and epoch")


@dataclass(frozen=True, slots=True)
class AuthenticatedOwnerIdentity:
    """Current identity attested by a trusted adapter, independently of consent scopes."""

    principal_id: str
    workspace_id: str
    gateway_issuer: str
    subject: str
    epoch: int

    def __post_init__(self) -> None:
        _validate_identity(self)


@dataclass(frozen=True, slots=True)
class RecipientProvenance:
    principal_id: str
    workspace_id: str
    gateway_issuer: str
    subject: str
    epoch: int
    client_id: str
    grant_id: str
    resource: str
    token_scopes: frozenset[str]
    effective_scopes: frozenset[str]

    def __post_init__(self) -> None:
        _validate_identity(self)
        for name in ("client_id", "grant_id", "resource"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        for name in ("token_scopes", "effective_scopes"):
            scopes = getattr(self, name)
            if not isinstance(scopes, frozenset) or any(
                not isinstance(scope, str) or not scope.strip() for scope in scopes
            ):
                raise ValueError(f"{name} must be a frozenset of nonempty strings")
        if not {"vuoro:work.read", "vuoro:evidence.record"} <= self.token_scopes:
            raise ValueError("binding requires original OAuth scopes")
        if not {"work:read", "work:evidence"} <= self.effective_scopes:
            raise ValueError("binding requires admitted work:read and work:evidence")
