"""Private transactional recipient-resolution delivery and verification reads.

There is intentionally no operation-catalog entry for these functions.  The
caller must be the private verified-gateway adapter and owns commit/retry.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Mapping

from .agent_inbox_pg import _allocate_inbox_seq
from .agent_inbox import InboxRefused, canonical_bytes, validate_declaration_v2
from .agent_capability_pg import latest_for_instance
from .operator_request import RequestRefused
from .operator_request_pg import _request
from .recipient_binding_pg import lookup_for_recipient_admission
from .recipient_provenance import AuthenticatedRequesterContext
from .recipient_admission_adapter import _require_unexpired_identity, requester_from_invocation_context
from .agent_inbox_pg import owner_now

NOTIFICATION_SCHEMA = "recipient-resolution-notification/v1"
CAPABILITY_SCHEMA = "agent-declaration/v2"
CAPABILITY_KIND = "request-resolution/v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _message_id() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = secrets.randbits(128)
    out = ""
    for _ in range(26):
        out = alphabet[value & 31] + out
        value >>= 5
    return "msg_" + out


def _require_requester(requester: AuthenticatedRequesterContext, store: Any) -> None:
    if not isinstance(requester, AuthenticatedRequesterContext):
        raise TypeError("trusted authenticated requester context required")
    if (requester.repo_id != store.repo_id or "work:evidence" not in requester.authorities):
        raise RequestRefused("authority-required", "current requester requires work:evidence authority")


def _require_delivery_storage(cur: Any) -> None:
    from .recipient_delivery_schema import inspect_catalog
    if not inspect_catalog(cur)["available"]:
        raise RequestRefused("delivery-storage-unavailable", "recipient delivery storage is unavailable")


def _require_capability(cur: Any, store: Any, workspace: str, instance: str) -> None:
    from .agent_capability_schema import inspect_catalog
    if not inspect_catalog(cur)["available"]:
        raise RequestRefused("recipient-unsupported", "recipient capability storage is unavailable")
    row = latest_for_instance(cur, store, workspace, instance)
    if (row is None or row["retired_at"] is not None
            or row["capability_revision"] is None
            or row["current_base_declaration_revision"] is None
            or row["base_declaration_revision"] != row["current_base_declaration_revision"]):
        raise RequestRefused("recipient-unsupported", "recipient has no current v2 capability")
    declaration = row["capability"]
    if isinstance(declaration, str):
        try:
            declaration = json.loads(declaration)
        except ValueError:
            declaration = None
    try:
        declaration = validate_declaration_v2(declaration)
    except InboxRefused:
        declaration = None
    kinds = declaration.get("accepted_kinds") if isinstance(declaration, dict) else None
    if (not isinstance(declaration, dict) or declaration.get("schema") != CAPABILITY_SCHEMA
            or hashlib.sha256(canonical_bytes(declaration)).hexdigest() != row["capability_digest"]
            or CAPABILITY_KIND not in kinds):
        raise RequestRefused("recipient-unsupported", "recipient does not advertise request-resolution/v1")


def _recipient_read_identity(context: Any, store: Any) -> tuple[str, str]:
    """Derive the reader from a currently authenticated Vuoro invocation."""
    try:
        from vuoro_service.identity import Identity
    except ImportError as exc:
        raise RequestRefused("gateway-identity-unavailable", "verified gateway identity support is unavailable") from exc
    identity = getattr(context, "identity", None)
    repo_id = getattr(context, "repo_id", None)
    authorizes_repo = getattr(identity, "authorizes_repo", None)
    if (type(identity) is not Identity or repo_id != store.repo_id
            or not callable(authorizes_repo) or not authorizes_repo(repo_id)):
        raise RequestRefused("repo-unauthorized", "recipient is not authorized for this repository")
    if not isinstance(identity.authorities, (set, frozenset)) or "work:read" not in identity.authorities:
        raise RequestRefused("authority-required", "recipient verification requires work:read authority")
    issuer, subject, epoch = identity.gateway_issuer, identity.gateway_subject, identity.principal_epoch
    scopes = identity.oauth_scopes
    if (not isinstance(identity.principal_id, str) or not identity.principal_id
            or not isinstance(identity.workspace_id, str) or not identity.workspace_id
            or not isinstance(issuer, str) or not issuer.strip()
            or not isinstance(subject, str) or not subject.strip()
            or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0
            or identity.principal_id != f"{issuer}:{subject}:{epoch}"
            or not isinstance(identity.client_id, str) or not identity.client_id.strip()
            or not isinstance(identity.grant_id, str) or not identity.grant_id.strip()
            or not isinstance(identity.oauth_resource, str) or not identity.oauth_resource.strip()
            or not isinstance(scopes, frozenset)
            # This is a recipient owner read: require its current Cloud
            # work.read scope; evidence-record is the sender's write authority.
            or "vuoro:work.read" not in scopes):
        raise RequestRefused("gateway-provenance-unavailable", "verified gateway principal is unavailable")
    _require_unexpired_identity(identity)
    return identity.principal_id, identity.workspace_id


def _expected_binding(requester: AuthenticatedRequesterContext, owner: Mapping[str, Any]):
    from vuoro_service.recipient_admission import AdmissionBinding, OPERATION
    recipient = owner["recipient"]
    return AdmissionBinding(
        operation=OPERATION, caller=requester.principal_id,
        request=owner["request_id"], resolution=str(owner["resolution_event_revision"]),
        work=str(owner["work_item_id"]), binding=owner["binding_id"],
        recipient_principal=recipient["principal_id"], recipient_instance=recipient["instance_id"],
        workspace=owner["workspace_id"], repo=owner["repo_id"],
        gateway_issuer=recipient["gateway_issuer"], subject=recipient["subject"],
        grant=recipient["grant_id"], client=recipient["client_id"], epoch=recipient["epoch"],
        resource=recipient["resource"], scopes=tuple(recipient["token_scopes"]),
        effective_scope="work:read",
    )


def _recheck_admission(token: str, keys: Mapping[str, Any], issuer: str,
                       expected: Any, admission: Any, cur: Any) -> None:
    """Reverify signature, expiry, and authority-read age after a lock wait."""
    from vuoro_service.recipient_admission import AUDIENCE, verify_recipient_admission
    current = verify_recipient_admission(
        token, keys=keys, issuer=issuer, audience=AUDIENCE, expected=expected,
        now=owner_now(cur).timestamp(),
    )
    if current.jti != admission.jti:
        raise RequestRefused("recipient-admission-invalid", "recipient admission changed during delivery")


def deliver(cur: Any, store: Any, invocation_context: Any, *,
            idempotency_key: str, assertion: str, keys: Mapping[str, Any], issuer: str,
            coords: Mapping[str, Any]) -> dict:
    """Verify owner-derived admission and atomically record one notification.

    ``assertion`` is consumed only in memory.  Auth/ownership runs before any
    idempotency result is returned.  Caller commits this transaction.
    """
    requester = requester_from_invocation_context(invocation_context)
    _require_requester(requester, store)
    _require_delivery_storage(cur)
    _require_unexpired_identity(invocation_context.identity)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
        raise ValueError("idempotency_key must be 1..256 characters")
    workspace, request_id = requester.workspace_id, coords["request_id"]
    # Serialize one sender key across requests before inspecting its historical
    # receipt. The immutable request lock still precedes binding/instance/work.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (json.dumps([store.repo_id, workspace, requester.principal_id, idempotency_key]),))
    request = _request(cur, store, workspace, request_id, lock=True)
    if request is None or request["requester_principal_id"] != requester.principal_id:
        raise RequestRefused("not-found", "request not found")
    _require_unexpired_identity(invocation_context.identity)
    payload = {"request_id": request_id, **{k: coords[k] for k in (
        "request_revision", "resolution_event_revision", "work_item_id", "work_revision",
        "binding_id", "binding_revision")}}
    request_digest = hashlib.sha256(_canonical(payload)).hexdigest()
    cur.execute("SELECT request_digest,message_id FROM operator_request_delivery_key "
                "WHERE repo_id=%s AND workspace_id=%s AND sender_principal_id=%s AND idempotency_key=%s FOR UPDATE",
                (store.repo_id, workspace, requester.principal_id, idempotency_key))
    prior = cur.fetchone()
    if prior is not None:
        if prior["request_digest"] != request_digest:
            raise RequestRefused("idempotency-conflict", "idempotency key used with changed request")
        _require_unexpired_identity(invocation_context.identity)
        return {"message_id": prior["message_id"], "historical_replay": True}

    owner = lookup_for_recipient_admission(cur, store, requester, **coords)
    _require_capability(cur, store, workspace, owner["recipient"]["instance_id"])
    _require_unexpired_identity(invocation_context.identity)
    from vuoro_service.recipient_admission import AUDIENCE, verify_recipient_admission
    expected = _expected_binding(requester, owner)
    admission = verify_recipient_admission(
        assertion, keys=keys, issuer=issuer, audience=AUDIENCE, expected=expected,
        now=owner_now(cur).timestamp(),
    )
    _require_unexpired_identity(invocation_context.identity)
    logical = (store.repo_id, workspace, request_id, coords["resolution_event_revision"],
               coords["work_item_id"], coords["work_revision"], owner["recipient"]["instance_id"],
               coords["binding_id"], coords["binding_revision"])
    cur.execute(
        "SELECT message_id FROM operator_request_delivery WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s AND resolution_revision=%s AND work_item_id=%s AND work_status_token=%s "
        "AND recipient_instance_id=%s AND binding_id=%s AND binding_revision=%s FOR SHARE", logical,
    )
    converged = cur.fetchone()
    _require_unexpired_identity(invocation_context.identity)
    _recheck_admission(assertion, keys, issuer, expected, admission, cur)
    if converged is not None:
        message_id = converged["message_id"]
        cur.execute("INSERT INTO operator_request_delivery_key VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (store.repo_id, workspace, requester.principal_id, idempotency_key,
                     request_digest, message_id, owner_now(cur)))
        _require_unexpired_identity(invocation_context.identity)
        _recheck_admission(assertion, keys, issuer, expected, admission, cur)
        return {"message_id": message_id, "historical_replay": False, "converged": True}

    message_id = _message_id()
    envelope = {
        "schema": NOTIFICATION_SCHEMA, "message_id": message_id,
        "request_id": request_id, "resolution_revision": coords["resolution_event_revision"],
        "work_item_id": coords["work_item_id"], "work_status_token": coords["work_revision"],
        "binding_id": coords["binding_id"], "binding_revision": coords["binding_revision"],
    }
    encoded = _canonical(envelope)
    digest = hashlib.sha256(encoded).hexdigest()
    inbox_seq = _allocate_inbox_seq(cur, store, workspace)
    _require_unexpired_identity(invocation_context.identity)
    _recheck_admission(assertion, keys, issuer, expected, admission, cur)
    persisted = owner_now(cur)
    cur.execute(
        "INSERT INTO agent_message(repo_id,workspace_id,message_id,inbox_seq,sender_principal_id,"
        "recipient_principal_id,recipient_instance_id,envelope,envelope_digest,persisted_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (store.repo_id, workspace, message_id, inbox_seq, requester.principal_id,
         owner["recipient"]["principal_id"], owner["recipient"]["instance_id"],
         encoded.decode(), digest, persisted),
    )
    cur.execute(
        "INSERT INTO operator_request_delivery(repo_id,workspace_id,request_id,resolution_revision,"
        "work_item_id,work_status_token,recipient_instance_id,binding_id,binding_revision,"
        "sender_principal_id,admission_issuer,admission_jti,message_id,envelope_digest,recorded_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (*logical, requester.principal_id, issuer, admission.jti, message_id, digest, persisted),
    )
    # A conflicting JTI can make this insert wait. If the competing owner
    # transaction rolls back, this insert may proceed; do not commit a write
    # after the gateway credential expired during that wait.
    _require_unexpired_identity(invocation_context.identity)
    _recheck_admission(assertion, keys, issuer, expected, admission, cur)
    cur.execute("INSERT INTO operator_request_delivery_key VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (store.repo_id, workspace, requester.principal_id, idempotency_key,
                 request_digest, message_id, persisted))
    _require_unexpired_identity(invocation_context.identity)
    _recheck_admission(assertion, keys, issuer, expected, admission, cur)
    return {"message_id": message_id, "inbox_seq": inbox_seq,
            "envelope_digest": digest, "historical_replay": False, "converged": False}


def verify_delivery(cur: Any, store: Any, invocation_context: Any, *,
                    instance_id: str, message_id: str) -> dict:
    """Principal-authorized, instance-addressed provenance read (single snapshot)."""
    _require_delivery_storage(cur)
    principal_id, workspace_id = _recipient_read_identity(invocation_context, store)
    cur.execute(
        "SELECT d.request_id,d.resolution_revision,d.work_item_id,d.work_status_token,d.binding_id,"
        "d.binding_revision,d.message_id,d.envelope_digest AS delivery_digest,m.envelope,"
        "m.envelope_digest AS message_digest "
        "FROM operator_request_delivery d JOIN agent_message m USING(repo_id,workspace_id,message_id) "
        "JOIN operator_request_recipient_binding b ON b.binding_id=d.binding_id "
        "AND b.repo_id=d.repo_id AND b.workspace_id=d.workspace_id AND b.request_id=d.request_id "
        "AND b.work_item_id=d.work_item_id AND b.revision=d.binding_revision "
        "AND b.instance_id=d.recipient_instance_id "
        "JOIN agent_instance i ON i.repo_id=d.repo_id AND i.workspace_id=d.workspace_id "
        "AND i.instance_id=d.recipient_instance_id "
        "WHERE d.repo_id=%s AND d.workspace_id=%s AND d.message_id=%s "
        "AND b.withdrawn_at IS NULL AND i.retired_at IS NULL "
        "AND i.instance_id=%s AND i.principal_id=%s AND b.principal_id=%s "
        "AND m.sender_principal_id=d.sender_principal_id "
        "AND m.recipient_principal_id=b.principal_id "
        "AND m.recipient_instance_id=d.recipient_instance_id",
        (store.repo_id, workspace_id, message_id, instance_id, principal_id, principal_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RequestRefused("not-found", "verified delivery unavailable")
    # Verify both persisted digest references and exact envelope coordinates.
    envelope = row["envelope"]
    if isinstance(envelope, str):
        try:
            envelope = json.loads(envelope)
        except ValueError:
            envelope = None
    if (row["delivery_digest"] != row["message_digest"]
            or row["message_digest"] != hashlib.sha256(_canonical(envelope)).hexdigest()
            or envelope != {"schema": NOTIFICATION_SCHEMA, "message_id": row["message_id"],
                            "request_id": row["request_id"], "resolution_revision": row["resolution_revision"],
                            "work_item_id": row["work_item_id"], "work_status_token": row["work_status_token"],
                            "binding_id": row["binding_id"], "binding_revision": row["binding_revision"]}):
        raise RequestRefused("not-found", "verified delivery unavailable")
    return {"verified": True, "message_id": row["message_id"], "envelope_digest": row["delivery_digest"],
            "request_id": row["request_id"], "resolution_revision": row["resolution_revision"],
            "work_item_id": row["work_item_id"], "work_status_token": row["work_status_token"],
            "binding_id": row["binding_id"], "binding_revision": row["binding_revision"]}
