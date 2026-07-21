"""Bounded, deterministic Tier-1 context candidates (item #1160).

Builds the small ranked packet described in
``docs/ops-upgrade-plan.md`` (Tier 1) and
``agentops/docs/plans/agentops/session-mechanization-plan.md``: a bounded
list of sprint items ranked by deterministic preference order, carrying an
explicit-target claim-eligibility marker and the cached projection watermark
age so a consuming session knows how stale its view is.

Ranking preference, in order:

1. an explicit item reference supplied by the caller;
2. exact path/manifest/glob/doc scope overlap (``sprintctl/db.py``'s
   ``ref`` scope model, from item #1155);
3. items carrying other linked governing documentation ("ownership
   boundaries");
4. deterministic lexical overlap between caller-supplied query terms and
   item title/description text -- no semantic model, no fuzzy scoring;
5. remaining repo-level candidates, in the existing ready-item order.

Only rank 1 (an explicit target that was actually found) is ever marked
``claim_eligible``. Ranks 2-5 are advisory context only -- this module never
creates or mutates a claim itself; it only exposes the policy marker for a
caller (e.g. a Tier-1 harness wrapper) to act on.

This module is pure: it takes already-fetched rows and returns a plain dict,
so it works identically against the SQLite (``sprintctl/db.py``) and
PostgreSQL (``sprintctl/pg.py``) backends -- callers fetch backend-specific
rows first (``get_ready_items``, ``list_refs_for_items``, ``get_work_item``)
and pass them in here.
"""

from __future__ import annotations

import re
from typing import Iterable

from . import db as _db

DEFAULT_CANDIDATE_LIMIT = 5
MAX_CANDIDATE_LIMIT = 20

RANK_EXPLICIT_TARGET = 1
RANK_PATH_OVERLAP = 2
RANK_LINKED_DOC = 3
RANK_LEXICAL = 4
RANK_REPO_LEVEL = 5

RANK_REASONS = {
    RANK_EXPLICIT_TARGET: "explicit-target",
    RANK_PATH_OVERLAP: "path-scope-overlap",
    RANK_LINKED_DOC: "linked-documentation",
    RANK_LEXICAL: "lexical-match",
    RANK_REPO_LEVEL: "repo-level",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ContextCandidatesError(ValueError):
    """Raised for invalid input to candidate ranking (e.g. a bad bound)."""


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def _candidate_priority(item: dict) -> int | None:
    if "effective_priority" in item:
        return item["effective_priority"]
    return _db.effective_priority(item)


def _make_candidate(item: dict, rank: int, reason_detail: str) -> dict:
    return {
        "item_id": item["id"],
        "title": item.get("title"),
        "status": item.get("status"),
        "track": item.get("track_name"),
        "priority": _candidate_priority(item),
        "rank": rank,
        "rank_reason": RANK_REASONS[rank],
        "reason_detail": reason_detail,
        "claim_eligible": rank == RANK_EXPLICIT_TARGET and item.get("status") == "pending",
    }


def build_context_candidates(
    *,
    ready_items: list[dict],
    refs_by_item: dict[int, list[dict]],
    explicit_item_id: int | None = None,
    explicit_item: dict | None = None,
    target_paths: Iterable[str] = (),
    query: str | None = None,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    watermark: dict | None = None,
) -> dict:
    """Return a bounded, deterministically ranked context-candidate packet.

    ``ready_items`` must already be in the caller's preferred stable order
    for repo-level fallback (``get_ready_items`` returns priority/created_at/id
    order) -- this function never reorders within a tier beyond stable sort.

    ``explicit_item`` is the caller-fetched work item for ``explicit_item_id``
    (``None`` if it does not exist or was not looked up). Passing
    ``explicit_item_id`` without ``explicit_item`` records the target as
    "not found" rather than raising.

    ``watermark`` is passed through unchanged (typically the dict already
    built by ``sprintctl/cli.py``'s projection-health helpers) so this
    function stays backend/storage agnostic.
    """
    if limit <= 0:
        raise ContextCandidatesError("limit must be a positive integer")
    bound = min(limit, MAX_CANDIDATE_LIMIT)

    normalized_paths = [path for path in target_paths if path]
    query_terms = _tokenize(query)

    selected: list[dict] = []
    selected_ids: set[int] = set()
    pool_ids: set[int] = {item["id"] for item in ready_items}

    def _add(item: dict, rank: int, reason_detail: str) -> None:
        if item["id"] in selected_ids or len(selected) >= bound:
            return
        selected.append(_make_candidate(item, rank, reason_detail))
        selected_ids.add(item["id"])

    explicit_found = explicit_item is not None
    if explicit_item_id is not None:
        pool_ids.add(explicit_item_id)
        if explicit_found:
            _add(explicit_item, RANK_EXPLICIT_TARGET, "Explicit item reference supplied by caller.")

    def _remaining_pool() -> list[dict]:
        return [item for item in ready_items if item["id"] not in selected_ids]

    if normalized_paths and len(selected) < bound:
        for item in _remaining_pool():
            if len(selected) >= bound:
                break
            refs = refs_by_item.get(item["id"], [])
            if any(
                _db.scope_ref_matches_path(ref, path)
                for ref in refs
                for path in normalized_paths
            ):
                _add(item, RANK_PATH_OVERLAP, "Item ref scope overlaps a given target path.")

    if len(selected) < bound:
        for item in _remaining_pool():
            if len(selected) >= bound:
                break
            refs = refs_by_item.get(item["id"], [])
            if any(ref.get("ref_type") == "doc" for ref in refs):
                _add(item, RANK_LINKED_DOC, "Item has linked governing documentation.")

    if query_terms and len(selected) < bound:
        scored: list[tuple[int, dict]] = []
        for item in _remaining_pool():
            haystack = _tokenize(item.get("title")) | _tokenize(item.get("description"))
            overlap = len(haystack & query_terms)
            if overlap > 0:
                scored.append((-overlap, item))
        # Stable sort: ties keep the incoming ready-item order.
        scored.sort(key=lambda pair: pair[0])
        for _, item in scored:
            _add(item, RANK_LEXICAL, "Deterministic lexical overlap with query terms.")

    for item in _remaining_pool():
        if len(selected) >= bound:
            break
        _add(item, RANK_REPO_LEVEL, "Repo-level candidate; no stronger match found.")

    return {
        "contract_version": "1",
        "explicit_target": (
            {"item_id": explicit_item_id, "found": explicit_found}
            if explicit_item_id is not None
            else None
        ),
        "bound": bound,
        "truncated": len(pool_ids) > len(selected),
        "watermark": watermark,
        "candidates": selected,
    }


__all__ = [
    "ContextCandidatesError",
    "DEFAULT_CANDIDATE_LIMIT",
    "MAX_CANDIDATE_LIMIT",
    "RANK_EXPLICIT_TARGET",
    "RANK_LEXICAL",
    "RANK_LINKED_DOC",
    "RANK_PATH_OVERLAP",
    "RANK_REASONS",
    "RANK_REPO_LEVEL",
    "build_context_candidates",
]
