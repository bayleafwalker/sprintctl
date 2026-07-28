"""The exact served-mode CLI command allowlist for sprintctl #1195.

``LEGACY_REMOTE_COMMAND_PARITY`` in ``vuoro_adapter.py`` describes parity at
the level of prose command groups ("claim heartbeat|handoff|release",
"item status / sprint status"). That is not precise enough to gate a CLI
command before it opens SQLite, reads recovery state, or performs any other
side effect: each entry here is one exact Click command path.

Resolution notes from planning:

- ``claim heartbeat|handoff|release`` is three separate Click commands, all
  mapped to ``work.claim.arbitrate`` (an immutable authority-command record;
  the operation itself, not the CLI verb, determines the transition).
- ``item status`` and ``sprint status`` are two separate Click commands,
  both mapped to ``work.lifecycle.arbitrate`` for the same reason.
- ``next-work`` is a single Click command whose behavior branches on
  ``--project``: without it, it maps to ``work.read.next-work``; with it, to
  ``work.project.next-work``. A route's ``precondition`` field encodes which
  option values the mapping is valid for.
- ``sprint list`` and (per its own catalog-parity entry) ``item show`` also
  accept a ``--project`` value, but no catalog operation exists for a
  project-scoped sprint listing or item read. Their served route only
  applies when ``--project`` is absent; the guard must reject the
  project-scoped invocation in served mode rather than silently treating it
  as project-scoped or falling back to a database read.
- ``work.project.batch`` ("project dispatch batching" in the parity table)
  has an application-layer implementation
  (``ProjectWorkApplication.invoke``, ``application.py:799``) but no
  existing CLI command invokes it. There is nothing to route: it is left
  out of this table. Exposing it is new CLI surface, not a legacy-command
  migration, and is out of #1195's scope.

This module intentionally does not import ``click`` or any store module — it
is pure data, checkable independent of the CLI wiring that will consume it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ServedRoute:
    command_path: str
    operation: str
    precondition: str = ""
    notes: str = ""


SERVED_COMMAND_ROUTES: tuple[ServedRoute, ...] = (
    ServedRoute(
        "identity.current",
        "work.identity.current",
        notes="Internal dependency for authenticated durable-command actor selection.",
    ),
    ServedRoute("usage.context", "work.read.context", precondition="project_path is None"),
    ServedRoute(
        "usage.context",
        "work.project.context",
        precondition="project_path is not None",
        notes="The catalog resolves its own canonical project binding; the CLI does not send project.toml data.",
    ),
    ServedRoute("handoff", "work.read.handoff"),
    ServedRoute("handoff.record", "work.handoff.record", notes="Internal post-output recording step for `handoff`."),
    ServedRoute(
        "sprint.list",
        "work.read.sprints",
        precondition="project_path is None",
    ),
    ServedRoute("sprint.create", "work.sprint.create"),
    ServedRoute(
        "sprint.list",
        "work.project.sprints",
        precondition="project_path is not None",
        notes="The catalog resolves its own canonical project binding; the CLI does not send project.toml data.",
    ),
    ServedRoute("item.show", "work.read.item"),
    ServedRoute("item.list", "work.read.items", precondition="project_path is None and not as_fzf"),
    ServedRoute(
        "item.list",
        "work.project.items",
        precondition="project_path is not None and not as_fzf",
        notes="The catalog resolves its canonical project binding and authorizes every member.",
    ),
    ServedRoute("claim.list", "work.read.claims"),
    ServedRoute("claim.list-sprint", "work.read.claims"),
    ServedRoute("claim.resume", "work.read.claims"),
    ServedRoute("claim.show", "work.read.claim"),
    ServedRoute("item.ref.add", "work.item.ref.add"),
    ServedRoute("item.ref.list", "work.read.item"),
    ServedRoute("item.ref.remove", "work.item.ref.remove"),
    ServedRoute("item.dep.add", "work.item.dep.add"),
    ServedRoute("item.dep.list", "work.read.item"),
    ServedRoute("item.dep.remove", "work.item.dep.remove"),
    ServedRoute(
        "next-work",
        "work.read.next-work",
        precondition="project_path is None",
    ),
    ServedRoute(
        "next-work.explain",
        "work.read.next-work-explain",
        precondition="project_path is None and explain",
    ),
    ServedRoute(
        "next-work",
        "work.project.next-work",
        precondition="project_path is not None",
        notes="Same Click command as the row above; operation depends on --project.",
    ),
    ServedRoute("claim.start", "work.claim.start"),
    ServedRoute("claim.heartbeat", "work.claim.arbitrate"),
    ServedRoute("claim.handoff", "work.claim.arbitrate"),
    ServedRoute("claim.release", "work.claim.arbitrate"),
    ServedRoute("item.status", "work.lifecycle.arbitrate"),
    ServedRoute("item.done-from-claim", "work.lifecycle.arbitrate"),
    ServedRoute("sprint.status", "work.lifecycle.arbitrate"),
    ServedRoute("event.observation.add", "work.evidence.ingest"),
    ServedRoute("event.list", "work.read.events"),
    ServedRoute("event.add", "work.event.add"),
    ServedRoute("event.log", "work.event.add", notes="Alias for `event add`."),
    ServedRoute("item.add", "work.item.create"),
    ServedRoute("item.edit", "work.item.edit"),
    ServedRoute("sprint.show", "work.read.sprint"),
    ServedRoute("sprint.show.detail", "work.read.sprint-detail"),
    ServedRoute("item.note", "work.item.note"),
    ServedRoute("authority.sync", "work.batch.apply"),
    ServedRoute("pilot.cutover-evidence", "work.pilot.cutover-evidence"),
)


# Every executable Click leaf is classified here, rather than leaving served
# support to an accidental consequence of whether its callback happens to call
# ``_get_store``.  ``catalog`` commands have a Vuoro route (some select a
# route from their options); ``local`` commands are intentionally backend-free
# diagnostics; and ``unavailable`` commands must fail before any store is
# constructed.  Keep this table in lockstep with the Click tree -- the CLI
# imports it and asserts exact coverage at import time.
ServedDisposition = Literal["catalog", "local", "unavailable"]

SERVED_COMMAND_DISPOSITIONS: dict[str, ServedDisposition] = {
    "doctor": "local",
    "sprint create": "catalog",
    "sprint show": "catalog",
    "sprint status": "catalog",
    "sprint list": "catalog",
    "sprint kind": "unavailable",
    "sprint backlog-seed": "unavailable",
    "item add": "catalog",
    "item edit": "catalog",
    "item priority": "unavailable",
    "item show": "catalog",
    "item list": "catalog",
    "item note": "catalog",
    "item status": "catalog",
    "item done-from-claim": "catalog",
    "item ref add": "catalog",
    "item ref list": "catalog",
    "item ref remove": "catalog",
    "item dep add": "catalog",
    "item dep list": "catalog",
    "item dep remove": "catalog",
    "event observation add": "unavailable",
    "event observation list": "unavailable",
    "event add": "catalog",
    "event log": "catalog",
    "event list": "catalog",
    # This reads only the local durable outbox and terminal-decision receipts.
    # It is the diagnostic needed to recover an ordered served sync stream;
    # unlike the other authority administration commands it never opens a
    # database backend or mutates authority state.
    "authority status": "local",
    # Per-repository rollout configuration is local state.  It must remain
    # reachable in served consumers so a named pilot can be enabled without a
    # fictitious catalog operation.
    "authority mode": "local",
    "authority submit": "unavailable",
    "authority sync": "catalog",
    # Reconciliation reads the served authority ledger through its dedicated
    # client and writes only local terminal receipts; it is not a catalog
    # operation and must not be rejected by the generic served guard.
    "authority reconcile": "local",
    "authority quarantine": "local",
    "authority rollover": "local",
    "authority recover-proof": "unavailable",
    "authority clear-proof": "unavailable",
    "pilot status": "unavailable",
    "pilot enable": "unavailable",
    "pilot disable": "unavailable",
    "pilot verify": "unavailable",
    "pilot sync": "unavailable",
    "pilot cutover-evidence": "catalog",
    "projection-reads status": "unavailable",
    "projection-reads enable": "unavailable",
    "projection-reads disable": "unavailable",
    "takeup sweep": "unavailable",
    "takeup take": "unavailable",
    "takeup release": "unavailable",
    "takeup list": "unavailable",
    "takeup show": "unavailable",
    "maintain check": "unavailable",
    "maintain sweep": "unavailable",
    "maintain carryover": "unavailable",
    "db vacuum": "unavailable",
    "db integrity": "unavailable",
    "db recover-from-remote": "unavailable",
    "export": "unavailable",
    "import": "unavailable",
    "claim create": "unavailable",
    "claim start": "catalog",
    "claim heartbeat": "catalog",
    "claim release": "catalog",
    "claim handoff": "catalog",
    "claim list": "catalog",
    "claim list-sprint": "catalog",
    "claim show": "catalog",
    "claim resume": "catalog",
    "claim recover": "catalog",
    "handoff": "catalog",
    "agent-protocol": "local",
    "next-work": "catalog",
    "context-candidates": "unavailable",
    "session resume": "unavailable",
    # The no-option form prints static command help. ``usage --context`` is
    # catalog-backed; the CLI selects that disposition from parsed options.
    "usage": "local",
    "git-context": "local",
    "render": "unavailable",
    "remote-schema check": "unavailable",
    "remote-schema migrate": "unavailable",
    "migrate-to-remote": "unavailable",
    "remote-backfill": "unavailable",
    "repo list": "unavailable",
    "repo delete": "unavailable",
}


_ROUTES_BY_COMMAND: dict[str, tuple[ServedRoute, ...]] = {}
for _route in SERVED_COMMAND_ROUTES:
    _ROUTES_BY_COMMAND[_route.command_path] = (
        *_ROUTES_BY_COMMAND.get(_route.command_path, ()),
        _route,
    )


def routes_for(command_path: str) -> tuple[ServedRoute, ...]:
    """All routes registered for an exact command path (usually zero or one; `next-work` has two)."""

    return _ROUTES_BY_COMMAND.get(command_path, ())


__all__ = [
    "ServedDisposition",
    "ServedRoute",
    "SERVED_COMMAND_DISPOSITIONS",
    "SERVED_COMMAND_ROUTES",
    "routes_for",
]
