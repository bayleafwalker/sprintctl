"""Private shared reduction for the existing read-only obligation report.

Drivers own query selection, tenant scoping, validation and row decoding. This
report does not enforce acceptance, infer evidence kinds or establish a
snapshot across its reads. Any evidence digest satisfies all declared labels.
"""
from __future__ import annotations

from typing import Protocol

from . import unbound as _unbound


class Reader(Protocol):
    def release(self, digest: str) -> dict | None: ...
    def accepted_decisions(self, digest: str) -> list[dict]: ...
    def release_digests(self, sprint_id: int | None) -> list[str]: ...


def unmet_obligations(reader: Reader, release_digest: str) -> list[dict]:
    release = reader.release(release_digest)
    if release is None:
        return []
    obligations = list(release["acceptance_contract"].get("evidence_obligations") or [])
    if not obligations:
        return []
    decisions = reader.accepted_decisions(release_digest)
    unmet: list[dict] = []
    for decision in decisions:
        if decision["evidence_digests"]:
            continue
        unmet.append({
            "item_id": decision["work_item_id"],
            "decision_id": decision["id"],
            "release_digest": release_digest,
            "unmet": list(obligations),
        })
    return unmet


def list_unmet_obligations(
    reader: Reader, *, sprint_id: int | None = None, limit: int | None = None
) -> dict:
    if limit is None:
        limit = _unbound.DEFAULT_LIMIT
    digests = reader.release_digests(sprint_id)
    rows: list[dict] = []
    for digest in digests:
        rows.extend(unmet_obligations(reader, digest))
    rows.sort(key=lambda row: (row["item_id"], row["decision_id"]))
    return {"count": len(rows), "items": rows[: int(limit)]}
