"""Offline packaging for explicitly captured continuation pages.

This preserves independent page observations. It never invokes the owner or
combines page contents into a claimed snapshot.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

from .result_packet import PacketError, canonical

SCHEMA = "sprintctl-portable-continuation-collection/v1"
MAX_PAGES = 64
MAX_COLLECTION_BYTES = 8 * 1024 * 1024
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


def _validate_request_basis(cursor: Any, checkpoint: Any) -> None:
    Draft202012Validator, ValidationError, contract = _contract()
    for name, value in (("cursor", cursor), ("checkpoint", checkpoint)):
        if value is None:
            continue
        schema = contract.input_schema["properties"].get(name)
        try:
            Draft202012Validator(schema).validate(value)
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
        encoded = canonical(value)
        if len(encoded) > MAX_COLLECTION_BYTES:
            raise CollectionError("collection input exceeds 8 MiB")
        copied = json.loads(encoded)
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
    """Package supplied {request_cursor, checkpoint, observed_at, response} pages."""
    envelopes = _bounded_copy(envelopes)
    if not isinstance(envelopes, list) or not envelopes or len(envelopes) > MAX_PAGES:
        raise CollectionError("pages must be a nonempty list of at most 64 envelopes")
    budget = page_budget if page_budget is not None else len(envelopes)
    if type(budget) is not int or not 1 <= budget <= MAX_PAGES or len(envelopes) > budget:
        raise CollectionError("page budget must be 1..64 and cover every supplied page")
    pages = []
    identity = None
    prior_returned = None
    prior_snapshot = None
    seen_cursors = set()
    warnings = []
    for ordinal, envelope in enumerate(envelopes, 1):
        if not isinstance(envelope, dict) or set(envelope) != {
            "request_cursor", "checkpoint", "observed_at", "response"
        }:
            raise CollectionError("each page requires request_cursor, checkpoint, observed_at, and response")
        request_cursor = envelope["request_cursor"]
        checkpoint = envelope["checkpoint"]
        _validate_request_basis(request_cursor, checkpoint)
        if ordinal == 1 and request_cursor is not None:
            raise CollectionError("first page request_cursor must be null (explicit first-page marker)")
        if ordinal > 1 and (prior_returned is None or not _cursor_equal(request_cursor, prior_returned)):
            raise CollectionError("each page request_cursor must equal the exact prior returned cursor")
        if request_cursor is not None:
            cursor_key = canonical(request_cursor)
            if cursor_key in seen_cursors:
                raise CollectionError("repeated request cursor; pages cannot form a continuation chain")
            seen_cursors.add(cursor_key)
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
        returned = delta["next_reference_cursor"]
        snapshot_matches_request = (request_cursor is None or
                                    request_cursor["expected_snapshot_digest"] == delta["snapshot_digest"])
        if not snapshot_matches_request:
            warnings.append(f"page {ordinal} response snapshot differs from its request cursor basis")
        pages.append({
            "ordinal": ordinal, "request_cursor": request_cursor,
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
        prior_returned = returned
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
    complete = {
        "references": "complete" if delta["next_reference_cursor"] is None else "partial-due-to-budget",
        "predecessor_notes": "complete" if delta["next_after_note_id"] is None else "partial-due-to-budget",
        "predecessor_evidence": "complete" if delta["next_after_chain_seq"] is None else "partial-due-to-budget",
        "decision_history": "complete" if delta["decision_page_complete"] else "partial-due-to-budget",
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
        raise CollectionError("collection exceeds 8 MiB")
    return {**body, "integrity": {"domain": "canonical-json/v1",
            "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest()}}
