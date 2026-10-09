"""Transaction-local attempt facts; never invokes a provider or commits.

The application claims the native idempotency ledger first. The owner then
locks intent, current work item when permission is requested, attempt, and
its event chain in that order. Historical replays never enter these helpers.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from . import effect_attempt as contract

AUTHORIZATION_SCHEMA = "sprintctl-effect-attempt-authorization/v1"
EVENT_SCHEMA = "sprintctl-effect-attempt-event/v1"


def digest(domain: str, body: Mapping[str, Any]) -> str:
    raw = json.dumps({"schema_version": domain, **body}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refuse(code: str, message: str) -> None:
    from .pg import EffectRefused
    raise EffectRefused(code, message)


def _owned(cur: Any, store: Any, attempt_id: str, binding: Mapping[str, Any], *, lock: bool = False) -> dict:
    cur.execute("SELECT * FROM work_effect_attempt WHERE repo_id=%s AND attempt_id=%s "
                "AND workspace_id=%s AND principal_id=%s "
                "AND client_id IS NOT DISTINCT FROM %s AND grant_id IS NOT DISTINCT FROM %s" +
                (" FOR UPDATE" if lock else ""),
                (store.repo_id, attempt_id, binding["workspace_id"], binding["principal_id"],
                 binding["client_id"], binding["grant_id"]))
    row = cur.fetchone()
    if row is None:
        _refuse("effect-attempt-not-found", "no attempt bound to this authenticated identity")
    return row


def _intent_locked(cur: Any, store: Any, intent_id: str, workspace_id: str) -> dict:
    cur.execute("SELECT * FROM work_effect_intent WHERE repo_id=%s AND intent_id=%s "
                "AND workspace_id=%s FOR UPDATE", (store.repo_id, intent_id, workspace_id))
    row = cur.fetchone()
    if row is None:
        _refuse("effect-not-found", "no effect intent in this authenticated workspace")
    return row


def _permission_guard(cur: Any, store: Any, intent: dict, revision: int,
                      intent_digest: str, expected_revision: str, release_digest: str) -> None:
    from . import pg
    if intent["state"] != "accepted":
        _refuse("effect-invalid-transition", "an attempt requires an accepted intent")
    if int(intent["revision"]) != revision:
        _refuse("effect-revision-mismatch", "attempt intent revision changed")
    if intent["canonical_intent_digest"] != intent_digest or (
            pg._effect_intent.canonical_intent_digest(pg._effect_content(intent)) != intent_digest):
        _refuse("effect-digest-mismatch", "attempt intent digest changed")
    # This is the current full Release revision, including revise count.
    _, current_revision, _ = pg._release_basis_locked(cur, store.repo_id, int(intent["work_item_id"]))
    current = pg._current_release_locked(cur, store.repo_id, int(intent["work_item_id"]))
    if (current_revision != expected_revision or current is None
            or current["item_revision"] != current_revision
            or current["release_digest"] != release_digest or intent["release_digest"] != release_digest):
        _refuse("effect-release-mismatch", "attempt requires the exact current full Release basis")
    pg._effect_application_guard(cur, store, intent)


def _event(cur: Any, store: Any, attempt: dict, kind: str, payload: dict) -> dict:
    from .pg import _iso
    from psycopg.types.json import Jsonb
    cur.execute("SELECT event_seq,event_digest FROM work_effect_attempt_event "
                "WHERE repo_id=%s AND attempt_id=%s ORDER BY event_seq DESC LIMIT 1",
                (store.repo_id, attempt["attempt_id"]))
    previous = cur.fetchone()
    cur.execute("SELECT clock_timestamp() AS created_at")
    created_at = cur.fetchone()["created_at"]
    body = {"repo_id": store.repo_id, "attempt_id": attempt["attempt_id"],
            "event_seq": 0 if previous is None else int(previous["event_seq"]) + 1,
            "event_kind": kind, "authorization_digest": attempt["authorization_digest"],
            "previous_event_digest": previous["event_digest"] if previous else None,
            "payload": payload, "created_at": _iso(created_at)}
    event_digest = digest(EVENT_SCHEMA, body)
    cur.execute("INSERT INTO work_effect_attempt_event(repo_id,attempt_id,event_seq,event_kind,"
                "authorization_digest,previous_event_digest,event_digest,payload,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (store.repo_id, body["attempt_id"], body["event_seq"], kind, body["authorization_digest"],
                 body["previous_event_digest"], event_digest, Jsonb(payload), created_at))
    return {**body, "event_digest": event_digest}


def open_in_transaction(cur: Any, store: Any, arguments: dict,
                        binding: Mapping[str, Any], request_digest: str) -> dict:
    from . import pg
    from psycopg.types.json import Jsonb
    intent = _intent_locked(cur, store, arguments["intent_id"], binding["workspace_id"])
    _permission_guard(cur, store, intent, arguments["revision"], arguments["canonical_intent_digest"],
                      arguments["expected_revision"], arguments["release_digest"])
    target = contract.canonical_target(intent, arguments["target"])
    cur.execute("SELECT attempt_id FROM work_effect_attempt WHERE repo_id=%s AND intent_id=%s "
                "AND intent_revision=%s AND provider_operation=%s",
                (store.repo_id, intent["intent_id"], int(intent["revision"]), target["operation"]))
    if cur.fetchone() is not None:
        _refuse("effect-attempt-already-authorized", "this intent revision and provider operation already has an authorization")
    attempt_id = pg._mint_prefixed_id("attempt_")
    authorization = {"repo_id": store.repo_id, "attempt_id": attempt_id, **binding,
        "intent_id": intent["intent_id"], "intent_revision": int(intent["revision"]),
        "canonical_intent_digest": intent["canonical_intent_digest"],
        "work_item_id": int(intent["work_item_id"]), "expected_revision": arguments["expected_revision"],
        "release_digest": arguments["release_digest"], "target": target,
        "target_digest": contract.canonical_target_digest(target),
        "acceptance": pg._effect_row(intent)["acceptance"],
        "verification_binding": intent.get("verification_binding")}
    authorization_digest = digest(AUTHORIZATION_SCHEMA, authorization)
    cur.execute("INSERT INTO work_effect_attempt(repo_id,attempt_id,intent_id,intent_revision,"
        "canonical_intent_digest,work_item_id,expected_revision,release_digest,workspace_id,"
        "principal_id,client_id,grant_id,provider_operation,target,target_digest,authorization_body,"
        "authorization_digest,idempotency_key,request_digest,state) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'accepted') RETURNING *",
        (store.repo_id, attempt_id, intent["intent_id"], int(intent["revision"]), intent["canonical_intent_digest"],
         int(intent["work_item_id"]), arguments["expected_revision"], arguments["release_digest"],
         binding["workspace_id"], binding["principal_id"], binding["client_id"], binding["grant_id"],
         target["operation"], Jsonb(target), authorization["target_digest"], Jsonb(authorization),
         authorization_digest, arguments["idempotency_key"], request_digest))
    attempt = cur.fetchone()
    event = _event(cur, store, attempt, "attempt_authorization_accepted", {"authorization": authorization})
    return {"authorization": authorization, "authorization_digest": authorization_digest, "receipt": event}


def _authorization_guard(attempt: dict, expected_digest: str) -> None:
    authorization = attempt["authorization_body"]
    if (attempt["authorization_digest"] != expected_digest
            or digest(AUTHORIZATION_SCHEMA, authorization) != expected_digest
            or contract.canonical_target_digest(attempt["target"]) != attempt["target_digest"]
            or attempt["target"] != authorization.get("target")
            or attempt["provider_operation"] != attempt["target"].get("operation")
            or any(attempt[field] != authorization.get(field) for field in (
                "repo_id", "attempt_id", "intent_id", "intent_revision", "canonical_intent_digest",
                "work_item_id", "expected_revision", "release_digest", "workspace_id", "principal_id",
                "client_id", "grant_id", "target_digest"))):
        _refuse("effect-attempt-digest-mismatch", "attempt authorization digest mismatch")


def consume_in_transaction(cur: Any, store: Any, arguments: dict,
                           binding: Mapping[str, Any], *, redeem: bool) -> dict:
    observed = _owned(cur, store, arguments["attempt_id"], binding)
    intent = _intent_locked(cur, store, observed["intent_id"], binding["workspace_id"])
    if redeem:
        _permission_guard(cur, store, intent, int(observed["intent_revision"]), observed["canonical_intent_digest"],
                          observed["expected_revision"], observed["release_digest"])
    attempt = _owned(cur, store, arguments["attempt_id"], binding, lock=True)
    _authorization_guard(attempt, arguments["authorization_digest"])
    if attempt["state"] != "accepted":
        _refuse("effect-attempt-already-consumed", "attempt authorization was already redeemed or sealed")
    state, column, kind = ("redeemed", "redeemed_at", "invocation_authorization_redeemed") if redeem else (
        "sealed_unused", "sealed_at", "attempt_closed_without_redemption")
    cur.execute(f"UPDATE work_effect_attempt SET state=%s,{column}=clock_timestamp() "
                "WHERE repo_id=%s AND attempt_id=%s", (state, store.repo_id, attempt["attempt_id"]))
    event = _event(cur, store, attempt, kind, {"target_digest": attempt["target_digest"]})
    return {"attempt_id": attempt["attempt_id"], "authorization_digest": attempt["authorization_digest"], "receipt": event}


def report_in_transaction(cur: Any, store: Any, arguments: dict, binding: Mapping[str, Any]) -> dict:
    observed = _owned(cur, store, arguments["attempt_id"], binding)
    _intent_locked(cur, store, observed["intent_id"], binding["workspace_id"])
    attempt = _owned(cur, store, arguments["attempt_id"], binding, lock=True)
    _authorization_guard(attempt, arguments["authorization_digest"])
    if (attempt["state"] != "redeemed" or attempt["provider_operation"] != "open_pull_request"
            or attempt["target"]["commit_sha"] != arguments["commit_sha"]):
        _refuse("effect-attempt-report-refused", "report requires this redeemed PR target and its exact commit")
    cur.execute("SELECT event_digest FROM work_effect_attempt_event WHERE repo_id=%s AND attempt_id=%s "
                "AND event_kind='application_report_received'", (store.repo_id, attempt["attempt_id"]))
    if cur.fetchone() is not None:
        _refuse("effect-attempt-already-reported", "attempt already has an immutable application report")
    event = _event(cur, store, attempt, "application_report_received",
                   {"commit_sha": arguments["commit_sha"], "pr_url": arguments["pr_url"]})
    return {"attempt_id": attempt["attempt_id"], "authorization_digest": attempt["authorization_digest"], "receipt": event}


def get_in_transaction(cur: Any, store: Any, attempt_id: str, binding: Mapping[str, Any]) -> dict:
    from .pg import _iso
    attempt = _owned(cur, store, attempt_id, binding)
    _authorization_guard(attempt, attempt["authorization_digest"])
    cur.execute("SELECT * FROM work_effect_attempt_event WHERE repo_id=%s AND attempt_id=%s ORDER BY event_seq",
                (store.repo_id, attempt_id))
    events = [{**r, "created_at": _iso(r["created_at"])} for r in cur.fetchall()]
    kinds = [r["event_kind"] for r in events]
    expected = ["attempt_authorization_accepted"]
    if attempt["state"] == "sealed_unused":
        expected.append("attempt_closed_without_redemption")
    elif attempt["state"] == "redeemed":
        expected.append("invocation_authorization_redeemed")
        if len(events) == 3:
            expected.append("application_report_received")
    if kinds != expected:
        _refuse("effect-attempt-integrity-refused", "attempt state and event history disagree")
    previous = None
    for seq, event in enumerate(events):
        body = {k: v for k, v in event.items() if k != "event_digest"}
        if (event["event_seq"] != seq or event["previous_event_digest"] != previous
                or event["authorization_digest"] != attempt["authorization_digest"]
                or digest(EVENT_SCHEMA, body) != event["event_digest"]):
            _refuse("effect-attempt-integrity-refused", "attempt event chain does not verify")
        previous = event["event_digest"]
    if events[0]["payload"] != {"authorization": attempt["authorization_body"]}:
        _refuse("effect-attempt-integrity-refused", "acceptance fact does not bind this authorization")
    return {"authorization": attempt["authorization_body"], "authorization_digest": attempt["authorization_digest"],
            "state": attempt["state"], "events": events}
