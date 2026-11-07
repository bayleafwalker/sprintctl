"""Owner-derived private lookup and receipt reads for request delivery.

These functions are intentionally absent from the WorkOperation catalog. The
private HTTP adapter supplies a currently verified gateway invocation; callers
cannot provide recipient provenance or choose repository/workspace scope.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .operator_request import RequestRefused
from .operator_request_pg import _request
from .recipient_admission_adapter import (
    _require_unexpired_identity,
    requester_from_invocation_context,
)
from .recipient_binding_pg import lookup_for_recipient_admission
from .recipient_delivery_pg import _require_delivery_storage
from .recipient_provenance import AuthenticatedRequesterContext
from .workitemcore import item_status_revision


def _authorize(context: Any, store: Any) -> AuthenticatedRequesterContext:
    requester = requester_from_invocation_context(context)
    if requester.repo_id != store.repo_id or "work:evidence" not in requester.authorities:
        raise RequestRefused("authority-required", "current requester requires work:evidence authority")
    return requester


def resolve_request_for_delivery(
    cur: Any, store: Any, context: Any, *, request_id: str, binding_id: str,
) -> dict:
    """Resolve one owner-known binding for an authenticated request owner.

    A request lock serializes this resolution with dependant/binding changes.
    Binding, work, revision and recipient provenance are read from owner rows;
    the caller supplies only the opaque binding selector. This private exact
    lookup never enumerates a request's recipient bindings.
    """
    requester = _authorize(context, store)
    _require_delivery_storage(cur)
    if (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128
            or not isinstance(binding_id, str) or not binding_id.strip() or len(binding_id) > 128):
        raise RequestRefused("not-found", "request unavailable")
    request = _request(cur, store, requester.workspace_id, request_id, lock=True)
    if (request is None or request["requester_principal_id"] != requester.principal_id
            or request["state"] != "resolved"):
        raise RequestRefused("not-found", "request unavailable")

    revision = int(request["revision"])
    packet = request["packet"]
    if isinstance(packet, str):
        try:
            packet = json.loads(packet)
        except (TypeError, ValueError):
            raise RequestRefused("request-freshness-unavailable", "request freshness unavailable") from None
    try:
        expiry_text = packet["blocker"]["fresh_until"]
        expiry = datetime.fromisoformat(expiry_text)
        if expiry.tzinfo is None:
            raise ValueError("timezone required")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RequestRefused("request-freshness-unavailable", "request freshness unavailable") from None
    cur.execute(
        "SELECT action,details FROM operator_request_event WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s AND revision=%s",
        (store.repo_id, requester.workspace_id, request_id, revision),
    )
    event = cur.fetchone()
    if (event is None or event["action"] != "resolved-human-attestation"
            or not isinstance(event["details"], dict)
            or event["details"].get("completion_basis") != "human-attestation"
            or event["details"].get("protected_effect_authorized") is not False):
        raise RequestRefused("resolution-evidence-unavailable", "resolution evidence unavailable")
    cur.execute("SELECT clock_timestamp() AS now")
    if cur.fetchone()["now"] >= expiry.astimezone(UTC):
        raise RequestRefused("not-found", "request unavailable")

    cur.execute(
        "SELECT b.work_item_id,b.binding_id,b.revision AS binding_revision "
        "FROM operator_request_recipient_binding b JOIN operator_request_dependant d "
        "ON d.repo_id=b.repo_id AND d.workspace_id=b.workspace_id AND d.request_id=b.request_id "
        "AND d.work_item_id=b.work_item_id AND d.state='active' "
        "WHERE b.repo_id=%s AND b.workspace_id=%s AND b.request_id=%s "
        "AND b.binding_id=%s AND b.withdrawn_at IS NULL FOR UPDATE OF b",
        (store.repo_id, requester.workspace_id, request_id, binding_id),
    )
    binding = cur.fetchone()
    if binding is None:
        raise RequestRefused("not-found", "request unavailable")
    work_item_id = int(binding["work_item_id"])
    cur.execute(
        "SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s",
        (store.repo_id, work_item_id),
    )
    work = cur.fetchone()
    if work is None:
        raise RequestRefused("work-not-found", "owner work row is unavailable")
    coords = {
        "request_id": request_id,
        "request_revision": revision,
        "resolution_event_revision": revision,
        "work_item_id": work_item_id,
        "work_revision": item_status_revision(work),
        "binding_id": binding["binding_id"],
        "binding_revision": int(binding["binding_revision"]),
    }
    owner = lookup_for_recipient_admission(cur, store, requester, **coords)

    cur.execute("SELECT clock_timestamp() AS now")
    if cur.fetchone()["now"] >= expiry.astimezone(UTC):
        raise RequestRefused("not-found", "request unavailable")
    _require_unexpired_identity(context.identity)
    return {
        "schema": "operator-request-delivery-read/v1",
        "request_id": request_id,
        "resolution_revision": revision,
        "coords": coords,
        "recipient": owner["recipient"],
    }


def read_request_delivery_receipts(
    cur: Any, store: Any, context: Any, *, request_id: str, prior_invocation_id: str,
) -> dict:
    """Read opaque historical receipts for this authenticated request owner.

    Historical lookup does not require present recipient consent or work
    freshness. It still requires a live sender identity, repo authority, and
    current owner-row request ownership. Recipient identity and admission data
    are deliberately omitted from the response.
    """
    requester = _authorize(context, store)
    _require_delivery_storage(cur)
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise RequestRefused("not-found", "request unavailable")
    request = _request(cur, store, requester.workspace_id, request_id)
    if request is None or request["requester_principal_id"] != requester.principal_id:
        raise RequestRefused("not-found", "request unavailable")

    if (not isinstance(prior_invocation_id, str) or not prior_invocation_id.strip()
            or len(prior_invocation_id) > 128 or "\x00" in prior_invocation_id):
        raise RequestRefused("not-found", "receipt unavailable")
    cur.execute(
        "SELECT d.message_id,d.resolution_revision,d.work_item_id,d.work_status_token,d.binding_id,"
        "d.binding_revision,d.envelope_digest,d.recorded_at "
        "FROM operator_request_delivery_key k JOIN operator_request_delivery d "
        "ON d.repo_id=k.repo_id AND d.workspace_id=k.workspace_id AND d.message_id=k.message_id "
        "WHERE k.repo_id=%s AND k.workspace_id=%s AND k.sender_principal_id=%s "
        "AND k.idempotency_key=%s AND d.request_id=%s AND d.sender_principal_id=%s",
        (store.repo_id, requester.workspace_id, requester.principal_id,
         f"gateway:{prior_invocation_id}", request_id, requester.principal_id),
    )
    row = cur.fetchone()
    _require_unexpired_identity(context.identity)
    return {
        "schema": "operator-request-delivery-read/v1",
        "request_id": request_id,
        "receipt": None if row is None else {
            "message_id": row["message_id"],
            "resolution_revision": row["resolution_revision"],
            "work_item_id": row["work_item_id"],
            "work_revision": row["work_status_token"],
            "binding_id": row["binding_id"],
            "binding_revision": row["binding_revision"],
            "envelope_digest": row["envelope_digest"],
            "recorded_at": row["recorded_at"].isoformat(),
        },
    }
