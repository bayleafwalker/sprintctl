"""Bounded work facts from explicitly cited parking evidence, never a chain read."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

REFERENCE = re.compile(r"vuoro-evidence:(run_[0-9A-HJKMNP-TV-Z]{26})/seq/(0|[1-9][0-9]{0,15})\Z")


def _utc(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    except (ValueError, AttributeError):
        return False


def bounded_observation(entry: dict | None, model_id: str) -> dict | None:
    """Publish only a validated, digest-bound denial's fixed scalar fields.

    Model families are classified by the recording consumer; the owner never
    classifies, selects, recommends or substitutes a model.
    """
    if entry is None or entry.get("kind") != "rate_limit_event":
        return None
    provenance = entry.get("provenance")
    value = provenance.get("observation") if isinstance(provenance, dict) else None
    fields = {"schema", "observed_at", "status", "limit_scope", "model_id", "model_family",
              "resets_at", "reset_source", "http_status", "overage"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    if (value["schema"] != "vuoro-rate-limit-observation/v1"
            or value["status"] != "rejected"
            or value["model_id"] != model_id
            or not isinstance(value["model_family"], str)
            or len(value["model_family"]) > 128
            or value["limit_scope"] not in ("plan", "family", "unknown")
            or value["reset_source"] not in ("harness-structured", "unparsed-local-string", "absent")
            or not _utc(value["observed_at"])
            or (value["resets_at"] is not None and not _utc(value["resets_at"]))
            or (value["overage"] is not None and type(value["overage"]) is not bool)
            or (value["http_status"] is not None and
                (type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599))):
        return None
    try:
        digest = "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                      ensure_ascii=False).encode()).hexdigest()
    except UnicodeError:
        return None
    if entry.get("digest") != digest:
        return None
    # No arbitrary provenance, claims, transcript refs, or collector fields cross.
    return {key: value[key] for key in ("observed_at", "status", "limit_scope", "resets_at",
                                        "reset_source", "http_status", "overage")}
