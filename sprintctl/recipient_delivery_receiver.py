"""Default-off private HTTP receiver for OA-01 resolution delivery.

This is a transport adapter around the private owner transaction in
``recipient_delivery_pg``. It is not part of the public WorkOperation catalog
and is not mounted by Sprintctl or Vuoro by default.

The requester's identity is the existing gateway-signed ``X-Vuoro-Identity``
assertion, verified for the dedicated receiver audience by Vuoro's
``GatewayAssertionIdentityResolver``. The receiver constructs an
``InvocationContext`` only from the resolved runtime ``Identity``. Cloud's
recipient-admission assertion remains a second, narrower statement about the
current recipient binding; it does not authenticate the sender.

Enabling this router requires all three at the composition point:

* the dedicated gateway audience ``sprintctl-operator-request-delivery/v1``;
* a private ingress guard (network policy / trusted proxy) supplied by the
  deployment owner; and
* trusted Cloud admission verification keys and issuer configuration.

No production audience, route, key, network policy, or credential is created
by this module. A fresh gateway assertion is required per HTTP attempt; the
gateway resolver enforces its expiry and replay cache, while the owner ledger
enforces idempotency across retries.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from vuoro_service.identity import Identity, InvocationContext
from .workitemcore import validate_item_status_revision

RECEIVER_SCHEMA = "operator-request-delivery/v1"
RECEIVER_AUDIENCE = "sprintctl-operator-request-delivery/v1"
ROUTE = "/internal/operator-request-delivery/v1"
_COORDINATE_FIELDS = frozenset({
    "request_id", "request_revision", "resolution_event_revision", "work_item_id",
    "work_revision", "binding_id", "binding_revision",
})
_BODY_FIELDS = frozenset({
    "schema", "invocation_id", "request_id", "repo_id", "assertion", "coords",
})
_MAX_BODY_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class PrivateDeliveryReceiverConfig:
    """Explicit, default-off dependencies for the private owner receiver."""

    enabled: bool = False
    gateway_public_key_file: Path | None = None
    gateway_issuer: str | None = None
    gateway_environment: str | None = None
    expected_workspace_id: str | None = None
    allowed_repo_ids: frozenset[str] | None = None
    gateway_key_id: str = "gateway-2026-01"
    replay_not_before: float | None = None
    # Process-owned policy only; never pass request data into this gate.
    private_ingress_guard: Callable[[], bool] | None = None
    # Must return an owned per-request connection lease context manager. The
    # receiver never closes or rolls back a borrowed/shared application store.
    store_lease_for_repo: Callable[[str], Any] | None = None
    admission_keys: Mapping[str, Any] | None = None
    admission_issuer: str | None = None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"schema": RECEIVER_SCHEMA, "error": code})


def _valid_payload(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _BODY_FIELDS:
        return False
    if value["schema"] != RECEIVER_SCHEMA:
        return False
    for name, limit in (("invocation_id", 128), ("request_id", 128),
                        ("repo_id", 2048), ("assertion", 16_384)):
        field = value[name]
        if (not isinstance(field, str) or not field or field != field.strip()
                or len(field) > limit or "\x00" in field):
            return False
    coords = value["coords"]
    if not isinstance(coords, dict) or set(coords) != _COORDINATE_FIELDS:
        return False
    if coords["request_id"] != value["request_id"]:
        return False
    if any(not isinstance(coords[name], str) or not coords[name] or len(coords[name]) > 2048
           for name in ("request_id", "work_revision", "binding_id")):
        return False
    if any(isinstance(coords[name], bool) or not isinstance(coords[name], int) or coords[name] < 1
           for name in ("request_revision", "resolution_event_revision", "work_item_id", "binding_revision")):
        return False
    try:
        validate_item_status_revision(coords["work_revision"])
    except (TypeError, ValueError):
        return False
    return True


def _json_object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


async def _read_bounded_body(request: Request) -> bytes | None:
    """Read incrementally; do not trust Content-Length or buffer an unbounded body."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise ValueError("invalid content length") from None
        if size < 0:
            raise ValueError("invalid content length")
        if size > _MAX_BODY_BYTES:
            return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)


