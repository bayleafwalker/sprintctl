"""Private per-candidate backlog seeding; backends own locks and transactions.

The existing scoped deduplication identity is the target authority/sprint plus
source event ID.
Earlier successful candidates remain committed after a later failure. A retry
returns newly created items, rather than replaying an earlier response.

Conforming-writer deduplication is bounded to SQLite writer serialization and
default PostgreSQL READ COMMITTED. No arbitrary-isolation snapshot, fabricated
link protection, whole-command transaction or public provider API is defined.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Protocol

from . import contracts


@dataclass(frozen=True)
class SeedParticipant:
    get_sprint: Callable[[int], dict | None]
    candidate: Callable[[int, int], dict | None]
    seeded_event_ids: Callable[[int], set]
    create_track: Callable[[int, str], int]
    create_item: Callable[..., int]
    insert_event: Callable[..., int]
    get_item: Callable[[int], dict | None]


class Backend(Protocol):
    def get_sprint(self, ident: int) -> dict | None: ...
    def candidates(self, source: int) -> list[dict]: ...
    def seeded_event_ids(self, target: int) -> set: ...
    def transaction(self, source: int, target: int) -> AbstractContextManager[SeedParticipant]: ...


def _validate_sprints(reader, source: int, target: int) -> None:
    for label, ident in (("Source", source), ("Target", target)):
        if reader.get_sprint(ident) is None:
            raise ValueError(f"{label} sprint #{ident} not found")


def seed_candidate(p: SeedParticipant, source: int, target: int, ident: int, actor: str) -> dict | None:
    # The backend holds sorted distinct sprint locks (or SQLite's writer lock).
    _validate_sprints(p, source, target)
    candidate = p.candidate(source, ident)
    if candidate is None or ident in p.seeded_event_ids(target):
        return None
    summary = candidate["payload"].get("summary", f"Knowledge item from event #{candidate['id']}")
    title = f"[knowledge] {summary}"
    track = p.create_track(target, "knowledge")
    item = p.create_item(target, track, title)
    contracts.require_generic_event_write_allowed("backlog-seeded")
    p.insert_event(target, actor, "backlog-seeded", source_type="system", work_item_id=item,
        payload={"source_sprint_id": source,
                 "source_item_id": candidate.get("work_item_id"),
                 "source_event_id": candidate["id"],
                 "source_event_type": candidate["event_type"]})
    result = p.get_item(item)
    if result is None:
        raise ValueError(f"Seeded item #{item} could not be read")
    return result


def seed_candidates(backend: Backend, source: int, target: int, actor: str) -> list[dict]:
    _validate_sprints(backend, source, target)
    candidates = backend.candidates(source)
    if not candidates:
        return []
    # Preserve eager decoding and errors before any candidate write/precommit.
    already_seeded = backend.seeded_event_ids(target)
    selected = [candidate["id"] for candidate in candidates if candidate["id"] not in already_seeded]
    result = []
    for ident in selected:
        with backend.transaction(source, target) as participant:
            copied = seed_candidate(participant, source, target, ident, actor)
        if copied is not None:
            result.append(copied)
    return result
