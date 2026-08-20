"""Bounded, read-only work-item context for native runtime adapters.

This module deliberately owns no cursor, hook, or mutation state.  It derives
one allowlisted projection from Sprintctl's authoritative item row and exposes
the same opaque status revision used by the owner's compare-and-swap path.
"""

from __future__ import annotations

import json
from typing import Any


PROJECTION_CONTRACT = "work-item-context/v1"
PROVIDER_ID = "sprintctl.work-item"
MAX_PROJECTION_BYTES = 4_096
MAX_TITLE_BYTES = 1_024


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "…"
    available = limit - len(suffix.encode("utf-8"))
    truncated = encoded[:available].decode("utf-8", errors="ignore") + suffix
    return truncated, True


def project_work_item(
    backend: Any, store: Any, *, repo_id: str, item_id: int
) -> dict[str, Any] | None:
    """Return an allowlisted projection, or ``None`` for an unknown item."""

    item = backend.get_work_item(store, item_id)
    if item is None:
        return None
    title, truncated = _truncate_utf8(str(item["title"]), MAX_TITLE_BYTES)
    revision = backend.item_status_revision(item)
    projection = {
        "contract_version": PROJECTION_CONTRACT,
        "provider_id": PROVIDER_ID,
        "resource_id": f"{repo_id}#{item_id}",
        "revision": revision,
        "data_class": "untrusted-work-state",
        "item": {
            "id": item_id,
            "title": title,
            "status": item["status"],
            "priority": item.get("priority"),
            "assignee": item.get("assignee"),
        },
        "truncated": truncated,
    }
    encoded = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_PROJECTION_BYTES:  # defensive if allowlisted fields grow
        raise ValueError("work-item projection exceeds its hard byte budget")
    return projection


def validate_status_mutation(
    backend: Any,
    store: Any,
    *,
    repo_id: str,
    item_id: int,
    expected_revision: str | None,
) -> dict[str, Any] | None:
    """Advisory precheck using the owner's status revision.

    This is intentionally read-only.  The status mutation performs the same
    comparison again while holding the owner transaction/lock.
    """

    projection = project_work_item(
        backend, store, repo_id=repo_id, item_id=item_id
    )
    if projection is None:
        return None
    current = projection["revision"]
    if expected_revision is None:
        return {
            "allowed": False,
            "reason": "expected revision is required",
            "current_revision": current,
            "projection": projection,
        }
    try:
        backend.validate_item_status_revision(expected_revision)
    except ValueError:
        return {
            "allowed": False,
            "reason": "expected revision is malformed",
            "current_revision": current,
            "projection": projection,
        }
    return {
        "allowed": expected_revision == current,
        "reason": (
            "revision matches"
            if expected_revision == current
            else "item status revision changed"
        ),
        "current_revision": current,
        "projection": projection,
    }


__all__ = [
    "MAX_PROJECTION_BYTES",
    "MAX_TITLE_BYTES",
    "PROJECTION_CONTRACT",
    "PROVIDER_ID",
    "project_work_item",
    "validate_status_mutation",
]
