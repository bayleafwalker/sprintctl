"""Agent directory and inbox storage (schema 25); never commits.

Every function runs on the caller's cursor inside the caller's transaction:
the application owns commit and rollback (the idempotency ledger's
transaction for register, observe, retire and send; a transaction of its own
for receipts and cancels; the invocation's implicit read transaction for
reads).  Every record is scoped to ``(store.repo_id, workspace_id)``, both
taken from the authenticated call, never from the wire; a record outside that
scope does not exist for the caller.

Timestamps come from the owner's clock (:func:`owner_now`).  Facts are
evaluated at read time; nothing here sweeps, deletes or transitions a record
because time passed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Mapping

from . import agent_inbox as contract
from .agent_inbox import InboxRefused
from .rows import iso_timestamp as _iso

#: Test support only (sprintctl.pg_testing.shift_inbox_clock): a session
#: setting that, when present, replaces the owner's clock for this session.
#: No served operation sets session parameters, so production reads always
#: fall through to clock_timestamp().
TEST_CLOCK_SETTING = "sprintctl.inbox_test_now"


def owner_now(cur: Any) -> datetime:
    cur.execute(
        "SELECT COALESCE(NULLIF(current_setting(%s, true), '')::timestamptz, clock_timestamp()) AS now",
        (TEST_CLOCK_SETTING,),
    )
    return _first(cur.fetchone(), "now")


def _first(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) else row[0]


def _refuse(code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
    raise InboxRefused(code, message, details)


# -- directory: registration, observation, retirement -----------------------

def _instance(cur: Any, store: Any, workspace_id: str, instance_id: str, *, lock: str = "") -> dict | None:
    cur.execute(
        "SELECT * FROM agent_instance WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s" + lock,
        (store.repo_id, workspace_id, instance_id),
    )
    return cur.fetchone()


def _latest_declaration(cur: Any, store: Any, workspace_id: str, instance_id: str) -> dict:
    cur.execute(
        "SELECT revision, declaration, declaration_digest FROM agent_declaration_revision "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s ORDER BY revision DESC LIMIT 1",
        (store.repo_id, workspace_id, instance_id),
    )
    row = cur.fetchone()
    assert row is not None, "a registered instance always has a declaration revision"
    return row


def _owned_instance(cur: Any, store: Any, principal_id: str, workspace_id: str, instance_id: str,
                    *, lock: str = "") -> dict:
    """The caller's instance: not-found when unregistered in scope,
    instance-not-owned when another principal registered it."""
    row = _instance(cur, store, workspace_id, instance_id, lock=lock)
    if row is None:
        _refuse("not-found", "no such instance is registered in this repository")
    if row["principal_id"] != principal_id:
        _refuse("instance-not-owned", "that instance is registered to another principal")
    return row


def register(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    instance_id, declaration = args["instance_id"], args["declaration"]
    digest = contract.sha256_hex(declaration)
    now = owner_now(cur)
    cur.execute(
        "INSERT INTO agent_instance(repo_id, workspace_id, instance_id, principal_id, registered_at) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING instance_id",
        (store.repo_id, workspace_id, instance_id, principal_id, now),
    )
    created = cur.fetchone() is not None
    row = _owned_instance(cur, store, principal_id, workspace_id, instance_id, lock=" FOR UPDATE")
    if row["retired_at"] is not None:
        _refuse("instance-retired", "a retired instance cannot be registered again")
    revision = 1
    if not created:
        latest = _latest_declaration(cur, store, workspace_id, instance_id)
        revision = int(latest["revision"])
        if latest["declaration_digest"] == digest:
            return {"instance_id": instance_id, "declaration_revision": revision, "replayed": False}
        revision += 1
    cur.execute(
        "INSERT INTO agent_declaration_revision(repo_id, workspace_id, instance_id, revision, "
        "declaration, declaration_digest, recorded_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)",
        (store.repo_id, workspace_id, instance_id, revision, json.dumps(declaration), digest, now),
    )
    return {"instance_id": instance_id, "declaration_revision": revision, "replayed": False}


def observe(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    instance_id, fact = args["instance_id"], args["fact"]
    row = _owned_instance(cur, store, principal_id, workspace_id, instance_id, lock=" FOR SHARE")
    if row["retired_at"] is not None:
        _refuse("instance-retired", "a retired instance cannot write observations")
    if fact == "attended":
        declaration = _latest_declaration(cur, store, workspace_id, instance_id)["declaration"]
        if declaration["interactivity"] == "non-interactive":
            _refuse("fact-not-applicable", "a non-interactive instance cannot report attended")
    observed_at = owner_now(cur)
    valid_until = observed_at + timedelta(seconds=int(args["valid_for_seconds"]))
    cur.execute(
        "INSERT INTO agent_observation(repo_id, workspace_id, instance_id, fact, channel, run_id, "
        "observed_at, valid_until, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (store.repo_id, workspace_id, instance_id, fact, args.get("channel"), args.get("run_id"),
         observed_at, valid_until, contract.SOURCE_SELF_REPORTED),
    )
    return {
        "instance_id": instance_id, "fact": fact, "observed_at": _iso(observed_at),
        "valid_until": _iso(valid_until), "source": contract.SOURCE_SELF_REPORTED, "replayed": False,
    }


def retire(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    instance_id = args["instance_id"]
    row = _owned_instance(cur, store, principal_id, workspace_id, instance_id, lock=" FOR UPDATE")
    if row["retired_at"] is not None:
        _refuse("instance-retired", "that instance is already retired")
    now = owner_now(cur)
    cur.execute(
        "UPDATE agent_instance SET retired_at=%s, retire_reason=%s "
        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s",
        (now, args["reason"], store.repo_id, workspace_id, instance_id),
    )
    return {"instance_id": instance_id, "retired_at": _iso(now), "replayed": False}


# -- directory: facts as read -------------------------------------------------

def _fact(row: dict | None, evaluated_at: datetime) -> dict:
    if row is None:
        return {"state": "unknown", "observed_at": None, "valid_until": None, "source": None}
    return {
        "state": "current" if evaluated_at < row["valid_until"] else "stale",
        "observed_at": _iso(row["observed_at"]), "valid_until": _iso(row["valid_until"]),
        "source": row["source"],
    }


def directory(cur: Any, store: Any, workspace_id: str, *, principal_id: str | None = None,
              include_retired: bool = True) -> list[dict]:
    """agent-directory-entry/v1 entries, one per principal, ordered by principal_id."""
    evaluated_at = owner_now(cur)
    params: list[Any] = [store.repo_id, workspace_id]
    where = "i.repo_id=%s AND i.workspace_id=%s"
    if principal_id is not None:
        where += " AND i.principal_id=%s"
        params.append(principal_id)
    if not include_retired:
        where += " AND i.retired_at IS NULL"
    cur.execute(
        "SELECT i.*, d.revision, d.declaration FROM agent_instance i "
        "JOIN LATERAL (SELECT revision, declaration FROM agent_declaration_revision r "
        "  WHERE r.repo_id=i.repo_id AND r.workspace_id=i.workspace_id AND r.instance_id=i.instance_id "
        "  ORDER BY revision DESC LIMIT 1) d ON true "
        f"WHERE {where} ORDER BY i.principal_id, i.registered_at, i.instance_id",
        params,
    )
    instances = cur.fetchall()
    if not instances:
        return []
    cur.execute(
        "SELECT DISTINCT ON (instance_id, fact, COALESCE(channel, ''), COALESCE(run_id, '')) * "
        "FROM agent_observation WHERE repo_id=%s AND workspace_id=%s AND instance_id = ANY(%s) "
        "ORDER BY instance_id, fact, COALESCE(channel, ''), COALESCE(run_id, ''), observation_id DESC",
        (store.repo_id, workspace_id, [row["instance_id"] for row in instances]),
    )
    latest: dict[str, list[dict]] = {}
    for row in cur.fetchall():
        latest.setdefault(row["instance_id"], []).append(row)
    entries: dict[str, dict] = {}
    for row in instances:
        entry = entries.setdefault(row["principal_id"], {
            "schema": contract.DIRECTORY_ENTRY_SCHEMA, "principal_id": row["principal_id"],
            "evaluated_at": _iso(evaluated_at), "instances": [],
        })
        entry["instances"].append(_instance_facts(row, latest.get(row["instance_id"], []), evaluated_at))
    return list(entries.values())


def _instance_facts(row: dict, observations: list[dict], evaluated_at: datetime) -> dict:
    declaration = row["declaration"]
    by_fact: dict[str, list[dict]] = {fact: [] for fact in contract.FACTS}
    for observation in observations:
        by_fact[observation["fact"]].append(observation)
    reachable_rows = {obs["channel"]: obs for obs in by_fact["reachable"]}
    channels = [channel["channel"] for channel in declaration["channels"]]
    channels += sorted(set(reachable_rows) - set(channels))
    executing = sorted(by_fact["executing"], key=lambda obs: obs["observation_id"])
    if declaration["interactivity"] == "non-interactive":
        attended = {"state": "not-applicable", "observed_at": None, "valid_until": None, "source": None}
    else:
        attended = _fact(by_fact["attended"][0] if by_fact["attended"] else None, evaluated_at)
    return {
        "instance_id": row["instance_id"],
        "declaration": declaration,
        "declaration_revision": int(row["revision"]),
        "registered": {
            "state": "retired" if row["retired_at"] is not None else "current",
            "registered_at": _iso(row["registered_at"]),
            "retired_at": _iso(row["retired_at"]),
            "retire_reason": row["retire_reason"],
        },
        "observed": _fact(by_fact["observed"][0] if by_fact["observed"] else None, evaluated_at),
        "reachable": [{**_fact(reachable_rows.get(channel), evaluated_at), "channel": channel}
                      for channel in channels],
        # Per run: current or stale.  An empty list is "unknown".
        "executing": [{**_fact(obs, evaluated_at), "run_id": obs["run_id"]} for obs in executing],
        "attended": attended,
    }


# -- inbox: send -------------------------------------------------------------

def _message(cur: Any, store: Any, workspace_id: str, message_id: str, *, lock: str = "") -> dict | None:
    cur.execute(
        "SELECT * FROM agent_message WHERE repo_id=%s AND workspace_id=%s AND message_id=%s" + lock,
        (store.repo_id, workspace_id, message_id),
    )
    return cur.fetchone()


def _stamps(row: Mapping[str, Any]) -> dict:
    return {
        "message_id": row["message_id"],
        "sender_principal_id": row["sender_principal_id"],
        "workspace_id": row["workspace_id"],
        "repo_id": row["repo_id"],
        "persisted_at": _iso(row["persisted_at"]),
        "envelope_digest": row["envelope_digest"],
        "inbox_seq": int(row["inbox_seq"]),
    }


def _require_recipient(cur: Any, store: Any, workspace_id: str, recipient: Mapping[str, Any]) -> None:
    cur.execute(
        "SELECT 1 FROM agent_instance WHERE repo_id=%s AND workspace_id=%s AND principal_id=%s LIMIT 1",
        (store.repo_id, workspace_id, recipient["principal_id"]),
    )
    if cur.fetchone() is None:
        _refuse("recipient-unknown", "the recipient principal has no registration in this repository")
    target = recipient.get("instance_id")
    if target is None:
        return
    # FOR SHARE: a concurrent retirement waits for this send, or this send
    # sees the retirement.
    row = _instance(cur, store, workspace_id, target, lock=" FOR SHARE")
    if row is None or row["principal_id"] != recipient["principal_id"] or row["retired_at"] is not None:
        _refuse("recipient-unknown", "the recipient instance is not a current instance of that principal")


def _require_context(cur: Any, store: Any, workspace_id: str, context: Mapping[str, Any]) -> None:
    if "item_id" in context:
        cur.execute("SELECT 1 FROM work_item WHERE repo_id=%s AND id=%s",
                    (store.repo_id, context["item_id"]))
        if cur.fetchone() is None:
            _refuse("context-not-found", "context.item_id names no work item in this repository")
    if "predecessor_run_id" in context:
        cur.execute("SELECT 1 FROM run WHERE repo_id=%s AND workspace_id=%s AND run_id=%s",
                    (store.repo_id, workspace_id, context["predecessor_run_id"]))
        if cur.fetchone() is None:
            _refuse("context-not-found", "context.predecessor_run_id names no run in this repository")


def _allocate_inbox_seq(cur: Any, store: Any, workspace_id: str) -> int:
    """The next ``inbox_seq`` of ``(workspace, repo)``.

    Not a sequence or identity column: the counter row's lock is held until
    this transaction ends, so a concurrent send to the same repository
    waits here and commits after this one.  Commit order is therefore
    sequence order, and a reader that has seen ``n`` can never later find a
    committed message below ``n`` (contract section 3.1).  A rolled-back
    send rolls its increment back too, so committed numbers have no gaps.
    """
    cur.execute(
        "INSERT INTO agent_inbox_counter(repo_id, workspace_id, last_seq) VALUES (%s,%s,0) "
        "ON CONFLICT DO NOTHING",
        (store.repo_id, workspace_id),
    )
    cur.execute(
        "UPDATE agent_inbox_counter SET last_seq = last_seq + 1 "
        "WHERE repo_id=%s AND workspace_id=%s RETURNING last_seq",
        (store.repo_id, workspace_id),
    )
    return int(_first(cur.fetchone(), "last_seq"))


def _insert_message(cur: Any, store: Any, values: Mapping[str, Any]) -> dict:
    cur.execute(
        "INSERT INTO agent_message(repo_id, workspace_id, message_id, inbox_seq, sender_principal_id, "
        "recipient_principal_id, recipient_instance_id, envelope, envelope_digest, persisted_at) "
        "VALUES (%(repo_id)s,%(workspace_id)s,%(message_id)s,%(inbox_seq)s,%(sender_principal_id)s,"
        "%(recipient_principal_id)s,%(recipient_instance_id)s,%(envelope)s,%(envelope_digest)s,"
        "%(persisted_at)s) RETURNING *",
        dict(values),
    )
    return cur.fetchone()


def send(cur: Any, store: Any, principal_id: str, workspace_id: str, envelope: Mapping[str, Any],
         encoded: bytes) -> dict:
    """Persist an admitted envelope; the stamps plus ``replayed``.

    The application's ledger (keyed by message_id per sender) answers a
    same-sender replay before this runs.  Here a stored message under the
    same id replays only for the same sender and digest; anything else is
    message-id-conflict.
    """
    digest = hashlib.sha256(encoded).hexdigest()
    existing = _message(cur, store, workspace_id, envelope["message_id"])
    if existing is not None:
        if existing["sender_principal_id"] == principal_id and existing["envelope_digest"] == digest:
            return {**_stamps(existing), "replayed": True}
        _refuse("message-id-conflict", "that message_id is already used by another envelope")
    _require_recipient(cur, store, workspace_id, envelope["recipient"])
    _require_context(cur, store, workspace_id, envelope["context"])
    inbox_seq = _allocate_inbox_seq(cur, store, workspace_id)
    row = _insert_message(cur, store, {
        "repo_id": store.repo_id, "workspace_id": workspace_id, "message_id": envelope["message_id"],
        "inbox_seq": inbox_seq, "sender_principal_id": principal_id,
        "recipient_principal_id": envelope["recipient"]["principal_id"],
        "recipient_instance_id": envelope["recipient"].get("instance_id"),
        "envelope": encoded.decode("utf-8"), "envelope_digest": digest,
        "persisted_at": owner_now(cur),
    })
    return {**_stamps(row), "replayed": False}


# -- inbox: reads ------------------------------------------------------------

def _receipt(row: Mapping[str, Any]) -> dict:
    return {
        "schema": contract.RECEIPT_SCHEMA, "message_id": row["message_id"], "kind": row["kind"],
        "principal_id": row["principal_id"], "instance_id": row["instance_id"],
        "recorded_at": _iso(row["recorded_at"]), **row["fields"],
    }


def _receipts(cur: Any, store: Any, workspace_id: str, message_ids: list[str]) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {message_id: [] for message_id in message_ids}
    if not message_ids:
        return found
    cur.execute(
        "SELECT * FROM agent_message_receipt WHERE repo_id=%s AND workspace_id=%s AND message_id = ANY(%s) "
        "ORDER BY recorded_at, receipt_seq",
        (store.repo_id, workspace_id, message_ids),
    )
    for row in cur.fetchall():
        found[row["message_id"]].append(_receipt(row))
    return found


def _stamped(row: Mapping[str, Any], receipts: list[dict]) -> dict:
    stamps = _stamps(row)
    del stamps["message_id"]
    return {**json.loads(row["envelope"]), **stamps, "receipts": receipts}


def read(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    after_seq, instance_id = int(args["after_seq"]), args.get("instance_id")
    if instance_id is not None:
        row = _instance(cur, store, workspace_id, instance_id)
        if row is None or row["principal_id"] != principal_id:
            _refuse("not-found", "no such instance of the caller is registered in this repository")
    cur.execute(
        "SELECT * FROM agent_message WHERE repo_id=%s AND workspace_id=%s AND recipient_principal_id=%s "
        "AND inbox_seq > %s AND (recipient_instance_id IS NULL OR recipient_instance_id = %s) "
        "ORDER BY inbox_seq LIMIT %s",
        (store.repo_id, workspace_id, principal_id, after_seq, instance_id, int(args["limit"])),
    )
    rows = cur.fetchall()
    receipts = _receipts(cur, store, workspace_id, [row["message_id"] for row in rows])
    return {
        "messages": [_stamped(row, receipts[row["message_id"]]) for row in rows],
        "next_after_seq": int(rows[-1]["inbox_seq"]) if rows else after_seq,
    }


def status(cur: Any, store: Any, principal_id: str, workspace_id: str, message_id: str) -> dict:
    row = _message(cur, store, workspace_id, message_id)
    if row is None or principal_id not in (row["sender_principal_id"], row["recipient_principal_id"]):
        _refuse("not-found", "no such message for the caller")
    receipts = _receipts(cur, store, workspace_id, [message_id])[message_id]
    return {"message": _stamped(row, receipts), "receipts": [dict(r) for r in receipts]}


# -- inbox: receipts and cancellation ----------------------------------------

def _existing(cur: Any, store: Any, workspace_id: str, message_id: str) -> list[dict]:
    cur.execute(
        "SELECT * FROM agent_message_receipt WHERE repo_id=%s AND workspace_id=%s AND message_id=%s "
        "ORDER BY receipt_seq",
        (store.repo_id, workspace_id, message_id),
    )
    return cur.fetchall()


def _insert_receipt(cur: Any, store: Any, workspace_id: str, message_id: str, kind: str,
                    principal_id: str, instance_id: str | None, fields: Mapping[str, Any]) -> dict:
    cur.execute(
        "INSERT INTO agent_message_receipt(repo_id, workspace_id, message_id, kind, principal_id, "
        "instance_id, fields, field_digest, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) "
        "RETURNING *",
        (store.repo_id, workspace_id, message_id, kind, principal_id, instance_id,
         json.dumps(dict(fields)), contract.sha256_hex(dict(fields)), owner_now(cur)),
    )
    return {**_receipt(cur.fetchone()), "replayed": False}


def _natural_key_match(existing: list[dict], kind: str, instance_id: str) -> dict | None:
    """The stored receipt sharing this request's natural key, if any."""
    for row in existing:
        if kind in contract.DECISION_KINDS and row["kind"] in contract.DECISION_KINDS:
            return row
        if row["kind"] != kind:
            continue
        if kind in ("run-linked", "result-linked"):
            return row
        if row["instance_id"] == instance_id:  # observed, acknowledged, launch-ambiguous
            return row
    return None


