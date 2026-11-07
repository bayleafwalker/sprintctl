"""Default-off private request resolution and delivery receipt read adapter.

The routes are not mounted by default or present in the public operation
catalog. They use the ordinary gateway-signed identity plus a separate
read-only-purpose audience and a deployment-owned private ingress guard.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .recipient_delivery_receiver import (
    _json_object_without_duplicates,
    _read_bounded_body,
)

READ_SCHEMA = "operator-request-delivery-read/v1"
READ_AUDIENCE = "sprintctl-operator-request-delivery-read/v1"
RESOLVE_ROUTE = "/internal/operator-request-delivery/v1/resolve"
RECEIPT_ROUTE = "/internal/operator-request-delivery/v1/receipts"
_BODY_FIELDS = frozenset({"schema", "invocation_id", "repo_id", "request_id"})


@dataclass(frozen=True, slots=True)
class PrivateDeliveryReadConfig:
    """Opt-in gateway trust and owned connection lease for private reads."""

    enabled: bool = False
    gateway_public_key_file: Path | None = None
    gateway_issuer: str | None = None
    gateway_environment: str | None = None
    expected_workspace_id: str | None = None
    allowed_repo_ids: frozenset[str] | None = None
    gateway_key_id: str = "gateway-2026-01"
    replay_not_before: float | None = None
    private_ingress_guard: Callable[[], bool] | None = None
    # Must authenticate the independent Cloud admission service caller; this
    # keeps persisted recipient grant provenance off the human requester API.
    trusted_cloud_adapter_guard: Callable[[Request], bool] | None = None
    store_lease_for_repo: Callable[[str], Any] | None = None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"schema": READ_SCHEMA, "error": code})


def _valid_payload(payload: Any, operation: str) -> bool:
    fields = _BODY_FIELDS | ({"prior_invocation_id"} if operation == "receipts" else {"binding_id"})
    if not isinstance(payload, dict) or frozenset(payload) != fields:
        return False
    if payload["schema"] != READ_SCHEMA:
        return False
    limits = [("invocation_id", 128), ("repo_id", 2048), ("request_id", 128)]
    if operation == "resolve":
        limits.append(("binding_id", 128))
    for key, limit in limits:
        value = payload[key]
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > limit or "\x00" in value):
            return False
    if operation == "receipts":
        prior = payload["prior_invocation_id"]
        if (not isinstance(prior, str) or not prior or prior != prior.strip()
                or len(prior) > 128 or "\x00" in prior):
            return False
    return True


def create_private_delivery_read_router(config: PrivateDeliveryReadConfig) -> APIRouter:
    """Build no routes while disabled; otherwise bind routes to verified identity."""
    router = APIRouter()
    if not config.enabled:
        return router
    if (not isinstance(config.gateway_public_key_file, Path)
            or not isinstance(config.gateway_issuer, str) or not config.gateway_issuer.strip()
            or not isinstance(config.gateway_environment, str) or not config.gateway_environment.strip()
            or not isinstance(config.expected_workspace_id, str) or not config.expected_workspace_id.strip()
            or not isinstance(config.allowed_repo_ids, frozenset) or not config.allowed_repo_ids
            or not isinstance(config.gateway_key_id, str) or not config.gateway_key_id.strip()
            or isinstance(config.replay_not_before, bool)
            or not isinstance(config.replay_not_before, (int, float))
            or not callable(config.private_ingress_guard)
            or not callable(config.trusted_cloud_adapter_guard)
            or not callable(config.store_lease_for_repo)):
        raise ValueError("private delivery read requires exact gateway trust, private service guards, and owner")

    from vuoro_service.gateway_identity import GatewayAssertionIdentityResolver
    resolver = GatewayAssertionIdentityResolver.from_file(
        config.gateway_public_key_file,
        issuer=config.gateway_issuer,
        audience=READ_AUDIENCE,
        environment=config.gateway_environment,
        expected_workspace_id=config.expected_workspace_id,
        allowed_repo_ids=config.allowed_repo_ids,
        key_id=config.gateway_key_id,
        replay_not_before=float(config.replay_not_before),
    )

    async def invoke(request: Request, operation: str) -> JSONResponse:
        try:
            allowed = config.private_ingress_guard()
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:  # noqa: BLE001 - private policy failure is fail-closed.
            allowed = False
        if allowed is not True:
            return _error(404, "not-found")
        try:
            raw = await _read_bounded_body(request)
        except ValueError:
            return _error(400, "invalid-request")
        if raw is None:
            return _error(413, "request-too-large")
        request.state.vuoro_bounded_body = raw
        try:
            trusted_adapter = config.trusted_cloud_adapter_guard(request)
            if inspect.isawaitable(trusted_adapter):
                trusted_adapter = await trusted_adapter
        except Exception:  # noqa: BLE001 - do not leak service-auth details.
            trusted_adapter = False
        if trusted_adapter is not True:
            return _error(404, "not-found")
        try:
            payload = json.loads(raw, object_pairs_hook=_json_object_without_duplicates)
        except (ValueError, UnicodeError):
            return _error(400, "invalid-request")
        if not _valid_payload(payload, operation):
            return _error(400, "invalid-request")

        request.state.vuoro_invocation_request_id = payload["invocation_id"]
        request.state.vuoro_invocation_body = raw
        try:
            import jwt
            token = request.headers.get("x-vuoro-identity")
            claims = jwt.decode(token, options={"verify_signature": False})
            if claims.get("aud") != READ_AUDIENCE or not isinstance(claims.get("aud"), str):
                return _error(401, "identity-required")
            identity = resolver(request)
            if inspect.isawaitable(identity):
                identity = await identity
        except Exception:  # noqa: BLE001 - generic identity refusal.
            return _error(401, "identity-required")
        from vuoro_service.identity import Identity, InvocationContext
        if type(identity) is not Identity:
            return _error(401, "identity-required")
        repo_id = payload["repo_id"]
        if not identity.authorizes_repo(repo_id):
            return _error(403, "repo-unauthorized")
        if "work:evidence" not in identity.authorities:
            return _error(403, "authority-required")

        context = InvocationContext(
            identity=identity,
            request_id=payload["invocation_id"],
            basis_revision=None,
            catalog_revision=READ_SCHEMA,
            idempotency_requirement="none",
            idempotency_key=None,
            repo_id=repo_id,
        )
        from .operator_request import RequestRefused
        try:
            lease = config.store_lease_for_repo(repo_id)
            if inspect.isawaitable(lease):
                lease = await lease
            if not callable(getattr(lease, "__enter__", None)):
                return _unknown()
            with lease as store:
                if getattr(getattr(store.conn.info, "transaction_status", None), "name", None) != "IDLE":
                    return _unknown()
                with store.conn.transaction(), store.conn.cursor() as cur:
                    from . import recipient_delivery_read_pg as owner
                    reader = (owner.resolve_request_for_delivery if operation == "resolve"
                              else owner.read_request_delivery_receipts)
                    kwargs = {"request_id": payload["request_id"]}
                    if operation == "receipts":
                        kwargs["prior_invocation_id"] = payload["prior_invocation_id"]
                    else:
                        kwargs["binding_id"] = payload["binding_id"]
                    result = reader(cur, store, context, **kwargs)
        except RequestRefused as exc:
            # Keep owner privacy while distinguishing retryable owner-health
            # conditions from absent/foreign/stale authorization predicates.
            # No exception detail is reflected to the caller.
            code = getattr(exc, "code", "")
            if code == "gateway-identity-expired":
                return _error(401, "identity-required")
            if code in {
                "not-found", "resolution-not-current", "request-needs-refresh",
                "work-needs-refresh", "binding-unavailable", "dependant-not-active",
                "instance-retired",
            }:
                return _error(404, "request-unavailable")
            return _unknown()
        except Exception:  # noqa: BLE001 - owner/storage outcome must be opaque.
            # A failed owner transaction is an unknown read; callers can retry
            # with a fresh signed invocation without learning DB details.
            return _unknown()
        return JSONResponse(status_code=200, content=result)

    @router.post(RESOLVE_ROUTE, include_in_schema=False)
    async def resolve(request: Request) -> JSONResponse:
        return await invoke(request, "resolve")

    @router.post(RECEIPT_ROUTE, include_in_schema=False)
    async def receipts(request: Request) -> JSONResponse:
        return await invoke(request, "receipts")

    return router


def _unknown() -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "schema": READ_SCHEMA, "error": "delivery-read-unknown", "retryable": True,
    })
