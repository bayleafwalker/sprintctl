"""Private Release policies shared by backend-owned transactions.

Storage adapters retain execution, decoding, locking and transaction ownership.
These helpers neither open transactions nor normalize public requests.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import releases as _releases


def _basis_query(*, ph: str, tenant_scoped: bool, lock_item: bool) -> str:
    event_scope = "e.repo_id = wi.repo_id AND " if tenant_scoped else ""
    decision_scope = "d.repo_id = wi.repo_id AND " if tenant_scoped else ""
    item_scope = f"wi.repo_id = {ph} AND " if tenant_scoped else ""
    return (
        "SELECT wi.*, "
        f"(SELECT COUNT(*) FROM event e WHERE {event_scope}e.work_item_id = wi.id "
        f" AND e.event_type = {ph}) AS edit_version, "
        f"(SELECT COUNT(*) FROM work_decision d WHERE {decision_scope}d.work_item_id = wi.id "
        " AND d.kind = 'revise') AS revise_count "
        f"FROM work_item wi WHERE {item_scope}wi.id = {ph}"
        + (" FOR UPDATE OF wi" if lock_item else "")
    )


def _current_release_query(*, ph: str, tenant_scoped: bool) -> str:
    release_scope = f"wr.repo_id = {ph} AND " if tenant_scoped else ""
    decision_scope = "d.repo_id = wr.repo_id AND " if tenant_scoped else ""
    reservation_scope = "r.repo_id = wr.repo_id AND " if tenant_scoped else ""
    return f"""
        SELECT wr.* FROM work_release wr
        WHERE {release_scope}wr.work_item_id = {ph}
          AND wr.revise_count = (
              SELECT COUNT(*) FROM work_decision d
              WHERE {decision_scope}d.work_item_id = wr.work_item_id AND d.kind = 'revise'
          )
        ORDER BY (
            SELECT MAX(r.id) FROM reservation r WHERE {reservation_scope}r.release_digest = wr.release_digest
        ) DESC NULLS LAST, wr.id DESC
        LIMIT 1
        """


def _basis_from_item(
    item: dict | None,
    item_id: int,
    *,
    description_revision: Callable[[dict, int], str],
) -> tuple[dict, str, int]:
    if item is None:
        raise ValueError(f"Work item #{item_id} not found")
    edit_version = int(item.pop("edit_version"))
    revise_count = int(item.pop("revise_count"))
    edit_revision = description_revision(item, edit_version)
    return item, _releases.release_revision(edit_revision, revise_count), revise_count


def _freeze(
    item: dict,
    revision: str,
    revise_count: int,
    *,
    actor: str,
    acceptance_contract: dict,
    refs: Iterable[Mapping[str, Any]],
    canonicalize_refs: Callable[[Iterable[Mapping[str, Any]]], list[dict]],
    insert: Callable[[Callable[[], tuple[int, str, int, str, str, str, str]]], None],
) -> str:
    context_refs = canonicalize_refs(refs)
    digest = _releases.release_digest(
        item["aggregate_uuid"], revision, acceptance_contract, context_refs
    )

    def values() -> tuple[int, str, int, str, str, str, str]:
        # The backend binds execute before consuming this supplier once.
        return (
            item["id"],
            revision,
            revise_count,
            digest,
            _releases.canonical_json(acceptance_contract),
            _releases.canonical_json(context_refs),
            actor,
        )

    insert(values)
    return digest


def _resolve_decision_release(
    item_id: int,
    digest: str | None,
    *,
    current: Callable[[], dict | None],
    member: Callable[[str], bool],
) -> str | None:
    if digest is None:
        release = current()
        return release["release_digest"] if release else None
    if not member(digest):
        raise _releases.ReleaseMismatch(
            f"release {digest} is not a release of item #{item_id}"
        )
    return digest