_CONFLICT_CODE = {
    "accepted": "already-decided", "refused": "already-decided", "run-linked": "run-already-linked",
    "launch-ambiguous": "already-recorded", "result-linked": "already-resulted",
}


def receipt(cur: Any, store: Any, principal_id: str, workspace_id: str, message_id: str,
            instance_id: str, kind: str, fields: Mapping[str, Any]) -> dict:
    """Record one receipt under the contract's natural keys (section 3.3).

    The message row is locked first, so every receipt write for one message
    is serialized; the partial unique indexes on agent_message_receipt hold
    the same uniqueness rules underneath.  Precedence: visibility
    (not-found), instance ownership, exact replay, retirement, then the
    kind's prerequisites and conflicts.
    """
    message = _message(cur, store, workspace_id, message_id, lock=" FOR NO KEY UPDATE")
    if message is None or message["recipient_principal_id"] != principal_id:
        _refuse("not-found", "no such message in the caller's inbox")
    instance = _owned_instance(cur, store, principal_id, workspace_id, instance_id)
    target = message["recipient_instance_id"]
    if target is not None and target != instance_id and kind != "result-linked":
        _refuse("not-found", "that message is addressed to another instance")
    digest = contract.sha256_hex(dict(fields))
    existing = _existing(cur, store, workspace_id, message_id)
    match = _natural_key_match(existing, kind, instance_id)
    if match is not None and match["kind"] == kind and match["instance_id"] == instance_id \
            and match["field_digest"] == digest:
        return {**_receipt(match), "replayed": True}
    if instance["retired_at"] is not None:
        _refuse("instance-retired", "a retired instance cannot write receipts")
    # observed / acknowledged share no fields, so their natural-key match
    # always replays above; every other kind's match is a conflict.
    if match is not None:
        details = {"instance_id": match["instance_id"], "kind": match["kind"]}
        _refuse(_CONFLICT_CODE[kind], f"this message already has a {match['kind']} receipt", details)

    decision = next((row for row in existing if row["kind"] in contract.DECISION_KINDS), None)
    accepted_by = decision["instance_id"] if decision is not None and decision["kind"] == "accepted" else None
    run_link = next((row for row in existing if row["kind"] == "run-linked"), None)
    if kind in ("run-linked", "launch-ambiguous"):
        if accepted_by != instance_id:
            _refuse("not-accepted", f"{kind} requires accepted by the same instance")
        if "run_id" in fields:
            _require_run(cur, store, workspace_id, fields["run_id"])
    elif kind == "result-linked":
        if accepted_by is None:
            _refuse("not-accepted", "result-linked requires an accepted message")
        if accepted_by != instance_id:
            acceptor = _instance(cur, store, workspace_id, accepted_by)
            if fields["outcome"] != "abandoned" or acceptor is None or acceptor["retired_at"] is None:
                _refuse("not-accepted", "only the accepting instance may close this message; another "
                        "instance may write abandoned once the acceptor is retired")
        if fields["outcome"] in contract.RUN_OUTCOMES and run_link is None:
            _refuse("not-accepted", f"outcome {fields['outcome']} requires a run-linked receipt")
    return _insert_receipt(cur, store, workspace_id, message_id, kind, principal_id, instance_id, fields)


def _require_run(cur: Any, store: Any, workspace_id: str, run_id: str) -> None:
    cur.execute("SELECT 1 FROM run WHERE repo_id=%s AND workspace_id=%s AND run_id=%s",
                (store.repo_id, workspace_id, run_id))
    if cur.fetchone() is None:
        _refuse("run-not-found", "that run does not exist in this repository")


def cancel(cur: Any, store: Any, principal_id: str, workspace_id: str, message_id: str, reason: str) -> dict:
    message = _message(cur, store, workspace_id, message_id, lock=" FOR NO KEY UPDATE")
    if message is None:
        _refuse("not-found", "no such message")
    if message["sender_principal_id"] != principal_id:
        _refuse("not-sender", "only the sender may request cancellation")
    for row in _existing(cur, store, workspace_id, message_id):
        if row["kind"] == contract.CANCEL_KIND:
            return {**_receipt(row), "replayed": True}
    return _insert_receipt(cur, store, workspace_id, message_id, contract.CANCEL_KIND, principal_id,
                           None, {"reason": reason})
