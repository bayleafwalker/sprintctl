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


def _list_observations(delta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "references": {"returned_cursor": delta["next_reference_cursor"],
                       "owner_complete": delta["reference_page_complete"],
                       "record_count": len(delta["reference_page"])},
        "predecessor_notes": {"returned_cursor": delta["next_after_note_id"],
                              "record_count": len(delta["predecessor_notes"])},
        "predecessor_evidence": {"returned_cursor": delta["next_after_chain_seq"],
                                 "record_count": len(delta["predecessor_evidence"])},
        "decision_history": {"owner_complete": delta["decision_page_complete"],
                             "record_count": len(delta["decision_page"])},
    }


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


def create_collection(envelopes: Any, *, page_budget: int = MAX_PAGES) -> dict[str, Any]:
    """Package supplied request-basis and response envelopes, without owner reads."""
    envelopes = _bounded_copy(envelopes)
    if not isinstance(envelopes, list) or not envelopes or len(envelopes) > MAX_PAGES:
        raise CollectionError("pages must be a nonempty list of at most 64 envelopes")
    budget = MAX_PAGES if page_budget is None else page_budget
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
        reference_flag_matches_cursor = (
            delta["reference_page_complete"] == (returned is None)
        )
        if not reference_flag_matches_cursor:
            warnings.append(f"page {ordinal} reference completeness flag conflicts with its returned cursor")
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
            "list_observations": _list_observations(delta),
            "owner_snapshot_digest": delta["snapshot_digest"],
            "request_checkpoint_digest": (request_cursor or {}).get("checkpoint_digest"),
            "owner_checkpoint_digest": (returned or {}).get("checkpoint_digest"),
            "snapshot_matches_request_basis": snapshot_matches_request,
            "reference_flag_matches_cursor": reference_flag_matches_cursor,
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
    cursor_pending = any((delta["next_reference_cursor"] is not None,
                          delta["next_after_note_id"] is not None,
                          delta["next_after_chain_seq"] is not None))
    owner_incomplete_without_cursor = (
        (any(not p["list_observations"]["references"]["owner_complete"] and
             p["returned_cursors"]["references"] is None for p in pages))
        or any(not p["list_observations"]["decision_history"]["owner_complete"] for p in pages)
    )
    stop_reason = (
        ("page-budget-exhausted" if len(pages) >= budget else "supplied-pages-ended-with-cursors-remaining")
        if cursor_pending else "owner-reported-incomplete-without-cursor" if owner_incomplete_without_cursor
        else "owner-cursors-null-and-decision-page-complete"
    )
    more_content_may_remain: bool | str = (
        True if cursor_pending else "unknown" if owner_incomplete_without_cursor else False
    )
    incomplete_status = ("partial-due-to-budget" if len(pages) >= budget
                         else "partial-supplied-pages-ended-early")
    basis_consistent = all(p["snapshot_matches_request_basis"] for p in pages) and not any(
        "different owner snapshot" in warning for warning in warnings
    )
    if any(not p["reference_flag_matches_cursor"] for p in pages):
        reference_status = "unknown-owner-completeness-cursor-mismatch"
    elif any(not p["list_observations"]["references"]["owner_complete"] and
             p["returned_cursors"]["references"] is None for p in pages):
        reference_status = "unknown-owner-incomplete-without-cursor"
    elif not basis_consistent:
        reference_status = "unknown-inconsistent-request-checkpoint-basis"
    elif delta["reference_page_complete"] and delta["next_reference_cursor"] is None:
        reference_status = "complete-for-this-request-checkpoint-basis"
    else:
        reference_status = incomplete_status
    def scoped_status(complete_for_basis: bool, pending: bool = False) -> str:
        if not basis_consistent:
            return "unknown-inconsistent-request-checkpoint-basis"
        if complete_for_basis and not pending:
            return "complete-for-this-request-checkpoint-basis"
        return incomplete_status
    complete = {
        "references": reference_status,
        "predecessor_notes": scoped_status(delta["next_after_note_id"] is None,
                                            delta["next_after_note_id"] is not None),
        "predecessor_evidence": scoped_status(delta["next_after_chain_seq"] is None,
                                               delta["next_after_chain_seq"] is not None),
        "decision_history": (
            "unknown-owner-incomplete-page" if any(
                not p["list_observations"]["decision_history"]["owner_complete"] for p in pages
            ) else scoped_status(True)
        ),
    }
    body = {"schema": SCHEMA, "serializer_version": 1, "page_budget": budget,
            "page_count": len(pages), "identity": {"repo_id": identity[0], "run_id": identity[1], "item_id": identity[2]},
            "collection_kind": "offline-page-observations; non-atomic",
            "authenticity": "unknown", "currentness": "unknown", "authority": "unknown",
            "stop_reason": stop_reason, "more_content_may_remain": more_content_may_remain,
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
    collection_fields = {
        "schema", "serializer_version", "page_budget", "page_count", "identity",
        "collection_kind", "authenticity", "currentness", "authority", "stop_reason",
        "more_content_may_remain", "warnings", "completeness", "record_counts",
        "pages", "integrity",
    }
    page_fields = {
        "ordinal", "request_inputs", "checkpoint_basis", "observed_at", "clock_provenance",
        "response", "returned_cursors", "list_observations", "owner_snapshot_digest",
        "request_checkpoint_digest", "owner_checkpoint_digest", "snapshot_matches_request_basis",
        "reference_flag_matches_cursor", "integrity",
    }
    if (not isinstance(collection, dict) or set(collection) != collection_fields
            or collection.get("schema") != SCHEMA):
        raise CollectionError("unsupported continuation collection schema")
    integrity = collection.get("integrity")
    pages = collection.get("pages")
    if (not isinstance(integrity, dict) or set(integrity) != {"domain", "sha256"}
            or integrity.get("domain") != "canonical-json/v1"
            or not isinstance(integrity.get("sha256"), str)
            or not isinstance(pages, list) or not pages or len(pages) > MAX_PAGES):
        raise CollectionError("malformed continuation collection integrity or pages")
    identity = collection["identity"]
    if (type(collection["serializer_version"]) is not int or collection["serializer_version"] != 1
            or type(collection["page_budget"]) is not int or not len(pages) <= collection["page_budget"] <= MAX_PAGES
            or type(collection["page_count"]) is not int or collection["page_count"] != len(pages)
            or not isinstance(identity, dict) or set(identity) != {"repo_id", "run_id", "item_id"}
            or not isinstance(identity["repo_id"], str) or not identity["repo_id"]
            or type(identity["item_id"]) is not int or identity["item_id"] < 1
            or not isinstance(identity["run_id"], str) or not identity["run_id"]
            or collection["collection_kind"] != "offline-page-observations; non-atomic"
            or any(collection[field] != "unknown" for field in ("authenticity", "currentness", "authority"))
            or not isinstance(collection["stop_reason"], str)
            or collection["stop_reason"] not in {
                "page-budget-exhausted", "supplied-pages-ended-with-cursors-remaining",
                "owner-cursors-null-and-decision-page-complete",
                "owner-reported-incomplete-without-cursor"
            }
            or not (type(collection["more_content_may_remain"]) is bool
                    or collection["more_content_may_remain"] == "unknown")
            or not isinstance(collection["warnings"], list)
            or any(not isinstance(warning, str) for warning in collection["warnings"])):
        raise CollectionError("malformed continuation collection v1 metadata")
    allowed_status = {"complete-for-this-request-checkpoint-basis", "partial-due-to-budget",
                      "partial-supplied-pages-ended-early", "unknown-owner-completeness-cursor-mismatch",
                      "unknown-inconsistent-request-checkpoint-basis",
                      "unknown-owner-incomplete-without-cursor", "unknown-owner-incomplete-page"}
    completeness = collection["completeness"]
    counts = collection["record_counts"]
    list_names = {"references", "predecessor_notes", "predecessor_evidence", "decision_history"}
    if (not isinstance(completeness, dict) or set(completeness) != list_names
            or any(not isinstance(status, str) or status not in allowed_status
                   for status in completeness.values())
            or not isinstance(counts, dict) or set(counts) != list_names
            or any(type(count) is not int or count < 0 for count in counts.values())):
        raise CollectionError("malformed continuation completeness or record counts")
    try:
        from .contracts import reject_credential_shaped_values
        reject_credential_shaped_values(collection, "continuation collection")
    except ValueError as exc:
        raise CollectionError("credential-shaped values are not supported in collections") from exc
    body = {key: child for key, child in collection.items() if key != "integrity"}
    computed = "sha256:" + hashlib.sha256(canonical(body)).hexdigest()
    page_reports = []
    for ordinal, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or set(page) != page_fields
                or type(page.get("ordinal")) is not int or page["ordinal"] != ordinal):
            raise CollectionError(f"malformed page {ordinal} v1 fields or ordinal")
        request = page["request_inputs"]
        if not isinstance(request, dict) or set(request) != {
            "run_id", "item_id", "limit", "cursor", "checkpoint", "after_note_id", "after_chain_seq"
        }:
            raise CollectionError(f"malformed page {ordinal} request inputs")
        observations = page["list_observations"]
        observation_shapes = {
            "references": {"returned_cursor", "owner_complete", "record_count"},
            "predecessor_notes": {"returned_cursor", "record_count"},
            "predecessor_evidence": {"returned_cursor", "record_count"},
            "decision_history": {"owner_complete", "record_count"},
        }
        if not isinstance(page["returned_cursors"], dict) or set(page["returned_cursors"]) != {
            "references", "predecessor_notes", "predecessor_evidence"
        } or not isinstance(observations, dict) or set(observations) != set(observation_shapes):
            raise CollectionError(f"malformed page {ordinal} list observation fields")
        for name, fields in observation_shapes.items():
            observation = observations[name]
            if (not isinstance(observation, dict) or set(observation) != fields
                    or type(observation["record_count"]) is not int or observation["record_count"] < 0
                    or ("owner_complete" in observation and type(observation["owner_complete"]) is not bool)):
                raise CollectionError(f"malformed page {ordinal} {name} observation")
        _validate_request_basis(request)
        _validate_request_basis_values(request["after_note_id"], request["after_chain_seq"])
        _timestamp(page["observed_at"])
        if page["clock_provenance"] != "caller-reported; unverified":
            raise CollectionError(f"malformed page {ordinal} clock provenance")
        response = page["response"]
        _validate_response(response)
        delta = response["delta"]
        if (type(page["snapshot_matches_request_basis"]) is not bool
                or type(page["reference_flag_matches_cursor"]) is not bool):
            raise CollectionError(f"malformed page {ordinal} basis/completeness metadata")
        recorded = page["integrity"]
        if (not isinstance(recorded, dict) or set(recorded) != {"domain", "sha256"}
                or recorded.get("domain") != "canonical-json/v1"
                or not isinstance(recorded.get("sha256"), str)):
            raise CollectionError(f"malformed page {ordinal} integrity record")
        page_digest = "sha256:" + hashlib.sha256(canonical(response)).hexdigest()
        page_reports.append({"ordinal": page.get("ordinal"),
                             "self_consistent": recorded.get("domain") == "canonical-json/v1"
                             and recorded.get("sha256") == page_digest,
                             "computed_sha256": page_digest,
                             "recorded_sha256": recorded.get("sha256")})
    metadata_consistency = False
    metadata_detail = "derived collection metadata differs from retained page request/response records"
    try:
        rebuilt = create_collection([
            {**page["request_inputs"], "observed_at": page["observed_at"], "response": page["response"]}
            for page in pages
        ], page_budget=collection["page_budget"])
        rebuilt_body = {key: child for key, child in rebuilt.items() if key != "integrity"}
        metadata_consistency = canonical(rebuilt_body) == canonical(body)
        if metadata_consistency:
            metadata_detail = "derived metadata matches retained page request/response records"
    except (CollectionError, PacketError, RecursionError, KeyError, TypeError, ValueError):
        pass
    return {"schema": "sprintctl-continuation-collection-inspection/v1",
            "integrity": {"self_consistent": computed == integrity.get("sha256"),
                          "computed_sha256": computed,
                          "recorded_sha256": integrity.get("sha256"),
                          "domain": "canonical-json/v1"},
            "page_integrity": page_reports,
            "metadata_consistency": {"consistent": metadata_consistency, "detail": metadata_detail},
            "authenticity": "unknown", "currentness": "unknown", "authority": "unknown",
            "owner_or_source_access": "none; offline inspection",
            "page_count": len(pages), "identity": collection.get("identity"),
            "stop_reason": collection.get("stop_reason"),
            "completeness": collection.get("completeness"),
            "record_counts": collection.get("record_counts")}
