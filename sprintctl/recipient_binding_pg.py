"""Private recipient-consent transactions. The caller owns commit and retry.

No public adapter currently constructs RecipientProvenance. Database deadlocks
and serialization failures propagate to the transaction owner as retryable unknowns.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .operator_request import RequestRefused
from .operator_request_pg import _now, _request
from .agent_inbox_pg import _owned_instance
from .recipient_provenance import (
    AuthenticatedOwnerIdentity,
    AuthenticatedRequesterContext,
    RecipientProvenance,
)
from .workitemcore import item_status_revision, validate_item_status_revision


def lookup_for_recipient_admission(
    cur: Any,
    store: Any,
    requester: AuthenticatedRequesterContext,
    *,
    request_id: str,
    request_revision: int,
    resolution_event_revision: int,
    work_item_id: int,
    work_revision: str,
    binding_id: str,
    binding_revision: int,
) -> dict:
    """Return internally stored recipient coordinates for a private adapter.

    This transaction-bound helper has no public route and does not authorize
    delivery. Recipient coordinates and provenance come only from owner rows;
    the requester may name only the opaque binding plus exact current request,
    resolution, work and revision tokens. The caller owns commit and retries.
    """
    if not isinstance(requester, AuthenticatedRequesterContext):
        raise TypeError("trusted authenticated requester context required")
    if requester.repo_id != store.repo_id:
        raise RequestRefused("repository-mismatch", "requester identity is not scoped to this repository")
    for name, value in (("request_revision", request_revision),
                        ("resolution_event_revision", resolution_event_revision),
                        ("work_item_id", work_item_id),
                        ("binding_revision", binding_revision)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id must be a nonempty string")
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise ValueError("binding_id must be a nonempty string")
    try:
        validate_item_status_revision(work_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("work_revision must be a valid item status revision") from exc

    workspace = requester.workspace_id
    # Request is the serialization root for dependant and binding changes.
    request = _request(cur, store, workspace, request_id, lock=True)
    if request is None or request["requester_principal_id"] != requester.principal_id:
        raise RequestRefused("not-found", "request not found")
    if (request["state"] != "resolved"
            or int(request["revision"]) != request_revision
            or resolution_event_revision != request_revision):
        raise RequestRefused("resolution-not-current", "exact request resolution is not current")

    packet = request["packet"]
    if isinstance(packet, str):
        try:
            packet = json.loads(packet)
        except (TypeError, ValueError) as exc:
            raise RequestRefused("request-freshness-unavailable", "request freshness is unavailable") from exc
    try:
        expiry_value = packet["blocker"]["fresh_until"]
        if not isinstance(expiry_value, str):
            raise ValueError("fresh_until is not a string")
        expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("fresh_until lacks timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestRefused("request-freshness-unavailable", "request freshness is unavailable") from exc
    cur.execute(
        "SELECT action,details,recorded_at FROM operator_request_event "
        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s AND revision=%s",
        (store.repo_id, workspace, request_id, resolution_event_revision),
    )
    event = cur.fetchone()
    details = event["details"] if event is not None else None
    if (event is None or event["action"] != "resolved-human-attestation"
            or not isinstance(details, dict)
            or details.get("completion_basis") != "human-attestation"
            or details.get("protected_effect_authorized") is not False):
        raise RequestRefused("resolution-evidence-unavailable", "supported resolution evidence is unavailable")

    cur.execute(
        "SELECT * FROM operator_request_recipient_binding WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s AND work_item_id=%s AND binding_id=%s FOR UPDATE",
        (store.repo_id, workspace, request_id, work_item_id, binding_id),
    )
    binding_row = cur.fetchone()
    if (binding_row is None or binding_row["withdrawn_at"] is not None
            or int(binding_row["revision"]) != binding_revision):
        raise RequestRefused("binding-unavailable", "recipient binding is unavailable")

    try:
        scopes = binding_row["token_scopes"]
        effective_scopes = binding_row["verified_effective_scopes"]
        effective_authority = binding_row["effective_authority"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        if isinstance(effective_scopes, str):
            effective_scopes = json.loads(effective_scopes)
        if isinstance(effective_authority, str):
            effective_authority = json.loads(effective_authority)
        if (not isinstance(scopes, list) or not isinstance(effective_scopes, list)
                or not isinstance(effective_authority, list)
                or any(not isinstance(value, str) for values in
                       (scopes, effective_scopes, effective_authority) for value in values)):
            raise ValueError("malformed stored scope provenance")
        provenance = RecipientProvenance(
            principal_id=binding_row["principal_id"],
            workspace_id=binding_row["workspace_id"],
            gateway_issuer=binding_row["gateway_issuer"],
            subject=binding_row["subject"],
            epoch=int(binding_row["epoch"]),
            client_id=binding_row["client_id"],
            grant_id=binding_row["grant_id"],
            resource=binding_row["resource"],
            token_scopes=frozenset(scopes),
            effective_scopes=frozenset(effective_scopes),
        )
        if (len(set(scopes)) != len(scopes)
                or len(set(effective_scopes)) != len(effective_scopes)
                or len(set(effective_authority)) != len(effective_authority)
                or set(effective_authority) != {"work:read"}
                or binding_row["principal_id"] !=
                   f"{binding_row['gateway_issuer']}:{binding_row['subject']}:{binding_row['epoch']}"):
            raise ValueError("stored recipient provenance is inconsistent")
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestRefused("binding-provenance-unavailable", "recipient binding provenance is unavailable") from exc

    instance = _owned_instance(
        cur, store, provenance.principal_id, workspace, binding_row["instance_id"], lock=" FOR SHARE")
    if instance["retired_at"] is not None:
        raise RequestRefused("instance-retired", "recipient instance is retired")

    cur.execute(
        "SELECT work_item_id,work_revision FROM operator_request_dependant "
        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s AND work_item_id=%s AND state='active' "
        "ORDER BY work_item_id,session_id",
        (store.repo_id, workspace, request_id, work_item_id),
    )
    dependants = cur.fetchall()
    if not any(int(row["work_item_id"]) == work_item_id for row in dependants):
        raise RequestRefused("dependant-not-active", "no active dependant for recipient work")

    # All active session rows for the requested work item share one status row.
    # Lock it once, after the request, binding and instance locks.
    cur.execute("SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s FOR SHARE",
                (store.repo_id, work_item_id))
    work = cur.fetchone()
    if work is None:
        raise RequestRefused("work-not-found", "active dependant work is unavailable")
    current_revision = item_status_revision(work)
    if any(current_revision != row["work_revision"] for row in dependants):
        raise RequestRefused("work-needs-refresh", "an active dependant work status changed")
    if current_revision != work_revision:
        raise RequestRefused("work-needs-refresh", "exact recipient work status is not current")

    # A previous clock read could become stale while waiting on any FOR SHARE
    # lock above. Check request freshness only after all eligibility locks land.
    if _now(cur) >= expiry:
        raise RequestRefused("request-needs-refresh", "request freshness has expired")

    return {
        "repo_id": store.repo_id,
        "workspace_id": workspace,
        "request_id": request_id,
        "request_revision": request_revision,
        "resolution_event_revision": resolution_event_revision,
        "work_item_id": work_item_id,
        "work_revision": work_revision,
        "binding_id": binding_id,
        "binding_revision": binding_revision,
        "recipient": {
            "principal_id": provenance.principal_id,
            "instance_id": binding_row["instance_id"],
            "gateway_issuer": provenance.gateway_issuer,
            "subject": provenance.subject,
            "epoch": provenance.epoch,
            "client_id": provenance.client_id,
            "grant_id": provenance.grant_id,
            "resource": provenance.resource,
            "token_scopes": sorted(provenance.token_scopes),
            "effective_scopes": sorted(provenance.effective_scopes),
            "effective_authority": sorted(effective_authority),
        },
        "requester_principal_id": requester.principal_id,
        "dependant_count": len(dependants),
        "event_recorded_at": event["recorded_at"].isoformat(),
        "fresh_until": expiry.astimezone(timezone.utc).isoformat(),
    }


def bind(cur: Any, store: Any, caller: RecipientProvenance, *, request_id: str,
         work_item_id: int, instance_id: str, idempotency_key: str) -> dict:
    if not isinstance(caller, RecipientProvenance):
        raise TypeError("verified recipient provenance required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be a nonempty string")
    workspace = caller.workspace_id
    # Serialize the caller/key even across different requests, before checking
    # mutable eligibility. Historical retries recover the original receipt.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps([store.repo_id, workspace, caller.principal_id, idempotency_key]),))
    cur.execute("SELECT * FROM operator_request_recipient_binding WHERE repo_id=%s AND workspace_id=%s "
                "AND principal_id=%s AND idempotency_key=%s FOR SHARE", (store.repo_id, workspace, caller.principal_id, idempotency_key))
    prior = cur.fetchone()
    if prior is not None:
        expected = (request_id, work_item_id, instance_id, caller.gateway_issuer, caller.subject,
                    caller.epoch, caller.client_id, caller.grant_id, caller.resource, sorted(caller.token_scopes),
                    sorted(caller.effective_scopes))
        actual = tuple(prior[k] for k in ("request_id", "work_item_id", "instance_id", "gateway_issuer", "subject",
                                          "epoch", "client_id", "grant_id", "resource", "token_scopes", "verified_effective_scopes"))
        if actual != expected:
            raise RequestRefused("idempotency-conflict", "idempotency key used with different binding")
        if prior["withdrawn_at"] is not None:
            return {"binding_id": prior["binding_id"], "revision": 1,
                    "historical_replay": True, "current_revision": prior["revision"],
                    "current_status": "withdrawn"}
        return {"binding_id": prior["binding_id"], "revision": 1}
    if _request(cur, store, workspace, request_id, lock=True) is None:
        raise RequestRefused("not-found", "request not found in caller workspace")
    cur.execute("SELECT 1 FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s "
                "AND request_id=%s AND work_item_id=%s AND state='active' LIMIT 1",
                (store.repo_id, workspace, request_id, work_item_id))
    if cur.fetchone() is None:
        raise RequestRefused("dependant-not-active", "no active dependant for this work item")
    instance = _owned_instance(cur, store, caller.principal_id, workspace, instance_id, lock=" FOR SHARE")
    if instance["retired_at"] is not None:
        raise RequestRefused("instance-retired", "instance is retired")
    # The request row lock also serializes withdrawal and different-key binds.
    cur.execute("SELECT 1 FROM operator_request_recipient_binding WHERE repo_id=%s AND workspace_id=%s "
                "AND request_id=%s AND work_item_id=%s AND principal_id=%s AND instance_id=%s "
                "AND withdrawn_at IS NULL", (store.repo_id, workspace, request_id, work_item_id,
                                             caller.principal_id, instance_id))
    if cur.fetchone() is not None:
        raise RequestRefused("binding-already-active", "active binding already exists")
    binding_id = str(uuid.uuid4())
    cur.execute("INSERT INTO operator_request_recipient_binding "
                "(binding_id,repo_id,workspace_id,request_id,work_item_id,principal_id,instance_id,"
                "gateway_issuer,subject,epoch,client_id,grant_id,resource,token_scopes,verified_effective_scopes,effective_authority,idempotency_key,"
                "revision,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,1,%s)",
                (binding_id, store.repo_id, workspace, request_id, work_item_id, caller.principal_id,
                 instance_id, caller.gateway_issuer, caller.subject, caller.epoch, caller.client_id,
                 caller.grant_id, caller.resource, json.dumps(sorted(caller.token_scopes)),
                 json.dumps(sorted(caller.effective_scopes)), json.dumps(["work:read"]), idempotency_key, _now(cur)))
    return {"binding_id": binding_id, "revision": 1}


def withdraw(cur: Any, store: Any, caller: AuthenticatedOwnerIdentity, *, request_id: str,
             binding_id: str, expected_revision: int) -> dict:
    if not isinstance(caller, AuthenticatedOwnerIdentity):
        raise TypeError("authenticated owner identity required")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    workspace = caller.workspace_id
    if _request(cur, store, workspace, request_id, lock=True) is None:
        raise RequestRefused("not-found", "request not found in caller workspace")
    cur.execute("SELECT * FROM operator_request_recipient_binding WHERE repo_id=%s AND workspace_id=%s "
                "AND request_id=%s AND binding_id=%s FOR UPDATE", (store.repo_id, workspace, request_id, binding_id))
    row = cur.fetchone()
    if row is None or any(row[name] != getattr(caller, name) for name in
                          ("principal_id", "gateway_issuer", "subject", "epoch")):
        raise RequestRefused("not-found", "binding not found")
    if row["revision"] != expected_revision:
        raise RequestRefused("revision-conflict", "binding revision changed")
    if row["withdrawn_at"] is not None:
        raise RequestRefused("binding-withdrawn", "binding already withdrawn")
    if row["revision"] >= 2147483647:
        raise RequestRefused("revision-exhausted", "binding revision limit reached")
    cur.execute("UPDATE operator_request_recipient_binding SET withdrawn_at=%s, revision=revision+1 "
                "WHERE binding_id=%s", (_now(cur), binding_id))
    return {"binding_id": binding_id, "revision": expected_revision + 1}
