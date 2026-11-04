"""Private recipient-consent transactions. The caller owns commit and retry.

No public adapter currently constructs RecipientProvenance. Database deadlocks
and serialization failures propagate to the transaction owner as retryable unknowns.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .operator_request import RequestRefused
from .operator_request_pg import _now, _request
from .agent_inbox_pg import _owned_instance
from .recipient_provenance import RecipientProvenance


def bind(cur: Any, store: Any, caller: RecipientProvenance, *, request_id: str,
         work_item_id: int, instance_id: str) -> dict:
    if not isinstance(caller, RecipientProvenance):
        raise TypeError("verified recipient provenance required")
    workspace = caller.workspace_id
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
    binding_id = str(uuid.uuid4())
    cur.execute("INSERT INTO operator_request_recipient_binding "
                "(binding_id,repo_id,workspace_id,request_id,work_item_id,principal_id,instance_id,"
                "gateway_issuer,subject,epoch,client_id,grant_id,resource,token_scopes,effective_authority,"
                "revision,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,1,%s)",
                (binding_id, store.repo_id, workspace, request_id, work_item_id, caller.principal_id,
                 instance_id, caller.gateway_issuer, caller.subject, caller.epoch, caller.client_id,
                 caller.grant_id, caller.resource, json.dumps(sorted(caller.token_scopes)),
                 json.dumps(["work:read"]), _now(cur)))
    return {"binding_id": binding_id, "revision": 1}


def withdraw(cur: Any, store: Any, caller: RecipientProvenance, *, request_id: str,
             binding_id: str, expected_revision: int) -> dict:
    if not isinstance(caller, RecipientProvenance):
        raise TypeError("verified recipient provenance required")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    workspace = caller.workspace_id
    if _request(cur, store, workspace, request_id, lock=True) is None:
        raise RequestRefused("not-found", "request not found in caller workspace")
    cur.execute("SELECT * FROM operator_request_recipient_binding WHERE repo_id=%s AND workspace_id=%s "
                "AND request_id=%s AND binding_id=%s FOR UPDATE", (store.repo_id, workspace, request_id, binding_id))
    row = cur.fetchone()
    if row is None or row["principal_id"] != caller.principal_id:
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
