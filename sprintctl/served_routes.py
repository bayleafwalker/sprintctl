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


@dataclass(frozen=True, slots=True)
class ServedRoute:
    command_path: str
    operation: str
    precondition: str = ""
    notes: str = ""


SERVED_COMMAND_ROUTES: tuple[ServedRoute, ...] = (
    ServedRoute(
        "sprint.list",
        "work.read.sprints",
        precondition="project_path is None",
        notes="`sprint list --project ...` has no catalog equivalent; reject it in served mode.",
    ),
    ServedRoute("item.show", "work.read.item"),
    ServedRoute("item.list", "work.read.items", precondition="project_path is None and not as_fzf"),
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
    ServedRoute("sprint.status", "work.lifecycle.arbitrate"),
    ServedRoute("event.observation.add", "work.evidence.ingest"),
    ServedRoute("event.list", "work.read.events"),
    ServedRoute("event.add", "work.event.add"),
    ServedRoute("item.add", "work.item.create"),
    ServedRoute("sprint.show", "work.read.sprint"),
    ServedRoute("item.note", "work.item.note"),
    ServedRoute("authority.sync", "work.batch.apply"),
    ServedRoute("pilot.cutover-evidence", "work.pilot.cutover-evidence"),
)


_ROUTES_BY_COMMAND: dict[str, tuple[ServedRoute, ...]] = {}
for _route in SERVED_COMMAND_ROUTES:
    _ROUTES_BY_COMMAND[_route.command_path] = (
        *_ROUTES_BY_COMMAND.get(_route.command_path, ()),
        _route,
    )


def routes_for(command_path: str) -> tuple[ServedRoute, ...]:
    """All routes registered for an exact command path (usually zero or one; `next-work` has two)."""

    return _ROUTES_BY_COMMAND.get(command_path, ())


__all__ = ["ServedRoute", "SERVED_COMMAND_ROUTES", "routes_for"]
