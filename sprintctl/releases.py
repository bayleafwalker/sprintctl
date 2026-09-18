"""Releases: what an execution reservation froze.

S3 PR2.  A Release is an append-only record of the work item an execution
reservation picked up: the item's revision, its acceptance contract and its
context refs, identified by a content digest.  Both backends store it in
``work_release`` (PostgreSQL schema 15, SQLite schema 24) and never change a
row once written.

The item revision frozen by a release is the item's description edit revision
plus the number of ``revise`` decisions recorded on the item::

    item:<aggregate_uuid>@description:v<N>@sha256:<hex>@revise:<count>

so a ``revise`` decision changes the revision even when nothing else did.
"Current release" is derived, never stored: the release frozen most recently
at the item's current revise count.  A ``revise`` decision therefore makes the
item's current release no longer current, and the next execution reservation
freezes a new one.

TS-11: ``review_required`` is the acceptance-contract default.

This module is SQL-free so both backends share one digest and one rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

DEFAULT_ACCEPTANCE_CONTRACT: dict[str, Any] = {"review_required": True}

_EDIT_REVISION = r"item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}"
_EDIT_REVISION_RE = re.compile(rf"^{_EDIT_REVISION}$")
_RELEASE_REVISION_RE = re.compile(rf"^{_EDIT_REVISION}@revise:[0-9]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class StaleReleaseBasis(ValueError):
    """A reservation request was made against an item revision that moved on.

    Freezing the current revision instead would release something other than
    what the requester saw, so the request is rejected (``stale-basis``).
    """

    reason_code = "stale-basis"

    def __init__(self, item_id: int, expected: str, current: str) -> None:
        super().__init__(
            f"Item #{item_id} revision changed since the reservation was requested: "
            f"expected {expected}, current {current}"
        )
        self.item_id = item_id
        self.expected = expected
        self.current_revision = current


class ReleaseMismatch(ValueError):
    """A decision named a release digest that is not a release of its item."""


def release_revision(edit_revision: str, revise_count: int) -> str:
    """Return the item revision a release freezes."""
    if not _EDIT_REVISION_RE.fullmatch(edit_revision):
        raise ValueError(f"invalid item edit revision {edit_revision!r}")
    if isinstance(revise_count, bool) or not isinstance(revise_count, int) or revise_count < 0:
        raise ValueError("revise_count must be a non-negative integer")
    return f"{edit_revision}@revise:{revise_count}"


def validate_basis(expected_revision: Any) -> str:
    """Validate a reservation request's item revision basis.

    Either form is accepted: the full release revision, or the plain
    description edit revision that item reads already return.
    """
    if isinstance(expected_revision, str) and (
        _RELEASE_REVISION_RE.fullmatch(expected_revision)
        or _EDIT_REVISION_RE.fullmatch(expected_revision)
    ):
        return expected_revision
    raise ValueError("expected_revision must be a valid item revision")


def basis_matches(expected_revision: str, current_release_revision: str) -> bool:
    """Compare a request basis with the item's current release revision."""
    if _RELEASE_REVISION_RE.fullmatch(expected_revision):
        return expected_revision == current_release_revision
    return expected_revision == current_release_revision.rsplit("@revise:", 1)[0]


def normalize_acceptance_contract(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    if contract is None:
        return dict(DEFAULT_ACCEPTANCE_CONTRACT)
    if not isinstance(contract, Mapping):
        raise ValueError("acceptance_contract must be an object")
    normalized = json.loads(canonical_json(dict(contract)))
    if not isinstance(normalized, dict):
        raise ValueError("acceptance_contract must be an object")
    return normalized


def canonical_context_refs(refs: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Return an item's refs in the backend-independent form a release stores.

    Local row ids and timestamps differ between backends and across imports,
    so only the ref's content is frozen, in a stable order.
    """
    canonical = [
        {
            "ref_type": str(ref["ref_type"]),
            "url": str(ref["url"]),
            "label": str(ref.get("label") or ""),
        }
        for ref in refs
    ]
    canonical.sort(key=lambda ref: (ref["ref_type"], ref["url"], ref["label"]))
    return canonical


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def release_digest(
    aggregate_uuid: str,
    item_revision: str,
    acceptance_contract: Mapping[str, Any],
    context_refs: list[Mapping[str, Any]],
) -> str:
    """Return the sha256 hex digest of a release's canonical JSON."""
    document = {
        "aggregate_uuid": str(aggregate_uuid).lower(),
        "item_revision": item_revision,
        "acceptance_contract": dict(acceptance_contract),
        "context_refs": [dict(ref) for ref in context_refs],
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def validate_digest(digest: Any) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ValueError("release_digest must be 64 lowercase hexadecimal characters")
    return digest


def validate_commit_sha(commit_sha: Any) -> str:
    if not isinstance(commit_sha, str) or not _COMMIT_SHA_RE.fullmatch(commit_sha):
        raise ValueError("commit_sha must be 40 lowercase hexadecimal characters")
    return commit_sha


def release_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a stored release's JSON columns into Python values."""
    release = dict(row)
    for column in ("acceptance_contract", "context_refs"):
        value = release.get(column)
        if isinstance(value, str):
            release[column] = json.loads(value)
    return release