def create_private_delivery_router(config: PrivateDeliveryReceiverConfig) -> APIRouter:
    """Build an opt-in router; disabled configuration registers no path."""
    router = APIRouter()
    if not config.enabled:
        return router
    if (not isinstance(config.gateway_public_key_file, Path)
            or not config.gateway_issuer or not config.gateway_issuer.strip()
            or not config.gateway_environment or not config.gateway_environment.strip()
            or not config.expected_workspace_id or not config.expected_workspace_id.strip()
            or not isinstance(config.allowed_repo_ids, frozenset) or not config.allowed_repo_ids
            or not isinstance(config.gateway_key_id, str) or not config.gateway_key_id.strip()
            or isinstance(config.replay_not_before, bool)
            or not isinstance(config.replay_not_before, (int, float))
            or not callable(config.private_ingress_guard)
            or not callable(config.store_lease_for_repo) or not config.admission_keys
            or not isinstance(config.admission_issuer, str) or not config.admission_issuer.strip()):
        raise ValueError("private delivery receiver requires exact gateway trust, ingress guard, owner, and issuer keys")

    # Construct the existing gateway verifier here with the receiver-specific
    # audience. A generic resolver injection could accidentally accept a
    # vuoro-service token intended for another ingress.
    from vuoro_service.gateway_identity import GatewayAssertionIdentityResolver
    identity_resolver = GatewayAssertionIdentityResolver.from_file(
        config.gateway_public_key_file,
        issuer=config.gateway_issuer,
        audience=RECEIVER_AUDIENCE,
        environment=config.gateway_environment,
        expected_workspace_id=config.expected_workspace_id,
        allowed_repo_ids=config.allowed_repo_ids,
        key_id=config.gateway_key_id,
        replay_not_before=float(config.replay_not_before),
    )

    @router.post(ROUTE, include_in_schema=False)
    async def receive(request: Request) -> JSONResponse:
        # Filter the private network boundary before parsing request content or
        # evaluating caller credentials. The guard must be supplied by the
        # deployment composition and fail closed.
        try:
            allowed = config.private_ingress_guard()
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:  # fail closed without reflecting proxy details
            allowed = False
        if allowed is not True:
            return _error(404, "not-found")

        try:
            raw = await _read_bounded_body(request)
        except ValueError:
            return _error(400, "invalid-request")
        if raw is None:
            return _error(413, "request-too-large")
        try:
            payload = json.loads(raw, object_pairs_hook=_json_object_without_duplicates)
        except (ValueError, UnicodeError):
            return _error(400, "invalid-request")
        if not _valid_payload(payload):
            return _error(400, "invalid-request")

        # Reuse the gateway's existing signed identity format and resolver.
        # These state values mirror the normal Vuoro dispatch envelope checks;
        # the resolver still requires the signed request_id to match. The
        # operator request ID remains a separate owner coordinate.
        request.state.vuoro_invocation_request_id = payload["invocation_id"]
        request.state.vuoro_invocation_body = raw
        try:
            # PyJWT's normal audience verification accepts a list containing
            # the requested audience. This private ingress requires an
            # exclusive scalar audience so the identity assertion cannot be
            # replayed at another Vuoro surface.
            import jwt
            token = request.headers.get("x-vuoro-identity")
            claims = jwt.decode(token, options={"verify_signature": False})
            if claims.get("aud") != RECEIVER_AUDIENCE or not isinstance(claims.get("aud"), str):
                return _error(401, "identity-required")
            identity = identity_resolver(request)
            if inspect.isawaitable(identity):
                identity = await identity
        except Exception:  # generic identity refusal; no bearer details
            return _error(401, "identity-required")
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
            catalog_revision=RECEIVER_SCHEMA,
            idempotency_requirement="required",
            # Durable replay key derived from the gateway-signed request id.
            # The owner's append-only key ledger serializes duplicate submits
            # across receiver replicas/restarts; caller input cannot choose a
            # fresh key while replaying the same signed identity assertion.
            idempotency_key=f"gateway:{payload['invocation_id']}",
            repo_id=repo_id,
        )
        from .operator_request import RequestRefused
        from vuoro_service.recipient_admission import AdmissionVerificationError

        try:
            lease = config.store_lease_for_repo(repo_id)
            if inspect.isawaitable(lease):
                lease = await lease
            if not callable(getattr(lease, "__enter__", None)):
                return _outcome_unknown()
            with lease as store:
                transaction_status = getattr(store.conn.info, "transaction_status", None)
                if getattr(transaction_status, "name", None) != "IDLE":
                    # The provider's lease owns cleanup. The receiver does not
                    # rollback or close a potentially shared connection.
                    return _outcome_unknown()
                from .recipient_delivery_pg import deliver
                with store.conn.transaction():
                    with store.conn.cursor() as cur:
                        try:
                            result = deliver(
                                cur,
                                store,
                                context,
                                idempotency_key=context.idempotency_key,
                                assertion=payload["assertion"],
                                keys=config.admission_keys,
                                issuer=config.admission_issuer,
                                coords=payload["coords"],
                            )
                        except (RequestRefused, AdmissionVerificationError, ValueError) as exc:
                            # Only typed domain refusals raised by the owner
                            # call are terminal. Transaction/commit failures
                            # remain outside this conversion and are unknown.
                            raise _KnownOwnerRefusal() from exc
        except _KnownOwnerRefusal:
            return _error(409, "delivery-refused")
        except Exception:
            # Storage, connection, and commit exceptions are uncertain. A
            # caller must retry through a fresh signed invocation; the durable
            # owner key derived from invocation_id converges any prior commit.
            return _outcome_unknown()
        return JSONResponse(status_code=200, content={"schema": RECEIVER_SCHEMA, **result})

    return router


class _KnownOwnerRefusal(Exception):
    """Typed failure raised only before entering the owner transaction commit."""


def _outcome_unknown() -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "schema": RECEIVER_SCHEMA,
        "error": "delivery-outcome-unknown",
        "retryable": True,
    })
