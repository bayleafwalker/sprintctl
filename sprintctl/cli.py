import json
import os
import sys
from datetime import datetime, timedelta, timezone

import click

from . import __version__
from . import db as _db
from . import maintain as _maintain
from .render import render_sprint_doc


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)
    db_path = _db.get_db_path()
    conn = _db.get_connection(db_path)
    _db.init_db(conn)
    ctx.obj["conn"] = conn
    ctx.call_on_close(conn.close)


# ---------------------------------------------------------------------------
# sprint
# ---------------------------------------------------------------------------

@cli.group()
def sprint() -> None:
    """Manage sprints."""


@sprint.command("create")
@click.option("--name", required=True, help="Sprint name")
@click.option("--goal", default="", help="Sprint goal")
@click.option("--start", "start_date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--end", "end_date", required=True, help="End date (YYYY-MM-DD)")
@click.option(
    "--status",
    default="planned",
    type=click.Choice(["planned", "active", "closed"]),
    help="Initial status",
)
@click.option(
    "--kind",
    default="active_sprint",
    type=click.Choice(["active_sprint", "backlog", "archive"]),
    help="Sprint kind (default: active_sprint)",
)
@click.pass_obj
def sprint_create(obj, name, goal, start_date, end_date, status, kind) -> None:
    """Create a new sprint."""
    sid = _db.create_sprint(obj["conn"], name, goal, start_date, end_date, status, kind=kind)
    click.echo(f"Created sprint #{sid}: {name}")


@sprint.command("show")
@click.option("--id", "sprint_id", type=int, default=None, help="Sprint ID")
@click.option("--detail", is_flag=True, default=False, help="Include sprint health, track health, and stale item count")
@click.pass_obj
def sprint_show(obj, sprint_id, detail) -> None:
    """Show a sprint (defaults to active sprint)."""
    conn = obj["conn"]
    if sprint_id is not None:
        s = _db.get_sprint(conn, sprint_id)
    else:
        s = _db.get_active_sprint(conn)
    if s is None:
        click.echo("No sprint found. Use --id to specify one.", err=True)
        sys.exit(1)
    click.echo(f"ID:     {s['id']}")
    click.echo(f"Name:   {s['name']}")
    click.echo(f"Goal:   {s['goal']}")
    click.echo(f"Dates:  {s['start_date']} to {s['end_date']}")
    click.echo(f"Status: {s['status']}")
    click.echo(f"Kind:   {s['kind']}")

    if detail:
        from . import calc as _calc
        now = datetime.utcnow()
        items = _db.list_work_items(conn, sprint_id=s["id"])
        tracks = _db.list_tracks(conn, s["id"])
        active_items = [it for it in items if it["status"] == "active"]
        risk = _calc.sprint_overrun_risk(s, len(active_items), now)
        stale_count = sum(
            1 for it in items if _calc.item_staleness(it, now)["is_stale"]
        )
        risk_tag = ""
        if risk["overdue"]:
            risk_tag = " [OVERDUE]"
        elif risk["at_risk"]:
            risk_tag = " [AT RISK]"
        click.echo(f"\nHealth: {risk['days_remaining']} days remaining, {risk['active_items']} active, {stale_count} stale{risk_tag}")
        items_by_track: dict[int, list] = {}
        for it in items:
            items_by_track.setdefault(it["track_id"], []).append(it)
        click.echo("Track health:")
        for t in tracks:
            health = _calc.track_health(items_by_track.get(t["id"], []))
            done_pct = int(health["done_ratio"] * 100)
            blocked_pct = int(health["blocked_ratio"] * 100)
            c = health["counts"]
            click.echo(
                f"  {t['name']}: {health['total']} items — "
                f"{c['done']} done ({done_pct}%), "
                f"{c['active']} active, "
                f"{c['pending']} pending, "
                f"{c['blocked']} blocked ({blocked_pct}%)"
            )


