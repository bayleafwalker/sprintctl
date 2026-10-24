"""Offline packaging for explicitly captured continuation pages.

This preserves independent page observations. It never invokes the owner or
combines page contents into a claimed snapshot.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

from .result_packet import MAX_BYTES, PacketError, canonical, decode

SCHEMA = "sprintctl-portable-continuation-collection/v1"
MAX_PAGES = 64
MAX_COLLECTION_BYTES = MAX_BYTES
class CollectionError(ValueError):
    """Invalid offline continuation collection input."""


def _timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise CollectionError("observed_at must be an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionError("observed_at must be an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollectionError("observed_at must include a timezone")


def _contract():
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
    except ImportError as exc:
        raise CollectionError("offline collection validation requires sprintctl[inspection]") from exc
    from .vuoro_adapter import WORK_OPERATION_CONTRACTS
    contract = next(c for c in WORK_OPERATION_CONTRACTS
                    if c.name == "work.run.continuation-delta-v1")
    return Draft202012Validator, ValidationError, contract


def _validate_response(response: Any) -> None:
    Draft202012Validator, ValidationError, contract = _contract()
    validator = Draft202012Validator(contract.result_schema)
    try:
        validator.validate(response)
    except ValidationError as exc:
        raise CollectionError("page response does not match the supported owner schema") from exc
    if response["delta"]["schema"] != "sprintctl-work-continuation-delta/v1":
        raise CollectionError("unsupported owner continuation response version")


def _validate_request_basis(envelope: dict[str, Any]) -> None:
    Draft202012Validator, ValidationError, contract = _contract()
    for name in ("run_id", "item_id", "limit", "cursor", "checkpoint"):
        value = envelope[name]
        if value is None:
            if name in {"run_id", "item_id", "limit"}:
                raise CollectionError(f"{name} is required in every request envelope")
            continue
        schema = contract.input_schema["properties"].get(name)
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            raise CollectionError(f"{name} does not match the owner request schema") from exc


def _validate_request_basis_values(after_note_id: Any, after_chain_seq: Any) -> None:
    Draft202012Validator, ValidationError, contract = _contract()
    for name, value in (("after_note_id", after_note_id), ("after_chain_seq", after_chain_seq)):
        if value is None:
            continue
        try:
            Draft202012Validator(contract.input_schema["properties"][name]).validate(value)
        except ValidationError as exc:
            raise CollectionError(f"{name} does not match the owner request schema") from exc


def _cursor_equal(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def _credential_check(value: Any) -> None:
    from .contracts import reject_credential_shaped_values
    try:
        reject_credential_shaped_values(value, "continuation collection")
    except ValueError as exc:
        raise CollectionError("credential-shaped values are not supported in collections") from exc


def _bounded_copy(value: Any) -> Any:
    try:
        if isinstance(value, bytes):
            return decode(value)
        encoded = canonical(value)
        if len(encoded) > MAX_COLLECTION_BYTES:
            raise CollectionError("collection input exceeds 2 MiB")
        copied = decode(encoded)
    except (PacketError, RecursionError, ValueError) as exc:
        if isinstance(exc, CollectionError):
            raise
        raise CollectionError("collection requires finite, valid Unicode JSON") from exc
    def check_depth(node: Any, depth: int = 0) -> None:
        if depth > 64:
            raise CollectionError("JSON nesting exceeds 64 levels")
        if isinstance(node, dict):
            for child in node.values():
                check_depth(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                check_depth(child, depth + 1)
    check_depth(copied)
    return copied


def create_collection(envelopes: Any, *, page_budget: int | None = None) -> dict[str, Any]:
    """Package supplied request-basis and response envelopes, without owner reads."""
    envelopes = _bounded_copy(envelopes)
    if not isinstance(envelopes, list) or not envelopes or len(envelopes) > MAX_PAGES:
        raise CollectionError("pages must be a nonempty list of at most 64 envelopes")
    budget = page_budget if page_budget is not None else len(envelopes)
    if type(budget) is not int or not 1 <= budget <= MAX_PAGES or len(envelopes) > budget:
        raise CollectionError("page budget must be 1..64 and cover every supplied page")
    pages = []
    identity = None
    prior_returned = None
    prior_checkpoint = None
    prior_snapshot = None
    seen_cursors = set()
    seen_note_cursors = set()
    seen_evidence_cursors = set()
    warnings = []
    for ordinal, envelope in enumerate(envelopes, 1):
        if not isinstance(envelope, dict) or set(envelope) != {
            "run_id", "item_id", "limit", "cursor", "checkpoint",
            "after_note_id", "after_chain_seq", "observed_at", "response"
        }:
            raise CollectionError("each page requires run_id, item_id, limit, cursor, checkpoint, "
                                  "after_note_id, after_chain_seq, observed_at, and response")
        request_cursor = envelope["cursor"]
        checkpoint = envelope["checkpoint"]
        after_note_id = envelope["after_note_id"]
        after_chain_seq = envelope["after_chain_seq"]
        _validate_request_basis(envelope)
        _validate_request_basis_values(after_note_id, after_chain_seq)
        if ordinal == 1 and request_cursor is not None:
            raise CollectionError("first page cursor must be null (explicit first-page marker)")
        if ordinal == 1 and (after_note_id is not None or after_chain_seq is not None):
            raise CollectionError("first page predecessor cursors must be null")
        if ordinal > 1:
            if prior_returned is None or not _cursor_equal(request_cursor, prior_returned["references"]):
                raise CollectionError("each page cursor must equal the exact prior returned reference cursor")
            if after_note_id != prior_returned["predecessor_notes"]:
                raise CollectionError("after_note_id must equal the prior returned predecessor note cursor")
            if after_chain_seq != prior_returned["predecessor_evidence"]:
                raise CollectionError("after_chain_seq must equal the prior returned predecessor evidence cursor")
            if not _cursor_equal(checkpoint, prior_checkpoint):
                raise CollectionError("checkpoint basis must remain identical across pages")
        if request_cursor is not None:
            cursor_key = canonical(request_cursor)
            if cursor_key in seen_cursors:
                raise CollectionError("repeated request cursor; pages cannot form a continuation chain")
            seen_cursors.add(cursor_key)
        for current, seen, name in (
            (after_note_id, seen_note_cursors, "after_note_id"),
            (after_chain_seq, seen_evidence_cursors, "after_chain_seq"),
        ):
            if current is not None:
                if current in seen:
                    raise CollectionError(f"repeated {name}; pages cannot form a continuation chain")
                seen.add(current)
        _timestamp(envelope["observed_at"])
        response = envelope["response"]
        _validate_response(response)
        _credential_check(envelope)
        delta = response["delta"]
        page_identity = (response["repo_id"], delta["authorization_scope"]["run_id"],
                         delta["work"]["item_id"])
        if identity is None:
            identity = page_identity
        elif identity != page_identity:
            raise CollectionError("all pages must identify the same repository, run, and item")
        if envelope["run_id"] != page_identity[1] or envelope["item_id"] != page_identity[2]:
            raise CollectionError("request run_id, item_id, and limit must match the owner response schema and bounds")
        returned = delta["next_reference_cursor"]
        snapshot_matches_request = (request_cursor is None or
                                    request_cursor["expected_snapshot_digest"] == delta["snapshot_digest"])
        if not snapshot_matches_request:
            warnings.append(f"page {ordinal} response snapshot differs from its request cursor basis")
        pages.append({
            "ordinal": ordinal, "request_inputs": {
                "run_id": envelope["run_id"], "item_id": envelope["item_id"],
                "limit": envelope["limit"],
                "cursor": request_cursor, "checkpoint": checkpoint,
                "after_note_id": after_note_id, "after_chain_seq": after_chain_seq,
            },
            "checkpoint_basis": checkpoint,
            "observed_at": envelope["observed_at"],
            "clock_provenance": "caller-reported; unverified",
            "response": response,
            "returned_cursors": {
                "references": returned,
                "predecessor_notes": delta["next_after_note_id"],
                "predecessor_evidence": delta["next_after_chain_seq"],
            },
            "owner_snapshot_digest": delta["snapshot_digest"],
            "request_checkpoint_digest": (request_cursor or {}).get("checkpoint_digest"),
            "owner_checkpoint_digest": (returned or {}).get("checkpoint_digest"),
            "snapshot_matches_request_basis": snapshot_matches_request,
            "integrity": {"domain": "canonical-json/v1",
                           "sha256": "sha256:" + hashlib.sha256(canonical(response)).hexdigest()},
        })
        prior_returned = {"references": returned,
                          "predecessor_notes": delta["next_after_note_id"],
                          "predecessor_evidence": delta["next_after_chain_seq"]}
        prior_checkpoint = checkpoint
        if prior_snapshot is not None and prior_snapshot != delta["snapshot_digest"]:
            warnings.append(f"page {ordinal} has a different owner snapshot digest from the prior page")
        prior_snapshot = delta["snapshot_digest"]
    delta = pages[-1]["response"]["delta"]
    cursors_remain = any((delta["next_reference_cursor"] is not None,
                          delta["next_after_note_id"] is not None,
                          delta["next_after_chain_seq"] is not None,
                          not delta["decision_page_complete"]))
    stop_reason = (
        ("page-budget-exhausted" if len(pages) >= budget else "supplied-pages-ended-with-cursors-remaining")
        if cursors_remain else "owner-cursors-null-and-decision-page-complete"
    )
    incomplete_status = ("partial-due-to-budget" if len(pages) >= budget
                         else "partial-supplied-pages-ended-early")
    complete = {
        "references": "complete" if delta["next_reference_cursor"] is None else incomplete_status,
        "predecessor_notes": "complete" if delta["next_after_note_id"] is None else incomplete_status,
        "predecessor_evidence": "complete" if delta["next_after_chain_seq"] is None else incomplete_status,
        "decision_history": "complete" if delta["decision_page_complete"] else incomplete_status,
    }
    body = {"schema": SCHEMA, "serializer_version": 1, "page_budget": budget,
            "page_count": len(pages), "identity": {"repo_id": identity[0], "run_id": identity[1], "item_id": identity[2]},
            "collection_kind": "offline-page-observations; non-atomic",
            "authenticity": "unknown", "currentness": "unknown", "authority": "unknown",
            "stop_reason": stop_reason, "more_content_may_remain": cursors_remain,
            "warnings": warnings,
            "completeness": complete, "record_counts": {
                "references": sum(len(p["response"]["delta"]["reference_page"]) for p in pages),
                "predecessor_notes": sum(len(p["response"]["delta"]["predecessor_notes"]) for p in pages),
                "predecessor_evidence": sum(len(p["response"]["delta"]["predecessor_evidence"]) for p in pages),
                "decision_history": sum(len(p["response"]["delta"]["decision_page"]) for p in pages)},
            "pages": pages}
    _credential_check(body)
    try:
        encoded = canonical(body)
    except PacketError as exc:
        raise CollectionError(str(exc)) from exc
    if len(encoded) > MAX_COLLECTION_BYTES:
        raise CollectionError("collection exceeds 2 MiB")
    collection = {**body, "integrity": {"domain": "canonical-json/v1",
                  "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest()}}
    if len(canonical(collection)) > MAX_COLLECTION_BYTES:
        raise CollectionError("collection including integrity record exceeds 2 MiB")
    return collection


def inspect_collection(value: bytes | dict[str, Any]) -> dict[str, Any]:
    """Verify canonical collection and page digests without contacting sources."""
    try:
        collection = decode(value) if isinstance(value, bytes) else _bounded_copy(value)
    except (PacketError, RecursionError, CollectionError) as exc:
        raise CollectionError("invalid offline collection JSON") from exc
    if not isinstance(collection, dict) or collection.get("schema") != SCHEMA:
        raise CollectionError("unsupported continuation collection schema")
    integrity = collection.get("integrity")
    pages = collection.get("pages")
    if (not isinstance(integrity, dict) or set(integrity) != {"domain", "sha256"}
            or integrity.get("domain") != "canonical-json/v1"
            or not isinstance(pages, list) or len(pages) > MAX_PAGES):
        raise CollectionError("malformed continuation collection integrity or pages")
    try:
        from .contracts import reject_credential_shaped_values
        reject_credential_shaped_values(collection, "continuation collection")
    except ValueError as exc:
        raise CollectionError("credential-shaped values are not supported in collections") from exc
    body = {key: child for key, child in collection.items() if key != "integrity"}
    computed = "sha256:" + hashlib.sha256(canonical(body)).hexdigest()
    page_reports = []
    for ordinal, page in enumerate(pages, 1):
        recorded = page.get("integrity") if isinstance(page, dict) else None
        response = page.get("response") if isinstance(page, dict) else None
        if not isinstance(recorded, dict) or not isinstance(response, dict):
            raise CollectionError(f"malformed page {ordinal} integrity record")
        page_digest = "sha256:" + hashlib.sha256(canonical(response)).hexdigest()
        page_reports.append({"ordinal": page.get("ordinal"),
                             "self_consistent": recorded.get("domain") == "canonical-json/v1"
                             and recorded.get("sha256") == page_digest,
                             "computed_sha256": page_digest,
                             "recorded_sha256": recorded.get("sha256")})
    return {"schema": "sprintctl-continuation-collection-inspection/v1",
            "integrity": {"self_consistent": computed == integrity.get("sha256"),
                          "computed_sha256": computed,
                          "recorded_sha256": integrity.get("sha256"),
                          "domain": "canonical-json/v1"},
            "page_integrity": page_reports,
            "authenticity": "unknown", "currentness": "unknown", "authority": "unknown",
            "owner_or_source_access": "none; offline inspection",
            "page_count": len(pages), "identity": collection.get("identity"),
            "stop_reason": collection.get("stop_reason"),
            "completeness": collection.get("completeness"),
            "record_counts": collection.get("record_counts")}
