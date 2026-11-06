"""Private adapter from a verified Vuoro invocation to recipient lookup.

This module is deliberately absent from the operation catalog. The caller
must pass the invocation context produced by Vuoro's authenticated dispatch;
identity fields supplied as operation arguments are never accepted. Vuoro is
imported lazily so local/SQLite Sprintctl use has no service-runtime import
dependency.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .operator_request import RequestRefused
from .recipient_binding_pg import lookup_for_recipient_admission
from .recipient_provenance import AuthenticatedRequesterContext


def _require_unexpired_identity(identity: Any) -> None:
    expires_at = getattr(identity, "expires_at", None)
    if not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
        raise RequestRefused(
            "gateway-provenance-unavailable",
            "current verified gateway identity expiry is unavailable",
        )
    if expires_at <= datetime.now(timezone.utc):
        raise RequestRefused("gateway-identity-expired", "gateway identity expired during the lookup")


def requester_from_invocation_context(context: Any) -> AuthenticatedRequesterContext:
    """Build the private requester context from Vuoro's resolved Identity.

    `Identity` is created by the service identity resolver and passed to the
    application only after the invocation's authentication and repository
    authorization checks. Requiring the runtime class and its paired gateway
    provenance rejects workspace/static identities and lookalike test objects.
    """
    try:
        from vuoro_service.identity import Identity
    except ImportError as exc:
        raise RequestRefused(
            "gateway-identity-unavailable",
            "verified gateway identity support is unavailable",
        ) from exc

    identity = getattr(context, "identity", None)
    if type(identity) is not Identity:
        raise RequestRefused("gateway-identity-required", "verified gateway identity required")

    repo_id = getattr(context, "repo_id", None)
    authorizes_repo = getattr(identity, "authorizes_repo", None)
    if (not isinstance(repo_id, str) or not repo_id.strip()
            or not callable(authorizes_repo) or not authorizes_repo(repo_id)):
        raise RequestRefused("repo-unauthorized", "requester is not authorized for this repository")

    authorities = getattr(identity, "authorities", None)
    if not isinstance(authorities, (set, frozenset)) or "work:evidence" not in authorities:
        raise RequestRefused("authority-required", "requester requires work:evidence authority")

    issuer = getattr(identity, "gateway_issuer", None)
    subject = getattr(identity, "gateway_subject", None)
    epoch = getattr(identity, "principal_epoch", None)
    principal_id = getattr(identity, "principal_id", None)
    workspace_id = getattr(identity, "workspace_id", None)
    expires_at = getattr(identity, "expires_at", None)
    if (not isinstance(issuer, str) or not issuer.strip()
            or not isinstance(subject, str) or not subject.strip()
            or isinstance(epoch, bool) or not isinstance(epoch, int)
            or not isinstance(principal_id, str)
            or principal_id != f"{issuer}:{subject}:{epoch}"
            or not isinstance(workspace_id, str) or not workspace_id.strip()
            or not isinstance(expires_at, datetime) or expires_at.utcoffset() is None):
        raise RequestRefused(
            "gateway-provenance-unavailable",
            "current verified gateway principal provenance is unavailable",
        )
    _require_unexpired_identity(identity)

    try:
        return AuthenticatedRequesterContext(
            repo_id=repo_id,
            principal_id=principal_id,
            workspace_id=workspace_id,
            gateway_issuer=issuer,
            subject=subject,
            epoch=epoch,
            authorities=frozenset(authorities),
        )
    except (TypeError, ValueError) as exc:
        raise RequestRefused(
            "gateway-provenance-unavailable",
            "current verified gateway principal provenance is unavailable",
        ) from exc


def lookup_for_invocation(
    cur: Any,
    store: Any,
    context: Any,
    **opaque_current_coordinates: Any,
) -> dict:
    """Authorize a verified invocation, then run the private owner lookup.

    Only opaque owner coordinates belong in `opaque_current_coordinates`;
    recipient principal, instance, grant, resource and scopes are always read
    from the persisted binding by the underlying lookup.
    """
    requester = requester_from_invocation_context(context)
    result = lookup_for_recipient_admission(
        cur, store, requester, **opaque_current_coordinates
    )
    # The owner lookup can block on row locks. A credential valid at entry may
    # expire while waiting; do not release recipient data after its expiry.
    _require_unexpired_identity(context.identity)
    return result
