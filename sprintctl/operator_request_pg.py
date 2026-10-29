"""PostgreSQL persistence for operator-request/v1; caller owns the transaction."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from . import operator_request as contract
from .operator_request import RequestRefused
from .rows import iso_timestamp as _iso
from .workitemcore import item_status_revision


def _first(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) else row[0]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _next_revision(current: int) -> int:
    if current >= contract.MAX_REVISION:
        raise RequestRefused("revision-exhausted", "request revision limit has been reached")
    return current + 1


def _now(cur: Any) -> datetime:
    cur.execute("SELECT clock_timestamp() AS now")
    return _first(cur.fetchone(), "now")


def _request(cur: Any, store: Any, workspace_id: str, request_id: str, *, lock: bool = False) -> dict | None:
    cur.execute(
        "SELECT * FROM operator_request WHERE repo_id=%s AND workspace_id=%s AND request_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (store.repo_id, workspace_id, request_id),
    )
    return cur.fetchone()


def _work_item(cur: Any, store: Any, item_id: int, *, lock: bool = False) -> dict:
    cur.execute(
        "SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s"
        + (" FOR KEY SHARE" if lock else ""),
        (store.repo_id, item_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RequestRefused("work-not-found", "dependant work item does not exist in this repository")
    return row


def _validate_dependant_work(cur: Any, store: Any, dependants: list[dict]) -> list[tuple[dict, dict]]:
    return [(dependent, _work_item(cur, store, dependent["work_item_id"], lock=True))
            for dependent in dependants]


def _record_event(cur: Any, store: Any, workspace_id: str, request_id: str, revision: int,
                  actor: str, action: str, details: Mapping[str, Any], recorded_at: datetime) -> None:
    cur.execute(
        "INSERT INTO operator_request_event(repo_id,workspace_id,request_id,revision,actor,action,details,recorded_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
        (store.repo_id, workspace_id, request_id, revision, actor, action, json.dumps(dict(details)), recorded_at),
    )


def _event(cur: Any, store: Any, workspace_id: str, request_id: str, revision: int,
           actor: str, action: str, details: Mapping[str, Any], recorded_at: datetime) -> dict:
    _record_event(cur, store, workspace_id, request_id, revision, actor, action, details, recorded_at)
    return {
        "request_id": request_id, "revision": revision, "action": action,
        "actor": actor, "details": dict(details), "recorded_at": _iso(recorded_at),
    }


def _dependants(cur: Any, store: Any, workspace_id: str, request_id: str) -> list[dict]:
    cur.execute(
        "SELECT work_item_id,session_id,runtime_id,work_revision,state,attached_at,withdrawn_at "
        "FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s AND request_id=%s "
        "ORDER BY work_item_id,session_id",
        (store.repo_id, workspace_id, request_id),
    )
    result = []
    for row in cur.fetchall():
        try:
            current = _work_item(cur, store, int(row["work_item_id"]))
            current_revision = item_status_revision(current)
        except RequestRefused:
            current_revision = None
        result.append({
            "work_item_id": int(row["work_item_id"]), "session_id": row["session_id"],
            "runtime_id": row["runtime_id"], "work_revision": row["work_revision"],
            "current_work_revision": current_revision,
            "source_changed": current_revision != row["work_revision"],
            "state": row["state"], "attached_at": _iso(row["attached_at"]),
            "withdrawn_at": _iso(row["withdrawn_at"]),
        })
    return result


def _snapshot(cur: Any, store: Any, workspace_id: str, row: Mapping[str, Any], now: datetime) -> dict:
    packet = row["packet"]
    if isinstance(packet, str):
        packet = json.loads(packet)
    checked = datetime.fromisoformat(packet["blocker"]["fresh_until"].replace("Z", "+00:00"))
    return {
        "request_id": row["request_id"], "revision": int(row["revision"]), "state": row["state"],
        "freshness": "current" if now < checked else "needs-refresh",
        "packet": packet, "dependants": _dependants(cur, store, workspace_id, row["request_id"]),
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
    }


def publish(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    request_id, packet, dependants = args["request_id"], args["packet"], args["dependants"]
    packet_digest = _digest(packet)
    publish_digest = _digest({"packet": packet, "dependants": dependants})
    existing = _request(cur, store, workspace_id, request_id, lock=True)
    if existing is not None:
        if existing["requester_principal_id"] == principal_id and existing["publish_digest"] == publish_digest:
            return {"request_id": request_id, "revision": int(existing["revision"]), "replayed": True}
        raise RequestRefused("request-id-conflict", "request_id is already bound to another request")
    item_links = _validate_dependant_work(cur, store, dependants)
    now = _now(cur)
    cur.execute(
        "INSERT INTO operator_request(repo_id,workspace_id,request_id,requester_principal_id,revision,"
        "material_revision,state,packet,packet_digest,publish_digest,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,1,1,'open',%s::jsonb,%s,%s,%s,%s) "
        "ON CONFLICT (repo_id,workspace_id,request_id) DO NOTHING RETURNING request_id",
        (store.repo_id, workspace_id, request_id, principal_id, json.dumps(packet), packet_digest,
         publish_digest, now, now),
    )
    inserted = cur.fetchone()
    if inserted is None:
        # A concurrent publish with another idempotency key may have won after
        # the initial read. Resolve that race as the same stable request or a
        # conflict instead of leaking a PostgreSQL unique-violation response.
        existing = _request(cur, store, workspace_id, request_id, lock=True)
        if (existing is not None and existing["requester_principal_id"] == principal_id
                and existing["publish_digest"] == publish_digest):
            return {"request_id": request_id, "revision": int(existing["revision"]), "replayed": True}
        raise RequestRefused("request-id-conflict", "request_id is already bound to another request")
    cur.executemany(
        "INSERT INTO operator_request_dependant(repo_id,workspace_id,request_id,work_item_id,session_id,"
        "runtime_id,work_revision,state,attached_at,withdrawn_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,NULL)",
        [(store.repo_id, workspace_id, request_id, dependent["work_item_id"], dependent["session_id"],
          dependent.get("runtime_id"), item_status_revision(item), now)
         for dependent, item in item_links],
    )
    _event(cur, store, workspace_id, request_id, 1, principal_id, "published",
           {"dependent_count": len(dependants), "packet_digest": packet_digest}, now)
    return {"request_id": request_id, "revision": 1, "replayed": False}


def revise(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    if row["requester_principal_id"] != principal_id:
        raise RequestRefused("requester-not-owner", "only the requesting principal may revise this request")
    expected = args["expected_revision"]
    if int(row["revision"]) != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": int(row["revision"]),
        })
    if row["state"] not in {"open", "reported-performed", "verifying"}:
        raise RequestRefused("request-not-open", "only a live request may be revised")
    revision = _next_revision(expected)
    packet = args["packet"]
    packet_digest = _digest(packet)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request SET revision=%s,state='open',material_revision=material_revision+1,packet=%s::jsonb,"
        "packet_digest=%s,updated_at=%s WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
        (revision, json.dumps(packet), packet_digest, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "revised",
           {"packet_digest": packet_digest}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def add_dependant(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    if row["requester_principal_id"] != principal_id:
        raise RequestRefused("requester-not-owner", "only the requesting principal may add a dependant")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] != "open":
        raise RequestRefused("request-not-open", "only an open request may gain a dependant")
    dependent = args["dependant"]
    cur.execute(
        "SELECT state FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s AND request_id=%s "
        "AND work_item_id=%s AND session_id=%s FOR UPDATE",
        (store.repo_id, workspace_id, args["request_id"], dependent["work_item_id"], dependent["session_id"]),
    )
    existing = cur.fetchone()
    if existing is not None:
        if existing["state"] == "active":
            return {"request_id": args["request_id"], "revision": current, "replayed": True}
        raise RequestRefused("dependant-already-withdrawn", "a withdrawn dependant cannot be reactivated")
    item = _work_item(cur, store, dependent["work_item_id"], lock=True)
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "INSERT INTO operator_request_dependant(repo_id,workspace_id,request_id,work_item_id,session_id,"
        "runtime_id,work_revision,state,attached_at,withdrawn_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,NULL)",
        (store.repo_id, workspace_id, args["request_id"], dependent["work_item_id"], dependent["session_id"],
         dependent.get("runtime_id"), item_status_revision(item), now),
    )
    cur.execute(
        "UPDATE operator_request SET revision=%s,updated_at=%s WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
        (revision, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "dependant-added",
           {"work_item_id": dependent["work_item_id"], "session_id": dependent["session_id"]}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def withdraw_dependant(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    if row["requester_principal_id"] != principal_id:
        raise RequestRefused("requester-not-owner", "only the requesting principal may withdraw a dependant")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] != "open":
        raise RequestRefused("request-not-open", "only an open request may withdraw a dependant")
    cur.execute(
        "SELECT state FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s AND request_id=%s "
        "AND work_item_id=%s AND session_id=%s FOR UPDATE",
        (store.repo_id, workspace_id, args["request_id"], args["work_item_id"], args["session_id"]),
    )
    dependent = cur.fetchone()
    if dependent is None or dependent["state"] != "active":
        raise RequestRefused("dependant-not-active", "dependant is absent or already withdrawn")
    cur.execute(
        "SELECT count(*) AS active_count FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s AND state='active'",
        (store.repo_id, workspace_id, args["request_id"]),
    )
    active_count = int(_first(cur.fetchone(), "active_count"))
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request_dependant SET state='withdrawn',withdrawn_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s AND work_item_id=%s AND session_id=%s",
        (now, store.repo_id, workspace_id, args["request_id"], args["work_item_id"], args["session_id"]),
    )
    state = "withdrawn" if active_count == 1 else "open"
    cur.execute(
        "UPDATE operator_request SET revision=%s,state=%s,updated_at=%s WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s",
        (revision, state, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "dependant-withdrawn",
           {"work_item_id": args["work_item_id"], "session_id": args["session_id"], "request_state": state}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def progress(cur: Any, store: Any, principal_id: str, workspace_id: str, args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] not in {"open", "reported-performed", "verifying"}:
        raise RequestRefused("request-not-open", "procedure progress requires a live request")
    packet = row["packet"]
    if isinstance(packet, str):
        packet = json.loads(packet)
    if args["step_id"] not in {step["step_id"] for step in packet["procedure"]["steps"]}:
        raise RequestRefused("unknown-procedure-step", "step_id is not in the current procedure")
    cur.execute(
        "SELECT state FROM operator_request_dependant WHERE repo_id=%s AND workspace_id=%s AND request_id=%s "
        "AND work_item_id=%s AND session_id=%s FOR UPDATE",
        (store.repo_id, workspace_id, args["request_id"], args["work_item_id"], args["session_id"]),
    )
    dependant = cur.fetchone()
    if dependant is None or dependant["state"] != "active":
        raise RequestRefused("dependant-not-active", "procedure progress requires an active dependant")
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request SET revision=%s,updated_at=%s WHERE repo_id=%s AND workspace_id=%s "
        "AND request_id=%s",
        (revision, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "step-progress",
           {"work_item_id": args["work_item_id"], "session_id": args["session_id"],
            "step_id": args["step_id"], "status": args["status"], "note": args["note"]}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def report_performed(cur: Any, store: Any, principal_id: str, workspace_id: str,
                     args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] != "open":
        raise RequestRefused("request-not-open", "only an open request accepts a performed report")
    state = "reported-performed" if args["outcome"] == "performed" else "verifying"
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request SET revision=%s,state=%s,updated_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s",
        (revision, state, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "performed-reported",
           {"outcome": args["outcome"], "note": args["note"], "verified": False}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def attest(cur: Any, store: Any, principal_id: str, workspace_id: str,
           args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] not in {"reported-performed", "verifying"}:
        raise RequestRefused("request-not-verifiable", "human attestation requires a performed or uncertain report")
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request SET revision=%s,state='resolved',updated_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s",
        (revision, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id,
           "resolved-human-attestation",
           {"completion_basis": "human-attestation", "statement": args["statement"],
            "performed_at": args["performed_at"], "protected_effect_authorized": False}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def decline(cur: Any, store: Any, principal_id: str, workspace_id: str,
            args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] not in {"open", "reported-performed", "verifying"}:
        raise RequestRefused("request-not-open", "only a live request can be declined")
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request SET revision=%s,state='declined',updated_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s",
        (revision, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "declined",
           {"reason": args["reason"]}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def supersede(cur: Any, store: Any, principal_id: str, workspace_id: str,
              args: Mapping[str, Any]) -> dict:
    row = _request(cur, store, workspace_id, args["request_id"], lock=True)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    if row["requester_principal_id"] != principal_id:
        raise RequestRefused("requester-not-owner", "only the requesting principal may supersede this request")
    expected = args["expected_revision"]
    current = int(row["revision"])
    if current != expected:
        raise RequestRefused("revision-conflict", "request revision changed", {
            "expected_revision": expected, "current_revision": current,
        })
    if row["state"] != "open":
        raise RequestRefused("request-not-open", "only an open request can be superseded")
    cur.execute(
        "SELECT work_item_id,session_id,runtime_id FROM operator_request_dependant WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s AND state='active' ORDER BY work_item_id,session_id",
        (store.repo_id, workspace_id, args["request_id"]),
    )
    dependants = cur.fetchall()
    if not dependants:
        raise RequestRefused("no-active-dependants", "request without active dependants cannot be superseded")
    replacement = publish(cur, store, principal_id, workspace_id, {
        "request_id": args["replacement_request_id"], "packet": args["packet"],
        "dependants": [{"work_item_id": int(item["work_item_id"]), "session_id": item["session_id"],
                        **({"runtime_id": item["runtime_id"]} if item["runtime_id"] is not None else {})}
                       for item in dependants],
    })
    revision = _next_revision(current)
    now = _now(cur)
    cur.execute(
        "UPDATE operator_request_dependant SET state='withdrawn',withdrawn_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s AND state='active'",
        (now, store.repo_id, workspace_id, args["request_id"]),
    )
    cur.execute(
        "UPDATE operator_request SET revision=%s,state='superseded',updated_at=%s WHERE repo_id=%s "
        "AND workspace_id=%s AND request_id=%s",
        (revision, now, store.repo_id, workspace_id, args["request_id"]),
    )
    _event(cur, store, workspace_id, args["request_id"], revision, principal_id, "superseded",
           {"replacement_request_id": args["replacement_request_id"],
            "replacement_revision": replacement["revision"]}, now)
    return {"request_id": args["request_id"], "revision": revision, "replayed": False}


def get(cur: Any, store: Any, workspace_id: str, request_id: str) -> dict:
    row = _request(cur, store, workspace_id, request_id)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    return _snapshot(cur, store, workspace_id, row, _now(cur))


def continuation(cur: Any, store: Any, workspace_id: str, request_id: str) -> dict:
    """Project a resolved request and its active work links in the caller's snapshot.

    This is a read-only successor packet, not delivery acknowledgement, native
    scheduling or protected-effect authorization. The supported resolution
    basis is the existing explicitly labeled human attestation.
    """
    row = _request(cur, store, workspace_id, request_id)
    if row is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    if row['state'] != 'resolved':
        raise RequestRefused("continuation-not-ready", "request has no supported owner resolution")
    snapshot = _snapshot(cur, store, workspace_id, row, _now(cur))
    cur.execute(
        "SELECT action,details,recorded_at FROM operator_request_event "
        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s AND revision=%s",
        (store.repo_id, workspace_id, request_id, snapshot['revision']),
    )
    event = cur.fetchone()
    details = event['details'] if event else None
    if (event is None or event['action'] != 'resolved-human-attestation'
            or not isinstance(details, dict) or details.get('completion_basis') != 'human-attestation'
            or details.get('protected_effect_authorized') is not False):
        raise RequestRefused('resolution-evidence-unavailable', 'owner resolution event is not supported')
    dependants = []
    for dependant in snapshot['dependants']:
        if dependant['state'] != 'active':
            continue
        current = (snapshot['freshness'] == 'current' and not dependant['source_changed']
                   and dependant['current_work_revision'] == dependant['work_revision'])
        dependants.append({
            'work_item_id': dependant['work_item_id'], 'session_id': dependant['session_id'],
            'work_revision': dependant['work_revision'],
            'current_work_revision': dependant['current_work_revision'],
            'state': 'continuation-ready' if current else 'needs-refresh',
        })
    return {
        'schema': 'operator-request-continuation-ready/v1',
        'owner': {'repo_id': store.repo_id, 'read_operation': contract.OPERATION_CONTINUATION},
        'request_id': request_id, 'request_revision': snapshot['revision'],
        'resolution': {'event_revision': snapshot['revision'], 'basis': 'human-attestation',
                       'event_recorded_at': _iso(event['recorded_at'])},
        'dependants': dependants,
    }


def list_requests(cur: Any, store: Any, workspace_id: str, args: Mapping[str, Any]) -> dict:
    after = args.get("after_request_id", "")
    parameters: list[Any] = [store.repo_id, workspace_id, after]
    where = "repo_id=%s AND workspace_id=%s AND request_id>%s"
    if "state" in args:
        where += " AND state=%s"
        parameters.append(args["state"])
    cur.execute(
        f"SELECT * FROM operator_request WHERE {where} ORDER BY request_id LIMIT %s",
        (*parameters, args["limit"] + 1),
    )
    rows = cur.fetchall()
    selected = rows[:args["limit"]]
    now = _now(cur)
    return {
        "requests": [_snapshot(cur, store, workspace_id, row, now) for row in selected],
        "next_after_request_id": selected[-1]["request_id"] if selected else args.get("after_request_id"),
    }


def events(cur: Any, store: Any, workspace_id: str, args: Mapping[str, Any]) -> dict:
    if _request(cur, store, workspace_id, args["request_id"]) is None:
        raise RequestRefused("not-found", "no such request in this repository and workspace")
    cur.execute(
        "SELECT revision,actor,action,details,recorded_at FROM operator_request_event "
        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s AND revision>%s "
        "ORDER BY revision LIMIT %s",
        (store.repo_id, workspace_id, args["request_id"], args["after_revision"], args["limit"] + 1),
    )
    rows = cur.fetchall()
    selected = rows[:args["limit"]]
    return {
        "request_id": args["request_id"],
        "events": [{"request_id": args["request_id"], "revision": int(row["revision"]),
                    "actor": row["actor"], "action": row["action"], "details": row["details"],
                    "recorded_at": _iso(row["recorded_at"])} for row in selected],
        "next_after_revision": int(selected[-1]["revision"]) if selected else args["after_revision"],
    }