@sprint.command("status")
@click.option("--id", "sprint_id", type=int, required=True, help="Sprint ID")
@click.option(
    "--status",
    "new_status",
    required=True,
    type=click.Choice(["planned", "active", "closed"]),
    help="New status",
)
@click.pass_obj
def sprint_status(obj, sprint_id, new_status) -> None:
    """Update a sprint's status (enforces allowed transitions)."""
    conn = obj["conn"]
    s = _db.get_sprint(conn, sprint_id)
    if s is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    current = s["status"]
    try:
        _db.set_sprint_status(conn, sprint_id, new_status)
    except _db.InvalidTransition as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Sprint #{sprint_id} status: {current} -> {new_status}")


@sprint.command("list")
@click.option("--include-backlog", is_flag=True, default=False, help="Include backlog sprints")
@click.option("--include-archive", is_flag=True, default=False, help="Include archive sprints")
@click.pass_obj
def sprint_list(obj, include_backlog, include_archive) -> None:
    """List sprints (active_sprint kind by default; use flags to include others)."""
    sprints = _db.list_sprints(obj["conn"])
    visible_kinds = {"active_sprint"}
    if include_backlog:
        visible_kinds.add("backlog")
    if include_archive:
        visible_kinds.add("archive")
    sprints = [s for s in sprints if s.get("kind", "active_sprint") in visible_kinds]
    if not sprints:
        click.echo("No sprints found.")
        return
    for s in sprints:
        kind = s.get("kind", "active_sprint")
        click.echo(f"#{s['id']}  [{s['status']:8}]  [{kind:14}]  {s['name']}  ({s['start_date']} to {s['end_date']})")


@sprint.command("kind")
@click.option("--id", "sprint_id", type=int, required=True, help="Sprint ID")
@click.option(
    "--kind",
    required=True,
    type=click.Choice(["active_sprint", "backlog", "archive"]),
    help="New kind",
)
@click.pass_obj
def sprint_kind_cmd(obj, sprint_id, kind) -> None:
    """Set the kind classification of a sprint."""
    conn = obj["conn"]
    try:
        _db.set_sprint_kind(conn, sprint_id, kind)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Sprint #{sprint_id} kind set to: {kind}")


# ---------------------------------------------------------------------------
# item
# ---------------------------------------------------------------------------

@cli.group()
def item() -> None:
    """Manage work items."""


@item.command("add")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--track", "track_name", required=True, help="Track name (created if absent)")
@click.option("--title", required=True, help="Item title")
@click.option("--assignee", default=None, help="Assignee name")
@click.pass_obj
def item_add(obj, sprint_id, track_name, title, assignee) -> None:
    """Add a work item to a sprint track."""
    conn = obj["conn"]
    s = _db.get_sprint(conn, sprint_id)
    if s is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    track_id = _db.get_or_create_track(conn, sprint_id, track_name)
    item_id = _db.create_work_item(conn, sprint_id, track_id, title, assignee=assignee)
    click.echo(f"Added item #{item_id}: {title}  [track: {track_name}]")


@item.command("list")
@click.option("--sprint-id", type=int, default=None, help="Filter by sprint ID")
@click.option("--track", "track_name", default=None, help="Filter by track name")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "active", "done", "blocked"]),
    help="Filter by status",
)
@click.pass_obj
def item_list(obj, sprint_id, track_name, status) -> None:
    """List work items."""
    items = _db.list_work_items(obj["conn"], sprint_id=sprint_id, track_name=track_name, status=status)
    if not items:
        click.echo("No items found.")
        return
    for it in items:
        assignee = it.get("assignee") or "-"
        click.echo(
            f"#{it['id']}  [{it['status']:8}]  {it['title']}  "
            f"(track: {it['track_name']}, assignee: {assignee})"
        )


