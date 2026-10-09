"""Read-time S4 evaluation. Authored assertions never establish execution facts.

The first served source covers the run chain and current Release basis only.
Execution authority coverage remains unsupported until a trusted owner source is
integrated. This module emits no Decision and grants no execution permission.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import re
from . import releases

EVALUATOR_REVISION = "s4-evidence-evaluation/v1"
MAX_CHAIN_ITEMS = 100_000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_INPUTS = 256
MAX_INPUT_TEXT = 256
MAX_SUBJECT_LENGTH = 512


class EvaluationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def instant(value: Any) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("timestamp requires an ISO date/time and timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_inputs(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or len(value) > MAX_INPUTS
        or any(
            not isinstance(k, str)
            or not 0 < len(k) <= MAX_INPUT_TEXT
            or not isinstance(v, str)
            or not 0 < len(v) <= MAX_INPUT_TEXT
            for k, v in value.items()
        )
    ):
        raise EvaluationError(
            "invalid-arguments",
            "current input digests must be a map of nonempty strings",
        )
    return dict(sorted(value.items()))


def validate_request(arguments: dict) -> None:
    fields = {
        "run_id",
        "subject",
        "basis",
        "as_of",
        "current_input_digests",
        "expected_tail",
    }
    try:
        if set(arguments) != fields:
            raise ValueError("evaluation requires exactly six closed arguments")
        if not isinstance(arguments["run_id"], str) or not re.fullmatch(
            r"run_[0-9A-HJKMNP-TV-Z]{26}", arguments["run_id"]
        ):
            raise ValueError("invalid run id")
        if (
            not isinstance(arguments["subject"], str)
            or not 0 < len(arguments["subject"]) <= MAX_SUBJECT_LENGTH
        ):
            raise ValueError("subject must be nonempty")
        basis = arguments["basis"]
        if (
            not isinstance(basis, dict)
            or set(basis) != {"item_id", "expected_revision", "release_digest"}
            or type(basis["item_id"]) is not int
            or basis["item_id"] < 1
        ):
            raise ValueError("invalid closed Release basis")
        if not isinstance(
            basis["expected_revision"], str
        ) or not releases._RELEASE_REVISION_RE.fullmatch(basis["expected_revision"]):
            raise ValueError("evaluation requires the full Release revision")
        releases.validate_digest(basis["release_digest"])
        instant(arguments["as_of"])
        validate_inputs(arguments["current_input_digests"])
        tail = arguments["expected_tail"]
        if tail is not None and (
            not isinstance(tail, dict)
            or set(tail) != {"item_id", "chain_seq", "entry_digest"}
            or not isinstance(tail["item_id"], str)
            or not tail["item_id"]
            or type(tail["chain_seq"]) is not int
            or tail["chain_seq"] < 0
            or not isinstance(tail["entry_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", tail["entry_digest"])
        ):
            raise ValueError("invalid exact evidence tail")
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvaluationError("invalid-arguments", str(exc)) from exc


def validity(item: dict, as_of: datetime, inputs: dict[str, str]) -> dict:
    """Inclusive valid_from/valid_until. Missing components are unknown."""
    result = {
        "item_id": item["item_id"],
        "digest": item["digest"],
        "status": "invalid",
        "reason_codes": [],
        "missing_inputs": [],
        "changed_inputs": [],
    }
    window = item.get("validity")
    try:
        if not isinstance(window, dict) or set(window) != {
            "basis",
            "valid_from",
            "valid_until",
            "component_digests",
        }:
            raise ValueError("invalid validity fields")
        basis = window["basis"]
        start = instant(window["valid_from"])
        end = None if window["valid_until"] is None else instant(window["valid_until"])
        components = validate_inputs(window["component_digests"])
        if basis not in {"indefinite", "bounded", "until_inputs_change"}:
            raise ValueError("invalid validity basis")
        if (
            (basis == "bounded" and end is None)
            or (basis != "bounded" and end is not None)
            or (end is not None and end < start)
        ):
            raise ValueError("invalid validity interval")
        if basis == "until_inputs_change" and not components:
            raise ValueError("input validity requires at least one component")
    except (ValueError, TypeError, OverflowError):
        result["reason_codes"] = ["invalid-validity"]
        return result
    if as_of < start:
        result.update(status="not-yet-valid", reason_codes=["before-valid-from"])
    elif end is not None and as_of > end:
        result.update(status="expired", reason_codes=["past-valid-until"])
    elif basis == "until_inputs_change":
        missing = sorted(set(components) - inputs.keys())
        changed = sorted(
            k for k in components if k in inputs and components[k] != inputs[k]
        )
        result.update(missing_inputs=missing, changed_inputs=changed)
        if changed:
            result.update(
                status="changed",
                reason_codes=["input-changed"] + (["input-missing"] if missing else []),
            )
        elif missing:
            result.update(status="unknown", reason_codes=["input-missing"])
        else:
            result.update(status="valid", reason_codes=["inputs-match"])
    else:
        result.update(
            status="valid",
            reason_codes=[
                "within-window" if basis == "bounded" else "content-addressed-assertion"
            ],
        )
    return result


def chain_tail(items: list[dict]) -> dict | None:
    if len(items) > MAX_CHAIN_ITEMS:
        raise EvaluationError(
            "evidence-snapshot-too-large", "complete chain exceeds evaluator bound", 409
        )
    previous = None
    seen = set()
    for sequence, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not {"item_id", "digest", "chain_seq", "chain_prev_digest"}
            <= item.keys()
        ):
            raise EvaluationError(
                "evidence-chain-invalid", "incomplete evidence chain", 409
            )
        if (
            type(item["chain_seq"]) is not int
            or item["chain_seq"] != sequence
            or item["chain_prev_digest"] != previous
            or item["item_id"] in seen
        ):
            raise EvaluationError(
                "evidence-chain-invalid",
                "evidence chain has a gap, duplicate or invalid predecessor",
                409,
            )
        seen.add(item["item_id"])
        # Preserve the existing chain's exact four-field digest encoding.
        previous = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        k: item[k]
                        for k in ("item_id", "digest", "chain_seq", "chain_prev_digest")
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
    return (
        None
        if not items
        else {
            "item_id": items[-1]["item_id"],
            "chain_seq": items[-1]["chain_seq"],
            "entry_digest": previous,
        }
    )


def evaluate(
    *,
    repo_id: str,
    run_binding: dict,
    subject: str,
    requested_basis: dict,
    current_basis: dict | None,
    items: list[dict],
    expected_tail: dict | None,
    as_of: str,
    current_input_digests: dict,
    observed_at: str,
) -> dict:
    try:
        clock = instant(as_of)
        observed = instant(observed_at)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvaluationError("invalid-arguments", str(exc)) from exc
    inputs = validate_inputs(current_input_digests)
    tail = chain_tail(items)
    if expected_tail != tail:
        raise EvaluationError(
            "evidence-tail-mismatch",
            "requested tail differs from complete captured chain",
            409,
        )
    snapshot = {
        "repo_id": repo_id,
        "run_binding": run_binding,
        "requested_basis": requested_basis,
        "current_basis": current_basis,
        "items": items,
    }
    try:
        encoded = canonical(snapshot)
    except (ValueError, TypeError) as exc:
        raise EvaluationError(
            "evidence-snapshot-invalid", "snapshot is not canonical JSON", 409
        ) from exc
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise EvaluationError(
            "evidence-snapshot-too-large",
            "complete snapshot exceeds evaluator byte bound",
            409,
        )
    basis_status = (
        "missing"
        if current_basis is None or current_basis.get("release_digest") is None
        else "current"
        if all(current_basis.get(k) == v for k, v in requested_basis.items())
        and current_basis.get("release_item_revision")
        == requested_basis["expected_revision"]
        else "stale"
    )
    valid = [validity(item, clock, inputs) for item in items]
    assertions = []
    for item, evaluated in zip(items, valid, strict=True):
        for position, claim in enumerate(item.get("claims", [])):
            if isinstance(claim, dict) and claim.get("subject") == subject:
                assertions.append(
                    {
                        "evidence_item_id": item["item_id"],
                        "claim_index": position,
                        "assertion": deepcopy(claim),
                        "validity_status": evaluated["status"],
                        "authority": "authored-assertion",
                    }
                )
    reasons = ["execution-authority-unsupported"]
    if basis_status != "current":
        reasons.append("release-basis-" + basis_status)
    if any(v["status"] != "valid" for v in valid):
        reasons.append("evidence-not-currently-sufficient")
    return {
        "schema_version": "evidence-evaluation/v1",
        "evaluator_revision": EVALUATOR_REVISION,
        "repo_id": repo_id,
        "run_binding": deepcopy(run_binding),
        "subject": subject,
        "as_of": timestamp(clock),
        "observed_at": timestamp(observed),
        "requested_basis": deepcopy(requested_basis),
        "current_basis": deepcopy(current_basis),
        "basis_status": basis_status,
        "evaluated_input_digests": inputs,
        "input_assurance": "caller-supplied",
        "source_watermark": {
            "evidence_chain": {"tail": tail, "item_count": len(items)},
            "item_release": deepcopy(current_basis),
            "execution_facts": None,
        },
        "snapshot_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "evidence_validity": valid,
        "authored_assertions": assertions,
        "authenticated_execution_facts": [],
        "authority_coverage": "unsupported",
        "effect_state": "unknown",
        "recommendation": "reconcile",
        "reason_codes": reasons,
        "supporting_ids": [],
        "conflicting_ids": [],
        "authorizes_execution": False,
    }
