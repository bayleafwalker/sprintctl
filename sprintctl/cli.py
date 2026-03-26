import json
import sys
from datetime import datetime, timezone

import click

from . import db as _db
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
@click.pass_obj
def sprint_create(obj, name, goal, start_date, end_date, status) -> None:
    """Create a new sprint."""
    sid = _db.create_sprint(obj["conn"], name, goal, start_date, end_date, status)
    click.echo(f"Created sprint #{sid}: {name}")


@sprint.command("show")
@click.option("--id", "sprint_id", type=int, default=None, help="Sprint ID")
@click.pass_obj
def sprint_show(obj, sprint_id) -> None:
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


@sprint.command("list")
@click.pass_obj
def sprint_list(obj) -> None:
    """List all sprints."""
    sprints = _db.list_sprints(obj["conn"])
    if not sprints:
        click.echo("No sprints found.")
        return
    for s in sprints:
        click.echo(f"#{s['id']}  [{s['status']:8}]  {s['name']}  ({s['start_date']} to {s['end_date']})")


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