@item.command("note")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--type", "note_type", required=True, help="Note type (e.g. decision, blocker, update)")
@click.option("--summary", required=True, help="Short summary")
@click.option("--detail", default=None, help="Extended detail")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--actor", default="actor", help="Actor name (default: actor)")
@click.pass_obj
def item_note(obj, item_id, note_type, summary, detail, tags, actor) -> None:
    """Record a structured note event on a work item."""
    conn = obj["conn"]
    it = _db.get_work_item(conn, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    payload: dict = {"summary": summary}
    if detail:
        payload["detail"] = detail
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    eid = _db.create_event(
        conn,
        it["sprint_id"],
        actor=actor,
        event_type=note_type,
        source_type="actor",
        work_item_id=item_id,
        payload=payload,
    )
    click.echo(f"Recorded note #{eid} ({note_type}) on item #{item_id}: {summary}")


@item.command("status")
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
@click.option(
    "--status",
    "new_status",
    required=True,
    type=click.Choice(["pending", "active", "done", "blocked"]),
    help="New status",
)
@click.pass_obj
def item_status(obj, item_id, new_status) -> None:
    """Update an item's status (enforces allowed transitions)."""
    conn = obj["conn"]
    it = _db.get_work_item(conn, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    current = it["status"]
    try:
        _db.set_work_item_status(conn, item_id, new_status)
    except _db.InvalidTransition as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Item #{item_id} status: {current} -> {new_status}")


# ---------------------------------------------------------------------------
# event
# ---------------------------------------------------------------------------

@cli.group()
def event() -> None:
    """Manage events."""


@event.command("add")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--type", "event_type", required=True, help="Event type")
@click.option("--actor", required=True, help="Actor name")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Work item ID")
@click.option(
    "--source",
    "source_type",
    default="actor",
    type=click.Choice(["actor", "daemon", "system"]),
    help="Source type",
)
@click.option("--payload", default=None, help="JSON payload string")
@click.pass_obj
def event_add(obj, sprint_id, event_type, actor, work_item_id, source_type, payload) -> None:
    """Record an event."""
    conn = obj["conn"]
    if _db.get_sprint(conn, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    if work_item_id is not None and _db.get_work_item(conn, work_item_id) is None:
        click.echo(f"Work item #{work_item_id} not found.", err=True)
        sys.exit(1)
    payload_dict: dict | None = None
    if payload:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError as e:
            click.echo(f"Invalid JSON payload: {e}", err=True)
            sys.exit(1)
    eid = _db.create_event(
        conn, sprint_id, actor, event_type,
        source_type=source_type, work_item_id=work_item_id, payload=payload_dict,
    )
    click.echo(f"Recorded event #{eid}: {event_type}  (actor: {actor})")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# maintain
# ---------------------------------------------------------------------------

@cli.group()
def maintain() -> None:
    """Maintenance commands (check, sweep, carryover)."""


def _resolve_sprint(conn, sprint_id: int | None) -> dict:
    if sprint_id is not None:
        s = _db.get_sprint(conn, sprint_id)
        if s is None:
            click.echo(f"Sprint #{sprint_id} not found.", err=True)
            sys.exit(1)
    else:
        s = _db.get_active_sprint(conn)
        if s is None:
            click.echo("No active sprint found. Use --sprint-id to specify one.", err=True)
            sys.exit(1)
    return s


def _parse_threshold(threshold_str: str | None) -> timedelta | None:
    if threshold_str is None:
        return None
    raw = threshold_str.rstrip("h")
    try:
        return timedelta(hours=float(raw))
    except ValueError:
        click.echo(f"Invalid threshold '{threshold_str}' — use format like '4h'.", err=True)
        sys.exit(1)


@maintain.command("check")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--threshold", default=None, help="Staleness threshold, e.g. 4h (default: 4h)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON")
@click.pass_obj
def maintain_check(obj, sprint_id, threshold, as_json) -> None:
    """Dry-run: report stale items and sprint health (no writes)."""
    conn = obj["conn"]
    s = _resolve_sprint(conn, sprint_id)
    now = datetime.utcnow()
    td = _parse_threshold(threshold)
    report = _maintain.check(conn, s["id"], now, threshold=td)

    if as_json:
        out = {
            "sprint": report["sprint"],
            "risk": report["risk"],
            "stale_items": report["stale_items"],
            "track_health": report["track_health"],
            "threshold_hours": report["threshold"].total_seconds() / 3600,
        }
        click.echo(json.dumps(out, indent=2))
        return

    sprint = report["sprint"]
    risk = report["risk"]
    stale = report["stale_items"]
    track_health = report["track_health"]
    threshold_hours = report["threshold"].total_seconds() / 3600

    risk_tag = ""
    if risk["overdue"]:
        risk_tag = "  [OVERDUE]"
    elif risk["at_risk"]:
        risk_tag = "  [AT RISK]"
    click.echo(
        f"Sprint #{sprint['id']}: \"{sprint['name']}\" — "
        f"{risk['days_remaining']} days remaining, "
        f"{risk['active_items']} active item(s){risk_tag}"
    )
    click.echo("")

    click.echo(f"Stale items (threshold: {threshold_hours:g}h):")
    if stale:
        for it in stale:
            h, rem = divmod(it["idle_seconds"], 3600)
            m = rem // 60
            idle = f"{h}h{m:02d}m"
            click.echo(f"  #{it['id']}  [{it['status']:8}]  {it['title']}  — idle {idle}  (track: {it['track_name']})")
    else:
        click.echo("  (none)")
    click.echo("")

    click.echo("Track health:")
    for name, health in track_health.items():
        done_pct = int(health["done_ratio"] * 100)
        blocked_pct = int(health["blocked_ratio"] * 100)
        c = health["counts"]
        click.echo(
            f"  {name}: {health['total']} items — "
            f"{c['done']} done ({done_pct}%), "
            f"{c['active']} active, "
            f"{c['pending']} pending, "
            f"{c['blocked']} blocked ({blocked_pct}%)"
        )


@maintain.command("sweep")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--threshold", default=None, help="Staleness threshold, e.g. 4h (default: 4h)")
@click.option("--auto-close", is_flag=True, default=False,
              help="Auto-close overdue sprint if no active items remain after sweep")
@click.pass_obj
def maintain_sweep(obj, sprint_id, threshold, auto_close) -> None:
    """Execute: block stale items and optionally auto-close overdue sprint."""
    conn = obj["conn"]
    s = _resolve_sprint(conn, sprint_id)
    now = datetime.utcnow()
    td = _parse_threshold(threshold)
    result = _maintain.sweep(conn, s["id"], now, threshold=td, auto_close=auto_close)

    blocked = result["blocked_items"]
    if blocked:
        click.echo(f"Blocked {len(blocked)} stale item(s):")
        for it in blocked:
            click.echo(f"  #{it['id']}  {it['title']}")
    else:
        click.echo("No stale items to block.")

    if result["auto_closed"]:
        click.echo(f"Sprint #{s['id']} auto-closed (overdue, no active items).")


@maintain.command("carryover")
@click.option("--from-sprint", "from_sprint_id", type=int, required=True, help="Source sprint ID")
@click.option("--to-sprint", "to_sprint_id", type=int, required=True, help="Target sprint ID")
@click.pass_obj
def maintain_carryover(obj, from_sprint_id, to_sprint_id) -> None:
    """Carry incomplete items from one sprint to another."""
    conn = obj["conn"]
    if _db.get_sprint(conn, from_sprint_id) is None:
        click.echo(f"Source sprint #{from_sprint_id} not found.", err=True)
        sys.exit(1)
    if _db.get_sprint(conn, to_sprint_id) is None:
        click.echo(f"Target sprint #{to_sprint_id} not found.", err=True)
        sys.exit(1)
    try:
        created = _maintain.carryover(conn, from_sprint_id, to_sprint_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if created:
        click.echo(f"Carried {len(created)} item(s) from sprint #{from_sprint_id} to #{to_sprint_id}:")
        for it in created:
            click.echo(f"  #{it['id']}  {it['title']}")
    else:
        click.echo("No incomplete items to carry over.")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

@cli.command("export")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID to export")
@click.option("--output", "output_path", default=None, help="Output file path (default: sprint-N.json)")
@click.pass_obj
def export_cmd(obj, sprint_id, output_path) -> None:
    """Export a sprint (sprint, tracks, items, events) to a JSON file."""
    conn = obj["conn"]
    sprint = _db.get_sprint(conn, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    tracks = _db.list_tracks(conn, sprint_id)
    items = _db.list_work_items(conn, sprint_id=sprint_id)
    events = _db.list_events(conn, sprint_id)
    envelope = {
        "sprintctl_version": __version__,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": dict(sprint),
        "tracks": [dict(t) for t in tracks],
        "items": [dict(it) for it in items],
        "events": [dict(e) for e in events],
    }
    dest = output_path or f"sprint-{sprint_id}.json"
    with open(dest, "w") as fh:
        json.dump(envelope, fh, indent=2)
    click.echo(f"Exported sprint #{sprint_id} to {dest}")


@cli.command("import")
@click.option("--file", "input_path", required=True, help="Path to exported sprint JSON file")
@click.pass_obj
def import_cmd(obj, input_path) -> None:
    """Import a sprint from a JSON export file (re-sequences all IDs)."""
    conn = obj["conn"]
    try:
        with open(input_path) as fh:
            envelope = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"Failed to read {input_path}: {e}", err=True)
        sys.exit(1)

    src_sprint = envelope["sprint"]
    new_sprint_id = _db.create_sprint(
        conn,
        name=src_sprint["name"],
        goal=src_sprint.get("goal", ""),
        start_date=src_sprint["start_date"],
        end_date=src_sprint["end_date"],
        status=src_sprint.get("status", "planned"),
        kind=src_sprint.get("kind", "active_sprint"),
    )

    # Map old track IDs → new track IDs
    track_id_map: dict[int, int] = {}
    for t in envelope.get("tracks", []):
        new_tid = _db.get_or_create_track(conn, new_sprint_id, t["name"], t.get("description", ""))
        track_id_map[t["id"]] = new_tid

    # Map old item IDs → new item IDs
    item_id_map: dict[int, int] = {}
    for it in envelope.get("items", []):
        old_track_id = it["track_id"]
        new_track_id = track_id_map.get(old_track_id)
        if new_track_id is None:
            # Track may not exist if export is partial; create by looking up in tracks list
            click.echo(f"Warning: track_id {old_track_id} not found for item '{it['title']}'; skipping.", err=True)
            continue
        new_iid = _db.create_work_item(
            conn,
            new_sprint_id,
            new_track_id,
            it["title"],
            description=it.get("description", ""),
            assignee=it.get("assignee"),
        )
        # Restore status via raw update (bypasses transition guard for import)
        imported_status = it.get("status", "pending")
        if imported_status != "pending":
            conn.execute(
                "UPDATE work_item SET status = ?, updated_at = ? WHERE id = ?",
                (imported_status, it.get("updated_at", it.get("created_at")), new_iid),
            )
        item_id_map[it["id"]] = new_iid

    # Re-insert events, preserving source_id in payload
    for ev in envelope.get("events", []):
        old_item_id = ev.get("work_item_id")
        new_item_id = item_id_map.get(old_item_id) if old_item_id is not None else None
        try:
            payload = json.loads(ev.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        payload["source_id"] = ev["id"]
        _db.create_event(
            conn,
            new_sprint_id,
            actor=ev["actor"],
            event_type=ev["event_type"],
            source_type=ev.get("source_type", "system"),
            work_item_id=new_item_id,
            payload=payload,
        )

    conn.commit()
    click.echo(
        f"Imported sprint '{src_sprint['name']}' as #{new_sprint_id} "
        f"({len(item_id_map)} items, {len(envelope.get('events', []))} events)"
    )


@cli.command("render")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.pass_obj
def render_cmd(obj, sprint_id) -> None:
    """Render a plain-text sprint document."""
    conn = obj["conn"]
    if sprint_id is not None:
        s = _db.get_sprint(conn, sprint_id)
    else:
        s = _db.get_active_sprint(conn)
    if s is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    tracks = _db.list_tracks(conn, s["id"])
    all_items = _db.list_work_items(conn, sprint_id=s["id"])
    items_by_track: dict[int, list[dict]] = {}
    for it in all_items:
        items_by_track.setdefault(it["track_id"], []).append(it)
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = render_sprint_doc(s, tracks, items_by_track, rendered_at)
    click.echo(doc)
