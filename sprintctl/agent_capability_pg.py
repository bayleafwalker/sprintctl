"""Private append-only adjunct capability revisions for registered agents."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import agent_inbox as contract
from .agent_inbox_pg import owner_now
from .operator_request import RequestRefused
from .recipient_provenance import AuthenticatedRequesterContext


def publish(
    cur: Any,
    store: Any,
    requester: AuthenticatedRequesterContext,
    *,
    instance_id: str,
    base_declaration_revision: int,
    declaration: Any,
) -> dict[str, Any]:
    """Append a private v2 capability bound to the current v1 declaration.

    The authenticated owner must already have registered the instance through
    ``work.agent.register-v1``. The instance row lock serializes this append
    with registration and retirement so a capability cannot publish against a
    declaration revision that changed during the write.
    """
    if not isinstance(requester, AuthenticatedRequesterContext):
        raise TypeError("trusted authenticated requester context required")
    if requester.repo_id != store.repo_id or "work:evidence" not in requester.authorities:
        raise RequestRefused("authority-required", "capability publication requires work:evidence authority")
    from .agent_capability_schema import inspect_catalog
    if not inspect_catalog(cur)["available"]:
        raise RequestRefused("recipient-unsupported", "recipient capability storage is unavailable")
    contract.instance_id(instance_id)
    if (isinstance(base_declaration_revision, bool)
            or not isinstance(base_declaration_revision, int) or base_declaration_revision < 1):
        raise contract.invalid("base_declaration_revision must be a positive integer")
    capability = contract.validate_declaration_v2(declaration)
    encoded = contract.canonical_bytes(capability)
    digest = hashlib.sha256(encoded).hexdigest()

    cur.execute(
        "SELECT principal_id, retired_at FROM agent_instance "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s FOR UPDATE",
        (store.repo_id, requester.workspace_id, instance_id),
    )
    instance = cur.fetchone()
    if instance is None:
        raise RequestRefused("not-found", "no such instance is registered in this repository")
    if instance["principal_id"] != requester.principal_id:
        raise RequestRefused("instance-not-owned", "that instance is registered to another principal")
    if instance["retired_at"] is not None:
        raise RequestRefused("instance-retired", "a retired instance cannot publish capabilities")

    cur.execute(
        "SELECT revision, declaration, declaration_digest FROM agent_declaration_revision "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s ORDER BY revision DESC LIMIT 1",
        (store.repo_id, requester.workspace_id, instance_id),
    )
    base = cur.fetchone()
    if base is None or base["revision"] != base_declaration_revision:
        raise RequestRefused(
            "revision-conflict", "base declaration revision is no longer current",
            {"expected": base_declaration_revision,
             "current": base["revision"] if base is not None else None},
        )
    try:
        contract.validate_declaration(base["declaration"])
    except contract.InboxRefused as exc:
        raise RequestRefused("recipient-unsupported", "current v1 declaration is invalid") from exc
    if hashlib.sha256(contract.canonical_bytes(base["declaration"])).hexdigest() != base["declaration_digest"]:
        raise RequestRefused("recipient-unsupported", "current v1 declaration digest is invalid")

    cur.execute(
        "SELECT revision, base_declaration_revision, capability_digest "
        "FROM agent_capability_revision WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s "
        "ORDER BY revision DESC LIMIT 1",
        (store.repo_id, requester.workspace_id, instance_id),
    )
    previous = cur.fetchone()
    if (previous is not None and previous["base_declaration_revision"] == base_declaration_revision
            and previous["capability_digest"] == digest):
        return {
            "instance_id": instance_id,
            "capability_revision": previous["revision"],
            "base_declaration_revision": base_declaration_revision,
            "capability_digest": digest,
            "replayed": True,
        }
    revision = 1 if previous is None else int(previous["revision"]) + 1
    if revision > 2**31 - 1:
        raise RequestRefused("revision-exhausted", "capability revision sequence is exhausted")
    cur.execute(
        "INSERT INTO agent_capability_revision(repo_id,workspace_id,instance_id,revision,"
        "base_declaration_revision,capability,capability_digest,recorded_at) "
        "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
        (store.repo_id, requester.workspace_id, instance_id, revision,
         base_declaration_revision, encoded.decode("utf-8"), digest, owner_now(cur)),
    )
    return {
        "instance_id": instance_id,
        "capability_revision": revision,
        "base_declaration_revision": base_declaration_revision,
        "capability_digest": digest,
        "replayed": False,
    }


def latest_for_instance(cur: Any, store: Any, workspace_id: str, instance_id: str) -> dict[str, Any] | None:
    cur.execute(
        "SELECT retired_at FROM agent_instance "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s FOR SHARE",
        (store.repo_id, workspace_id, instance_id),
    )
    instance = cur.fetchone()
    if instance is None:
        return None
    # Keep this read in a separate statement. Under READ COMMITTED, a reader
    # can wait for a concurrent v1 registration on the instance lock; its
    # locking statement's snapshot predates the appended declaration revision.
    # This next statement gets a fresh snapshot while the instance lock is held.
    cur.execute(
        "SELECT (SELECT revision FROM agent_declaration_revision r "
        "  WHERE r.repo_id=%s AND r.workspace_id=%s AND r.instance_id=%s "
        "  ORDER BY revision DESC LIMIT 1) AS current_base_declaration_revision, "
        "c.revision AS capability_revision, c.base_declaration_revision, "
        "c.capability, c.capability_digest FROM (SELECT 1) AS one "
        "LEFT JOIN LATERAL (SELECT revision, base_declaration_revision, capability, capability_digest "
        "  FROM agent_capability_revision WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s "
        "  ORDER BY revision DESC LIMIT 1) c ON TRUE",
        (store.repo_id, workspace_id, instance_id,
         store.repo_id, workspace_id, instance_id),
    )
    return {"retired_at": instance["retired_at"], **cur.fetchone()}
