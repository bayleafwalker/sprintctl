"""Closed causal preconditions for the additive bound proposal operation."""
from __future__ import annotations

import re
from typing import Any

from . import releases

FIELDS = frozenset({"expected_revision", "release_digest", "reserve_idempotency_key",
                    "commit_sha", "evidence_tail"})
TAIL_FIELDS = frozenset({"item_id", "chain_seq", "entry_digest"})
ADMISSION_SCHEMA = "sprintctl-bound-proposal-admission/v1"


def validate_basis(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("causal_basis must contain exactly the five causal guard fields")
    revision = releases.validate_basis(value["expected_revision"])
    if not releases._RELEASE_REVISION_RE.fullmatch(revision):
        raise ValueError("causal basis requires the full observed owner Release revision")
    releases.validate_digest(value["release_digest"])
    key = value["reserve_idempotency_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise ValueError("invalid native reserve key")
    commit = value["commit_sha"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("trailer-backed commit_sha must be 40 lowercase hexadecimal characters")
    tail = value["evidence_tail"]
    if not isinstance(tail, dict) or set(tail) != TAIL_FIELDS:
        raise ValueError("evidence_tail must contain exactly item_id, chain_seq and entry_digest")
    item = tail["item_id"]
    if (not isinstance(item, str) or not item or "\0" in item
        or any(0xD800 <= ord(c) <= 0xDFFF for c in item)):
        raise ValueError("invalid evidence tail identifier")
    if type(tail["chain_seq"]) is not int or tail["chain_seq"] < 0:
        raise ValueError("evidence tail sequence must be a nonnegative integer")
    digest = tail["entry_digest"]
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("evidence tail requires its entry digest, not a payload digest")
    return {**value, "evidence_tail": dict(tail)}
