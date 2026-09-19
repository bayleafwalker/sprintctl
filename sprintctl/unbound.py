"""Work that is not bound to a decision (S3): ``work.read.unbound``.

S3 makes a decision, bound to the Release that was picked up and to evidence,
the only writer of an item's terminal status.  Three kinds of item fall short
of that, each for a different reason and with a different remedy:

``legacy_done``
    Done before decisions existed, with no decision.  The remedy is one
    legacy re-mark (schema 16 / SQLite 25).
``decided_unreleased``
    Done by a terminal decision that names no Release: the item was closed
    without an execution reservation ever freezing what was picked up (or
    before releases existed).  The closure is recorded but not bound to what
    it closed; it is immutable, so this is reported, not repaired.
``released_undecided``
    Open, with a current Release frozen by an execution reservation, and no
    decision yet: picked-up work waiting for the decision that must bind that
    Release.

A done item that is neither legacy nor bound to a matching decision is not a
category: the database refuses to commit one (PostgreSQL schema 14 / SQLite
23 terminal guards), so there is nothing to list.

The SQL here is shared by both backends; each passes its named-placeholder
style and its tenant predicate.  ``legacy`` is a boolean on PostgreSQL and 0/1 on SQLite,
and is used bare so that both read it the same way.
"""

from __future__ import annotations

from typing import Any, Callable

CATEGORIES = ("legacy_done", "decided_unreleased", "released_undecided")
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_ITEM_COLUMNS = (
    "wi.id, wi.sprint_id, wi.track_id, wi.title, wi.status, wi.legacy, "
    "wi.resolution, wi.terminal_decision_id, wi.updated_at"
)

# The current release: frozen since the item's latest revise decision, most
# recently reserved first -- the rule ``current_release`` applies.
_CURRENT_RELEASE = (
    "(SELECT wr.release_digest FROM work_release wr "
    "WHERE {wr_tenant} wr.work_item_id = wi.id AND wr.revise_count = ("
    "SELECT COUNT(*) FROM work_decision rd WHERE {rd_tenant} "
    "rd.work_item_id = wi.id AND rd.kind = 'revise') "
    "ORDER BY (SELECT MAX(r.id) FROM reservation r WHERE {r_tenant} "
    "r.release_digest = wr.release_digest) DESC NULLS LAST, wr.id DESC LIMIT 1)"
)


def normalize_arguments(
    category: Any = None, limit: Any = None
) -> tuple[tuple[str, ...], int]:
    """Validate the optional category filter and the per-category limit."""
    if category is None:
        categories = CATEGORIES
    elif category in CATEGORIES:
        categories = (category,)
    else:
        raise ValueError("category must be one of " + ", ".join(CATEGORIES))
    if limit is None:
        limit = DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return categories, limit


def _queries(tenant: Callable[[str], str]) -> dict[str, tuple[str, str, str]]:
    """Return ``{category: (columns, from, where)}``."""
    current_release = _CURRENT_RELEASE.format(
        wr_tenant=tenant("wr"), rd_tenant=tenant("rd"), r_tenant=tenant("r")
    )
    decision_join = "d.id = wi.terminal_decision_id"
    if tenant("d"):
        decision_join += " AND d.repo_id = wi.repo_id"
    return {
        "legacy_done": (
            _ITEM_COLUMNS,
            "work_item wi",
            f"{tenant('wi')} wi.status = 'done' AND wi.legacy "
            "AND wi.terminal_decision_id IS NULL",
        ),
        "decided_unreleased": (
            f"{_ITEM_COLUMNS}, d.kind AS decision_kind, d.actor AS decision_actor, "
            "d.created_at AS decided_at",
            f"work_item wi JOIN work_decision d ON {decision_join}",
            f"{tenant('wi')} wi.status = 'done' AND d.release_digest IS NULL",
        ),
        "released_undecided": (
            f"{_ITEM_COLUMNS}, {current_release} AS release_digest",
            "work_item wi",
            f"{tenant('wi')} wi.status <> 'done' AND {current_release} IS NOT NULL",
        ),
    }


def list_unbound(
    query_all: Callable[[str, dict], list[dict]],
    *,
    param: Callable[[str], str],
    tenant: Callable[[str], str],
    params: dict[str, Any],
    sprint_id: int | None = None,
    category: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """Run the unbound read over one backend.

    ``param(name)`` renders a named placeholder; ``tenant(alias)`` returns
    the tenant predicate for a table alias followed by ``AND`` (``""`` when
    the backend has no tenant column); ``params`` holds the tenant values.
    """
    categories, limit = normalize_arguments(category, limit)
    queries = _queries(tenant)
    params = dict(params)
    sprint_clause = ""
    if sprint_id is not None:
        sprint_clause = f" AND wi.sprint_id = {param('sprint_id')}"
        params["sprint_id"] = sprint_id
    categories_result: dict[str, Any] = {}
    for name in categories:
        columns, source, where = queries[name]
        [count_row] = query_all(
            f"SELECT COUNT(*) AS n FROM {source} WHERE {where}{sprint_clause}",
            params,
        )
        rows = query_all(
            f"SELECT {columns} FROM {source} WHERE {where}{sprint_clause} "
            f"ORDER BY wi.id LIMIT {int(limit)}",
            params,
        )
        categories_result[name] = {
            "count": int(count_row["n"]),
            "items": [_item_row(row) for row in rows],
        }
    totals = query_all(
        "SELECT wi.resolution AS resolution, COUNT(*) AS n FROM work_item wi "
        f"WHERE {tenant('wi')} wi.status = 'done'{sprint_clause} GROUP BY wi.resolution",
        params,
    )
    return {
        "sprint_id": sprint_id,
        "limit": limit,
        "categories": categories_result,
        "resolutions": resolution_totals(totals),
    }


def _item_row(row: dict) -> dict:
    item = dict(row)
    if item.get("legacy") is not None:
        item["legacy"] = bool(item["legacy"])
    return item


def resolution_totals(rows: list[dict]) -> dict[str, int]:
    """Fold ``(resolution, n)`` rows of done items into resolution counts.

    Same shape as :func:`sprintctl.calc.resolution_counts`: a done item with
    no resolution is legacy done (the terminal guards admit no other kind).
    """
    from .calc import RESOLUTION_METRIC_KEYS

    counts = {key: 0 for key in RESOLUTION_METRIC_KEYS}
    legacy_done = 0
    for row in rows:
        n = int(row["n"])
        if row.get("resolution") in counts:
            counts[row["resolution"]] += n
        else:
            legacy_done += n
    decided = sum(counts.values())
    return {
        **counts,
        "decided_done": decided,
        "legacy_done": legacy_done,
        "done": decided + legacy_done,
    }
