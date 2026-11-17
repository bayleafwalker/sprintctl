"""Operator orientation: the ``status`` home view and its shared helpers.

``status`` is built on the ``usage --context`` snapshot (ContextContract v1),
so local and served backends feed the same renderer.  Helpers here are also
used by ``usage --context``, ``next-work`` and ``item list`` so ages, fallback
notices and next commands are spelled once.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click

STATUS_SCHEMA = "sprintctl.status/v1"
ROWS_PER_SECTION = 3  # worst case 24 lines: 15 fixed + 3 sections x (3 rows + 'more')


def format_age(seconds: int | float | None) -> str:
    """Relative age for people: ``40s``, ``12m``, ``5h``, ``17d``."""
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def fallback_reason(config: Any) -> str | None:
    """Why a local target is an unintended fallback, or ``None``.

    Local mode only reads a repository database through ``SPRINTCTL_DB``; with
    it unset the home database is used, whether or not a marker exists.
    """
    if config is None or config.mode != "local" or os.environ.get("SPRINTCTL_DB"):
        return None
    return "no .sprintctl/ marker" if config.marker is None else "SPRINTCTL_DB unset"


def db_label() -> str:
    """The local sqlite database path in use (only meaningful in local mode)."""
    from . import db as _db  # noqa: PLC0415

    return _tilde(_db.get_db_path())


def fallback_notice(config: Any) -> str | None:
    """Two-line notice for empty results read from a fallback target."""
    reason = fallback_reason(config)
    if reason is None:
        return None
    return (
        f"note: read {db_label()}, a fallback target ({reason}); "
        "this is probably not the repository you meant\n"
        "  fix: cd into the repository, or set SPRINTCTL_DB=<repo>/.sprintctl/sprintctl.db"
    )


def _root_obj() -> dict:
    ctx = click.get_current_context(silent=True)
    return (ctx.find_root().obj if ctx is not None else None) or {}


def echo_fallback_notice(*, err: bool = False) -> None:
    """Print the fallback notice when the resolved config is a fallback."""
    notice = fallback_notice(_root_obj().get("backend_config"))
    if notice:
        click.echo(notice, err=err)


def no_active_sprint() -> None:
    """Exit 1 with the standard no-sprint error, naming a fallback target."""
    click.echo("No active sprint found. Use --sprint-id to specify one.", err=True)
    echo_fallback_notice(err=True)
    sys.exit(1)


def _is_served() -> bool:
    config = _root_obj().get("backend_config")
    return config is not None and config.mode == "served"


def reserve_command(item_id: int, *, served: bool | None = None) -> str:
    """Pasteable reserve command; served mode binds the actor to the identity."""
    served = _is_served() if served is None else served
    actor = "" if served else ' --actor "$USER"'
    return f"sprintctl reservation reserve --item-id {item_id}{actor}"


def next_action_command(action: dict[str, Any], *, served: bool | None = None) -> str | None:
    """Exact command that acts on a ContextContract ``next_action``."""
    kind = action.get("kind")
    item, blocker, reservation = (action.get(k) for k in ("item_id", "blocker_item_id", "reservation_id"))
    if kind in {"review-stale-reservation", "inspect-active-reservation"} and reservation:
        return f"sprintctl reservation show --id {reservation}"
    if kind in {"unblock-dependent-work", "resolve-blocker"} and blocker:
        return f"sprintctl item show --id {blocker}"
    if kind == "start-ready-item" and item:
        return reserve_command(item, served=served)
    if item and kind != "no-action":
        return f"sprintctl item show --id {item}"
    return None


def reservation_age(row: dict[str, Any], now: datetime | None = None) -> int | None:
    if row.get("activity_age_seconds") is not None:
        return int(row["activity_age_seconds"])
    stamp = row.get("last_activity_at")
    if not stamp:
        return None
    from .reservation import parse_time  # noqa: PLC0415

    return max(0, int(((now or datetime.now().astimezone()) - parse_time(stamp)).total_seconds()))


def reserved_by(snapshot: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map item id to its active reservation (the most recently active one)."""
    out: dict[int, dict[str, Any]] = {}
    for row in reversed(snapshot.get("active_reservations", [])):
        out[row["work_item_id"]] = row
    return out


def reserved_tag(row: dict[str, Any] | None) -> str:
    return f"  [reserved: {row['actor']}]" if row else ""


def _ref(item_id: int) -> str:
    return f"#{item_id}"


def build_status(
    snapshot: dict[str, Any],
    context: dict[str, Any],
    *,
    titles: dict[int, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive the ``sprintctl.status/v1`` object from a context snapshot."""
    titles = dict(titles or {})
    for key in ("ready_items", "blocked_items", "active_unreserved_items"):
        for item in snapshot.get(key, []):
            titles.setdefault(item["id"], item["title"])
    reservations = snapshot.get("active_reservations", [])
    held = reserved_by(snapshot)

    in_flight = [
        {
            "reservation_id": row["id"], "item_id": row["work_item_id"],
            "title": row.get("item_title") or titles.get(row["work_item_id"]),
            "actor": row["actor"], "session_id": row.get("session_id"), "role": row.get("role"),
            "age_seconds": reservation_age(row, now), "stale": bool(row.get("stale")),
        }
        for row in reservations
    ]

    needs: list[dict[str, Any]] = []
    for row in (r for r in in_flight if r["stale"]):
        needs.append({
            "kind": "stale-reservation", "item_id": row["item_id"],
            "reservation_id": row["reservation_id"], "age_seconds": row["age_seconds"],
            "summary": f"reservation {row['reservation_id']} on {_ref(row['item_id'])} idle {format_age(row['age_seconds'])} ({row['actor']})",
            "command": f"sprintctl reservation show --id {row['reservation_id']}",
        })
    for item in snapshot.get("active_unreserved_items", []):
        needs.append({
            "kind": "unreserved-active-work", "item_id": item["id"],
            "summary": f"{_ref(item['id'])} active with no reservation: {item['title']}",
            "command": f"sprintctl item show --id {item['id']}",
        })
    for item in snapshot.get("blocked_items", []):
        needs.append({
            "kind": "blocked-work", "item_id": item["id"],
            "summary": f"{_ref(item['id'])} blocked: {item['title']}",
            "command": f"sprintctl item show --id {item['id']}",
        })
    seen_kinds = {"stale-reservation", "unreserved-active-work", "blocked-work", "dependency-blocked"}
    for conflict in snapshot.get("conflicts", []):
        if conflict["kind"] not in seen_kinds:
            needs.append({"kind": conflict["kind"], "summary": conflict["summary"], "command": None})

    ready = snapshot.get("ready_items", [])
    free = [item for item in ready if item["id"] not in held]
    reserved = [{**item, "reserved_by": held[item["id"]]["actor"]} for item in ready if item["id"] in held]

    served = context.get("backend") == "served"
    candidates = [next_action_command(snapshot.get("next_action") or {}, served=served)]
    candidates += [row["command"] for row in needs if row.get("command")][:2]
    if free:
        candidates.append(reserve_command(free[0]["id"], served=served))
    if snapshot["summary"]["total"] == 0:
        candidates.append(f"sprintctl item add --sprint-id {snapshot['sprint']['id']} --track main --title \"...\"")
    nxt = list(dict.fromkeys(c for c in candidates if c))[:3]
    if not nxt:
        nxt = ["sprintctl item list --all"]

    summary = snapshot["summary"]
    return {
        "schema": STATUS_SCHEMA,
        "context": context,
        "sprint": snapshot["sprint"],
        "summary": summary,
        "needs_you": needs,
        "in_flight": in_flight,
        "ready": {"free": free, "reserved": reserved},
        "next": nxt,
    }


def dumps(payload: Any) -> str:
    """JSON for ``--json``: pretty on a TTY, compact otherwise."""
    if sys.stdout.isatty():
        return json.dumps(payload, indent=2)
    return json.dumps(payload, separators=(",", ":"))


def _clip(text: str, width: int = 72) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _section(lines: list[str], head: str, rows: list[str], show_all: bool) -> None:
    lines.append(head)
    shown = rows if show_all else rows[:ROWS_PER_SECTION]
    lines.extend(f"  {row}" for row in shown)
    if len(shown) < len(rows):
        lines.append(f"  … {len(rows) - len(shown)} more (--all)")


def render_status(status: dict[str, Any], *, show_all: bool = False) -> str:
    sprint, summary, context = status["sprint"], status["summary"], status["context"]
    fb = context.get("fallback")
    how = f"fallback: {fb}" if fb else f"repo from {context['repo_source']}"
    lines = [
        f"sprint #{sprint['id']} {sprint['name']} ({sprint['status']}): {summary['total']} items: "
        f"done={summary['done']} active={summary['active']} pending={summary['pending']} "
        f"blocked={summary['blocked']} waiting={summary['waiting_on_dependencies']}",
        f"context: {context['target']} [{context['backend']}] repo={context['repo_id']} ({how})",
        "",
    ]
    needs = status["needs_you"]
    if needs:
        _section(lines, f"▲ needs you ({len(needs)})", [f"▲ {_clip(n['summary'])}" for n in needs], show_all)
    else:
        lines.append("Nothing needs you.")
    flight = status["in_flight"]
    lines.append("")
    if flight:
        rows = [
            f"◐ {_ref(r['item_id'])} {_clip(r['title'] or '', 40)}  {r['actor']}  "
            f"{'idle ' if r['stale'] else ''}{format_age(r['age_seconds'])}"
            for r in flight
        ]
        _section(lines, f"◐ in flight ({len(flight)})", rows, show_all)
    else:
        lines.append("In flight: nothing reserved.")
    ready = status["ready"]
    lines.append("")
    n_free, n_res = len(ready["free"]), len(ready["reserved"])
    rows = [f"○ {_ref(i['id'])} {_clip(i['title'], 60)}" for i in ready["free"]]
    if rows:
        _section(lines, f"○ ready ({n_free} free, {n_res} already in flight)", rows, show_all)
    else:
        lines.append("Ready: nothing is ready to start.")
    lines.append("")
    lines.extend(f"next: {cmd}" for cmd in status["next"])
    return "\n".join(lines)
