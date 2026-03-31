import json
import os
import sqlite3
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone

import click

from . import __version__
from . import db as _db
from . import maintain as _maintain
from .render import render_sprint_doc


def _detect_runtime_session_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return (
        os.environ.get("SPRINTCTL_RUNTIME_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )


def _detect_instance_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("SPRINTCTL_INSTANCE_ID") or str(uuid.uuid4())


def _detect_hostname(explicit: str | None) -> str:
    if explicit:
        return explicit
    return socket.gethostname()


def _detect_pid(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    return os.getpid()


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)
    ctx.obj.setdefault("conn", None)


def _get_conn(obj: dict) -> sqlite3.Connection:
    conn = obj.get("conn")
    if conn is None:
        db_path = _db.get_db_path()
        conn = _db.get_connection(db_path)
        _db.init_db(conn)
        obj["conn"] = conn
        click.get_current_context().call_on_close(conn.close)
    return conn


# ---------------------------------------------------------------------------
# sprint
# ---------------------------------------------------------------------------

@cli.group()
def sprint() -> None:
    """Manage sprints."""


@sprint.command("create")
@click.option("--name", required=True, help="Sprint name")
@click.option("--goal", default="", help="Sprint goal")
@click.option("--start", "start_date", default=None, help="Start date (YYYY-MM-DD, optional)")
@click.option("--end", "end_date", default=None, help="End date (YYYY-MM-DD, optional)")
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
    sid = _db.create_sprint(_get_conn(obj), name, goal, start_date, end_date, status, kind=kind)
    click.echo(f"Created sprint #{sid}: {name}")


@sprint.command("show")
@click.option("--id", "sprint_id", type=int, default=None, help="Sprint ID")
@click.option("--detail", is_flag=True, default=False, help="Include sprint health, track health, and stale item count")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_show(obj, sprint_id, detail, as_json) -> None:
    """Show a sprint (defaults to active sprint)."""
    conn = _get_conn(obj)
    if sprint_id is not None:
        s = _db.get_sprint(conn, sprint_id)
    else:
        s = _db.get_active_sprint(conn)
    if s is None:
        click.echo("No sprint found. Use --id to specify one.", err=True)
        sys.exit(1)

    if as_json:
        out: dict = {
            "id": s["id"],
            "name": s["name"],
            "goal": s["goal"],
            "start_date": s["start_date"],
            "end_date": s["end_date"],
            "status": s["status"],
            "kind": s["kind"],
        }
        if detail:
            from . import calc as _calc
            now = datetime.now(timezone.utc)
            items = _db.list_work_items(conn, sprint_id=s["id"])
            tracks = _db.list_tracks(conn, s["id"])
            active_items = [it for it in items if it["status"] == "active"]
            risk = _calc.sprint_overrun_risk(s, len(active_items), now)
            pending_threshold = _maintain._pending_stale_threshold()
            stale_count = sum(
                1 for it in items if _calc.item_staleness(it, now, pending_threshold=pending_threshold)["is_stale"]
            )
            items_by_track: dict[int, list] = {}
            for it in items:
                items_by_track.setdefault(it["track_id"], []).append(it)
            track_health_out = {}
            for t in tracks:
                track_health_out[t["name"]] = _calc.track_health(items_by_track.get(t["id"], []))
            out["detail"] = {
                "risk": risk,
                "stale_count": stale_count,
                "track_health": track_health_out,
            }
        click.echo(json.dumps(out, indent=2))
        return

    click.echo(f"ID:     {s['id']}")
    click.echo(f"Name:   {s['name']}")
    click.echo(f"Goal:   {s['goal']}")
    if s.get("start_date") and s.get("end_date"):
        click.echo(f"Dates:  {s['start_date']} to {s['end_date']}")
    click.echo(f"Status: {s['status']}")
    click.echo(f"Kind:   {s['kind']}")

    if detail:
        from . import calc as _calc
        now = datetime.now(timezone.utc)
        items = _db.list_work_items(conn, sprint_id=s["id"])
        tracks = _db.list_tracks(conn, s["id"])
        active_items = [it for it in items if it["status"] == "active"]
        risk = _calc.sprint_overrun_risk(s, len(active_items), now)
        pending_threshold = _maintain._pending_stale_threshold()
        stale_count = sum(
            1 for it in items if _calc.item_staleness(it, now, pending_threshold=pending_threshold)["is_stale"]
        )
        risk_tag = ""
        if risk["overdue"]:
            risk_tag = " [OVERDUE]"
        elif risk["at_risk"]:
            risk_tag = " [AT RISK]"
        if risk.get("date_bound", True):
            click.echo(f"\nHealth: {risk['days_remaining']} days remaining, {risk['active_items']} active, {stale_count} stale{risk_tag}")
        else:
            click.echo(f"\nHealth: {risk['active_items']} active, {stale_count} stale")
        items_by_track2: dict[int, list] = {}
        for it in items:
            items_by_track2.setdefault(it["track_id"], []).append(it)
        click.echo("Track health:")
        for t in tracks:
            health = _calc.track_health(items_by_track2.get(t["id"], []))
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
    conn = _get_conn(obj)
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
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_list(obj, include_backlog, include_archive, as_json) -> None:
    """List sprints (active_sprint kind by default; use flags to include others)."""
    sprints = _db.list_sprints(_get_conn(obj))
    visible_kinds = {"active_sprint"}
    if include_backlog:
        visible_kinds.add("backlog")
    if include_archive:
        visible_kinds.add("archive")
    sprints = [s for s in sprints if s.get("kind", "active_sprint") in visible_kinds]
    if as_json:
        click.echo(json.dumps(sprints, indent=2))
        return
    if not sprints:
        click.echo("No sprints found.")
        return
    for s in sprints:
        kind = s.get("kind", "active_sprint")
        date_part = f"  ({s['start_date']} to {s['end_date']})" if s.get("start_date") and s.get("end_date") else ""
        click.echo(f"#{s['id']}  [{s['status']:8}]  [{kind:14}]  {s['name']}{date_part}")


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
    conn = _get_conn(obj)
    try:
        _db.set_sprint_kind(conn, sprint_id, kind)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Sprint #{sprint_id} kind set to: {kind}")


@sprint.command("backlog-seed")
@click.option("--from-sprint-id", "source_sprint_id", type=int, required=True,
              help="Sprint ID to read knowledge candidates from")
@click.option("--to-sprint-id", "target_sprint_id", type=int, required=True,
              help="Sprint ID (backlog) to seed items into")
@click.option("--actor", default="system", help="Actor name (default: system)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output seeded items as JSON")
@click.pass_obj
def sprint_backlog_seed(obj, source_sprint_id, target_sprint_id, actor, as_json) -> None:
    """Seed backlog items from knowledge candidate events in another sprint."""
    conn = _get_conn(obj)
    try:
        seeded = _db.backlog_seed_from_candidates(conn, source_sprint_id, target_sprint_id, actor=actor)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(seeded, indent=2))
        return
    if not seeded:
        click.echo(f"No new items seeded (0 candidates or all already seeded).")
        return
    click.echo(f"Seeded {len(seeded)} item(s) into sprint #{target_sprint_id}:")
    for it in seeded:
        click.echo(f"  #{it['id']}  {it['title']}")


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
    conn = _get_conn(obj)
    s = _db.get_sprint(conn, sprint_id)
    if s is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    track_id = _db.get_or_create_track(conn, sprint_id, track_name)
    item_id = _db.create_work_item(conn, sprint_id, track_id, title, assignee=assignee)
    click.echo(f"Added item #{item_id}: {title}  [track: {track_name}]")


@item.command("show")
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_show(obj, item_id, as_json) -> None:
    """Show a single work item with its recent events and active claims."""
    conn = _get_conn(obj)
    it = _db.get_work_item(conn, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    events = _db.list_events(conn, it["sprint_id"])
    item_events = [e for e in events if e.get("work_item_id") == item_id]
    claims = _db.list_claims(conn, item_id, active_only=True)
    refs = _db.list_refs(conn, item_id)
    blocking = _db.list_deps_blocking(conn, item_id)
    blocked_by_me = _db.list_deps_blocked_by(conn, item_id)

    if as_json:
        click.echo(json.dumps({
            "item": dict(it),
            "events": item_events,
            "active_claims": claims,
            "refs": refs,
            "deps": {"blocked_by": blocking, "blocks": blocked_by_me},
        }, indent=2))
        return

    click.echo(f"#{it['id']}  [{it['status']}]  {it['title']}")
    click.echo(f"  Sprint:   #{it['sprint_id']}")
    track_name = it.get("track_name", "")
    if track_name:
        click.echo(f"  Track:    {track_name}")
    assignee = it.get("assignee") or "-"
    click.echo(f"  Assignee: {assignee}")
    click.echo(f"  Updated:  {it['updated_at']}")

    if refs:
        click.echo("\nRefs:")
        for r in refs:
            label = f"  {r['label']}" if r["label"] else ""
            click.echo(f"  #{r['id']}  [{r['ref_type']}]  {r['url']}{label}")

    if blocking:
        click.echo("\nBlocked by:")
        for d in blocking:
            click.echo(f"  #{d['item_id']}  [{d['blocker_status']}]  {d['blocker_title']}")
    if blocked_by_me:
        click.echo("\nBlocks:")
        for d in blocked_by_me:
            click.echo(f"  #{d['blocked_item_id']}  [{d['waiting_status']}]  {d['waiting_title']}")

    if claims:
        click.echo("\nActive claims:")
        for c in claims:
            excl = "exclusive" if c["exclusive"] else "shared"
            parts = [
                f"  #{c['claim_id']}  {c['actor']}  [{c['claim_type']}]  {excl}  "
                f"proof={c['identity_status']}  expires={c['expires_at']}"
            ]
            if c.get("runtime_session_id"):
                parts.append(f"  runtime={c['runtime_session_id']}")
            if c.get("instance_id"):
                parts.append(f"  instance={c['instance_id']}")
            if c.get("branch"):
                parts.append(f"  branch={c['branch']}")
            if c.get("commit_sha"):
                parts.append(f"  commit={c['commit_sha']}")
            if c.get("pr_ref"):
                parts.append(f"  pr={c['pr_ref']}")
            if c.get("worktree_path"):
                parts.append(f"  worktree={c['worktree_path']}")
            if c.get("hostname"):
                parts.append(f"  host={c['hostname']}")
            if c.get("pid") is not None:
                parts.append(f"  pid={c['pid']}")
            click.echo("".join(parts))

    if item_events:
        click.echo("\nEvents:")
        for e in item_events[-10:]:
            click.echo(f"  #{e['id']}  [{e['event_type']}]  {e['actor']}  {e['created_at']}")
    else:
        click.echo("\nEvents: (none)")


@item.command("list")
@click.option("--sprint-id", type=int, default=None, help="Filter by sprint ID")
@click.option("--track", "track_name", default=None, help="Filter by track name")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "active", "done", "blocked"]),
    help="Filter by status",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_list(obj, sprint_id, track_name, status, as_json) -> None:
    """List work items."""
    items = _db.list_work_items(_get_conn(obj), sprint_id=sprint_id, track_name=track_name, status=status)
    if as_json:
        click.echo(json.dumps(items, indent=2))
        return
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
@click.option("--evidence-item-id", type=int, default=None, help="Work item ID this knowledge came from")
@click.option("--evidence-event-id", type=int, default=None, help="Event ID this knowledge came from")
@click.option("--git-branch", default=None, help="Git branch name at time of note")
@click.option("--git-sha", default=None, help="Git commit SHA at time of note")
@click.option("--git-worktree", default=None, help="Git worktree path at time of note")
@click.pass_obj
def item_note(
    obj, item_id, note_type, summary, detail, tags, actor,
    evidence_item_id, evidence_event_id,
    git_branch, git_sha, git_worktree,
) -> None:
    """Record a structured note event on a work item."""
    conn = _get_conn(obj)
    it = _db.get_work_item(conn, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    payload: dict = {"summary": summary}
    if detail:
        payload["detail"] = detail
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if evidence_item_id is not None:
        payload["evidence_item_id"] = evidence_item_id
    if evidence_event_id is not None:
        payload["evidence_event_id"] = evidence_event_id
    if git_branch is not None:
        payload["git_branch"] = git_branch
    if git_sha is not None:
        payload["git_sha"] = git_sha
    if git_worktree is not None:
        payload["git_worktree"] = git_worktree
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
@click.option("--actor", default=None, help="Actor name")
@click.option("--claim-id", type=int, default=None, help="Claim ID to prove ownership of an active exclusive claim")
@click.option("--claim-token", default=None, help="Claim token proving ownership of an active exclusive claim")
@click.pass_obj
def item_status(obj, item_id, new_status, actor, claim_id, claim_token) -> None:
    """Update an item's status (enforces allowed transitions and exclusive claims)."""
    conn = _get_conn(obj)
    it = _db.get_work_item(conn, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    current = it["status"]
    try:
        _db.set_work_item_status(
            conn,
            item_id,
            new_status,
            actor=actor,
            claim_id=claim_id,
            claim_token=claim_token,
        )
    except (_db.InvalidTransition, _db.ClaimConflict) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Item #{item_id} status: {current} -> {new_status}")


# ---------------------------------------------------------------------------
# item ref
# ---------------------------------------------------------------------------

@item.group("ref")
def item_ref() -> None:
    """Manage external references on a work item."""


@item_ref.command("add")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option(
    "--type", "ref_type",
    required=True,
    type=click.Choice(["pr", "issue", "doc", "other"]),
    help="Reference type",
)
@click.option("--url", required=True, help="URL of the external reference")
@click.option("--label", default="", help="Short human-readable label")
@click.pass_obj
def item_ref_add(obj, item_id, ref_type, url, label) -> None:
    """Attach an external reference to a work item."""
    conn = _get_conn(obj)
    try:
        ref_id = _db.add_ref(conn, item_id, ref_type, url, label)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Ref #{ref_id} added to item #{item_id}: [{ref_type}] {url}")


@item_ref.command("list")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_ref_list(obj, item_id, as_json) -> None:
    """List external references on a work item."""
    conn = _get_conn(obj)
    if _db.get_work_item(conn, item_id) is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    refs = _db.list_refs(conn, item_id)
    if as_json:
        click.echo(json.dumps(refs, indent=2))
        return
    if not refs:
        click.echo(f"No refs on item #{item_id}.")
        return
    for r in refs:
        label = f"  {r['label']}" if r["label"] else ""
        click.echo(f"  #{r['id']}  [{r['ref_type']}]  {r['url']}{label}")


@item_ref.command("remove")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--ref-id", type=int, required=True, help="Ref ID to remove")
@click.pass_obj
def item_ref_remove(obj, item_id, ref_id) -> None:
    """Remove an external reference from a work item."""
    conn = _get_conn(obj)
    try:
        _db.remove_ref(conn, ref_id, item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Ref #{ref_id} removed from item #{item_id}.")


# ---------------------------------------------------------------------------
# item dep
# ---------------------------------------------------------------------------

@item.group("dep")
def item_dep() -> None:
    """Manage dependencies between work items."""


@item_dep.command("add")
@click.option("--id", "item_id", type=int, required=True, help="Blocker item ID (must complete first)")
@click.option("--blocks-item-id", type=int, required=True, help="ID of the item being blocked")
@click.pass_obj
def item_dep_add(obj, item_id, blocks_item_id) -> None:
    """Record that item --id must complete before --blocks-item-id can start."""
    conn = _get_conn(obj)
    try:
        dep_id = _db.add_dep(conn, item_id, blocks_item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Dep #{dep_id}: item #{item_id} blocks item #{blocks_item_id}")


@item_dep.command("list")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_dep_list(obj, item_id, as_json) -> None:
    """List dependencies for a work item (what blocks it and what it blocks)."""
    conn = _get_conn(obj)
    if _db.get_work_item(conn, item_id) is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    blocking = _db.list_deps_blocking(conn, item_id)
    blocked_by_me = _db.list_deps_blocked_by(conn, item_id)
    if as_json:
        click.echo(json.dumps({"blocked_by": blocking, "blocks": blocked_by_me}, indent=2))
        return
    if not blocking and not blocked_by_me:
        click.echo(f"No dependencies on item #{item_id}.")
        return
    if blocking:
        click.echo(f"Item #{item_id} is blocked by:")
        for d in blocking:
            click.echo(f"  #{d['item_id']}  [{d['blocker_status']}]  {d['blocker_title']}  (dep #{d['id']})")
    if blocked_by_me:
        click.echo(f"Item #{item_id} blocks:")
        for d in blocked_by_me:
            click.echo(f"  #{d['blocked_item_id']}  [{d['waiting_status']}]  {d['waiting_title']}  (dep #{d['id']})")


@item_dep.command("remove")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID (either side of the dep)")
@click.option("--dep-id", type=int, required=True, help="Dep ID to remove")
@click.pass_obj
def item_dep_remove(obj, item_id, dep_id) -> None:
    """Remove a dependency."""
    conn = _get_conn(obj)
    try:
        _db.remove_dep(conn, dep_id, item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Dep #{dep_id} removed.")


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
    conn = _get_conn(obj)
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


@event.command("list")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Filter by work item ID")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--knowledge", "knowledge_only", is_flag=True, default=False,
              help="Show only knowledge candidate events (pattern-noted, lesson-learned, risk-accepted)")
@click.option("--limit", default=None, type=int, help="Maximum number of events to return (most recent)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def event_list(obj, sprint_id, work_item_id, event_type, knowledge_only, limit, as_json) -> None:
    """List events for a sprint."""
    if knowledge_only and event_type is not None:
        click.echo("Error: --knowledge and --type are mutually exclusive.", err=True)
        sys.exit(1)
    conn = _get_conn(obj)
    if _db.get_sprint(conn, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    if knowledge_only:
        events = _db.list_knowledge_candidates(conn, sprint_id)
        # list_knowledge_candidates already deserializes payload; re-serialize for JSON output
        if work_item_id is not None:
            events = [e for e in events if e.get("work_item_id") == work_item_id]
        if limit is not None:
            events = events[-limit:]
        if as_json:
            click.echo(json.dumps(events, indent=2))
            return
        if not events:
            click.echo("No events found.")
            return
        for e in events:
            item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
            click.echo(
                f"#{e['id']}  [{e['event_type']}]  {e['actor']}  "
                f"{e['created_at']}{item_label}"
            )
        return
    events = _db.list_events(conn, sprint_id)
    if work_item_id is not None:
        events = [e for e in events if e.get("work_item_id") == work_item_id]
    if event_type is not None:
        events = [e for e in events if e.get("event_type") == event_type]
    if limit is not None:
        events = events[-limit:]
    if as_json:
        click.echo(json.dumps(events, indent=2))
        return
    if not events:
        click.echo("No events found.")
        return
    for e in events:
        item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
        click.echo(
            f"#{e['id']}  [{e['event_type']}]  {e['actor']}  "
            f"{e['created_at']}{item_label}"
        )


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
    conn = _get_conn(obj)
    s = _resolve_sprint(conn, sprint_id)
    now = datetime.now(timezone.utc)
    td = _parse_threshold(threshold)
    report = _maintain.check(conn, s["id"], now, threshold=td)

    if as_json:
        pt = report["pending_threshold"]
        out = {
            "sprint": report["sprint"],
            "risk": report["risk"],
            "stale_items": report["stale_items"],
            "track_health": report["track_health"],
            "threshold_hours": report["threshold"].total_seconds() / 3600,
            "pending_threshold_hours": pt.total_seconds() / 3600 if pt else None,
        }
        click.echo(json.dumps(out, indent=2))
        return

    sprint = report["sprint"]
    risk = report["risk"]
    stale = report["stale_items"]
    track_health = report["track_health"]
    threshold_hours = report["threshold"].total_seconds() / 3600
    pending_threshold = report["pending_threshold"]

    risk_tag = ""
    if risk["overdue"]:
        risk_tag = "  [OVERDUE]"
    elif risk["at_risk"]:
        risk_tag = "  [AT RISK]"
    if risk.get("date_bound", True):
        date_info = f"{risk['days_remaining']} days remaining, "
    else:
        date_info = ""
    click.echo(
        f"Sprint #{sprint['id']}: \"{sprint['name']}\" — "
        f"{date_info}{risk['active_items']} active item(s){risk_tag}"
    )
    click.echo("")

    pending_label = f", pending: {pending_threshold.total_seconds() / 3600:g}h" if pending_threshold else ", pending: off"
    click.echo(f"Stale items (active threshold: {threshold_hours:g}h{pending_label}):")
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
    conn = _get_conn(obj)
    s = _resolve_sprint(conn, sprint_id)
    now = datetime.now(timezone.utc)
    td = _parse_threshold(threshold)
    result = _maintain.sweep(conn, s["id"], now, threshold=td, auto_close=auto_close)

    blocked = result["blocked_items"]
    if blocked:
        click.echo(f"Blocked {len(blocked)} stale item(s):")
        for it in blocked:
            click.echo(f"  #{it['id']}  {it['title']}")
    else:
        click.echo("No stale items to block.")

    purged = result["expired_claims_purged"]
    if purged:
        click.echo(f"Purged {purged} expired claim(s).")

    if result["auto_closed"]:
        click.echo(f"Sprint #{s['id']} auto-closed (overdue, no active items).")


@maintain.command("carryover")
@click.option("--from-sprint", "from_sprint_id", type=int, required=True, help="Source sprint ID")
@click.option("--to-sprint", "to_sprint_id", type=int, required=True, help="Target sprint ID")
@click.pass_obj
def maintain_carryover(obj, from_sprint_id, to_sprint_id) -> None:
    """Carry incomplete items from one sprint to another."""
    conn = _get_conn(obj)
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
    conn = _get_conn(obj)
    sprint = _db.get_sprint(conn, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    tracks = _db.list_tracks(conn, sprint_id)
    items = _db.list_work_items(conn, sprint_id=sprint_id)
    events = _db.list_events(conn, sprint_id)
    refs_by_item: dict[int, list[dict]] = {}
    for it in items:
        item_refs = _db.list_refs(conn, it["id"])
        if item_refs:
            refs_by_item[it["id"]] = item_refs
    envelope = {
        "sprintctl_version": __version__,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": dict(sprint),
        "tracks": [dict(t) for t in tracks],
        "items": [dict(it) for it in items],
        "events": [dict(e) for e in events],
        "refs": refs_by_item,
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
    conn = _get_conn(obj)
    try:
        with open(input_path) as fh:
            envelope = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"Failed to read {input_path}: {e}", err=True)
        sys.exit(1)

    src_sprint = envelope["sprint"]

    # Pre-flight: all track_ids referenced by items must be present in tracks list.
    # Validate before writing anything so the DB is never left in a partial state.
    exported_track_ids = {t["id"] for t in envelope.get("tracks", [])}
    missing: list[str] = []
    for it in envelope.get("items", []):
        if it["track_id"] not in exported_track_ids:
            missing.append(f"  item '{it['title']}' references track_id {it['track_id']} not found in export")
    if missing:
        click.echo("Import aborted — items reference tracks missing from the export file:", err=True)
        for m in missing:
            click.echo(m, err=True)
        sys.exit(1)

    new_sprint_id = _db.create_sprint(
        conn,
        name=src_sprint["name"],
        goal=src_sprint.get("goal", ""),
        start_date=src_sprint.get("start_date"),
        end_date=src_sprint.get("end_date"),
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
        new_track_id = track_id_map[it["track_id"]]  # guaranteed present after pre-flight
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

    # Re-insert refs, remapping old item IDs to new item IDs
    refs_by_item = envelope.get("refs", {})
    for old_item_id_str, item_refs in refs_by_item.items():
        new_item_id = item_id_map.get(int(old_item_id_str))
        if new_item_id is None:
            continue
        for r in item_refs:
            _db.add_ref(conn, new_item_id, r["ref_type"], r["url"], r.get("label", ""))

    conn.commit()
    click.echo(
        f"Imported sprint '{src_sprint['name']}' as #{new_sprint_id} "
        f"({len(item_id_map)} items, {len(envelope.get('events', []))} events)"
    )


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

@cli.group()
def claim() -> None:
    """Manage agent claims on work items."""


@claim.command("create")
@click.option("--item-id", type=int, required=True, help="Work item ID to claim")
@click.option("--actor", "--agent", "actor", required=True, help="Actor identifier")
@click.option(
    "--type", "claim_type",
    default="execute",
    type=click.Choice(["inspect", "execute", "review", "coordinate"]),
    help="Claim type (default: execute)",
)
@click.option("--non-exclusive", is_flag=True, default=False, help="Allow concurrent claims (non-exclusive)")
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="TTL in seconds (default: 300)")
@click.option("--branch", default=None, help="Git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="PR reference (e.g. owner/repo#123)")
@click.option("--runtime-session-id", default=None, help="Runtime session identifier when available")
@click.option("--instance-id", default=None, help="Stable client-process-local instance ID")
@click.option("--hostname", default=None, help="Hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="PID override (defaults to current process)")
@click.option("--coordinate-claim-id", type=int, default=None, help="Coordinator's claim ID (sub-agent use: bypass coordinate claim lock)")
@click.option("--coordinate-claim-token", default=None, help="Coordinator's claim token (required with --coordinate-claim-id)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the created claim as JSON")
@click.pass_obj
def claim_create(
    obj,
    item_id,
    actor,
    claim_type,
    non_exclusive,
    ttl_seconds,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    runtime_session_id,
    instance_id,
    hostname,
    pid,
    coordinate_claim_id,
    coordinate_claim_token,
    as_json,
) -> None:
    """Claim a work item for an actor.

    Sub-agents spawned by a coordinator should pass --coordinate-claim-id and
    --coordinate-claim-token to create an execute/inspect/review claim under
    an active coordinate claim without triggering a conflict error.
    """
    conn = _get_conn(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        cid = _db.create_claim(
            conn,
            work_item_id=item_id,
            agent=actor,
            claim_type=claim_type,
            exclusive=not non_exclusive,
            ttl_seconds=ttl_seconds,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            coordinate_claim_id=coordinate_claim_id,
            coordinate_claim_token=coordinate_claim_token,
        )
    except (_db.ClaimConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    claim = _db.get_claim(conn, cid, include_secret=True)
    assert claim is not None
    if as_json:
        click.echo(json.dumps(claim, indent=2))
        return
    click.echo(f"Claim #{cid} created: {actor} → item #{item_id} ({claim_type}, ttl={ttl_seconds}s)")
    click.echo(f"Claim token: {claim['claim_token']}")


@claim.command("heartbeat")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token returned when the claim was created")
@click.option("--actor", "--agent", "actor", default=None, help="Actor identifier (advisory metadata only)")
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="Refresh TTL in seconds (default: 300)")
@click.option(
    "--warn-before-expiry", "warn_before_expiry", type=int, default=60,
    help="Emit a warning if the refreshed claim expires within N seconds (default: 60). Set 0 to disable.",
)
@click.option("--runtime-session-id", default=None, help="Runtime session identifier when available")
@click.option("--instance-id", default=None, help="Stable client-process-local instance ID")
@click.option("--branch", default=None, help="Git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="PR reference (e.g. owner/repo#123)")
@click.option("--hostname", default=None, help="Hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="PID override (defaults to current process)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output refreshed claim state as JSON")
@click.pass_obj
def claim_heartbeat(
    obj,
    claim_id,
    claim_token,
    actor,
    ttl_seconds,
    warn_before_expiry,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    as_json,
) -> None:
    """Refresh the TTL on an existing claim."""
    conn = _get_conn(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        _db.heartbeat_claim(
            conn,
            claim_id,
            claim_token,
            ttl_seconds=ttl_seconds,
            actor=actor,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    refreshed = _db.get_claim(conn, claim_id)
    assert refreshed is not None
    if as_json:
        refreshed["heartbeat_ttl_seconds"] = ttl_seconds
        click.echo(json.dumps(refreshed, indent=2))
        return
    click.echo(f"Claim #{claim_id} heartbeat refreshed (ttl={ttl_seconds}s, expires={refreshed['expires_at']})")
    if warn_before_expiry > 0 and ttl_seconds <= warn_before_expiry:
        click.echo(
            f"Warning: claim #{claim_id} expires in {ttl_seconds}s which is within "
            f"the --warn-before-expiry window ({warn_before_expiry}s). "
            "Consider increasing --ttl or heartbeating more frequently.",
            err=True,
        )


@claim.command("release")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token returned when the claim was created")
@click.option("--actor", "--agent", "actor", default=None, help="Actor identifier (advisory metadata only)")
@click.pass_obj
def claim_release(obj, claim_id, claim_token, actor) -> None:
    """Release (delete) a claim."""
    conn = _get_conn(obj)
    try:
        _db.release_claim(conn, claim_id, claim_token, actor=actor)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Claim #{claim_id} released.")


@claim.command("handoff")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", default=None, help="Existing claim token (required unless adopting a legacy ambiguous claim)")
@click.option("--actor", "--agent", "actor", required=True, help="Recipient actor identifier")
@click.option(
    "--mode",
    default="rotate",
    type=click.Choice(["transfer", "rotate"]),
    help="Transfer keeps the token; rotate mints a new one (default: rotate)",
)
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="Refresh TTL in seconds after handoff (default: 300)")
@click.option("--runtime-session-id", default=None, help="Recipient runtime session identifier")
@click.option("--instance-id", default=None, help="Recipient client-process-local instance ID")
@click.option("--branch", default=None, help="Recipient git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Recipient worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Recipient commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="Recipient PR reference (e.g. owner/repo#123)")
@click.option("--hostname", default=None, help="Recipient hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="Recipient PID override (defaults to current process)")
@click.option("--performed-by", default=None, help="Actor performing the handoff")
@click.option("--note", default=None, help="Structured note to include in the handoff event")
@click.option("--allow-legacy-adopt", is_flag=True, default=False, help="Adopt a legacy ambiguous claim with no token by minting a fresh proof")
@click.option("--output", "output_path", default=None, help="Write the claim handoff bundle to a file instead of stdout")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the claim handoff bundle as JSON")
@click.pass_obj
def claim_handoff(
    obj,
    claim_id,
    claim_token,
    actor,
    mode,
    ttl_seconds,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    performed_by,
    note,
    allow_legacy_adopt,
    output_path,
    as_json,
) -> None:
    """Explicitly transfer or rotate claim ownership and emit a claim handoff bundle."""
    conn = _get_conn(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        claim = _db.handoff_claim(
            conn,
            claim_id,
            claim_token,
            actor=actor,
            mode=mode,
            ttl_seconds=ttl_seconds,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
            performed_by=performed_by,
            note=note,
            allow_legacy_adopt=allow_legacy_adopt,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    item = _db.get_work_item(conn, claim["work_item_id"])
    sprint = _db.get_sprint(conn, item["sprint_id"]) if item else None
    bundle = {
        "bundle_type": "claim_handoff",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "claim": claim,
        "item": item,
        "sprint": sprint,
        "performed_by": performed_by or actor,
    }

    if output_path and output_path != "-":
        with open(output_path, "w") as fh:
            json.dump(bundle, fh, indent=2)
        click.echo(f"Claim handoff bundle written to {output_path}")
        if not as_json:
            click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
            click.echo(f"Claim token: {claim['claim_token']}")
        return

    if as_json or output_path == "-":
        click.echo(json.dumps(bundle, indent=2))
        return

    click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
    click.echo(f"Claim token: {claim['claim_token']}")


@claim.command("list")
@click.option("--item-id", type=int, required=True, help="Work item ID")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list(obj, item_id, show_all, as_json) -> None:
    """List claims on a work item."""
    conn = _get_conn(obj)
    claims = _db.list_claims(conn, item_id, active_only=not show_all)
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        click.echo(f"No {'active ' if not show_all else ''}claims on item #{item_id}.")
        return
    for c in claims:
        excl = "exclusive" if c["exclusive"] else "shared"
        proof = c["identity_status"]
        click.echo(
            f"#{c['claim_id']}  {c['actor']}  [{c['claim_type']}]  {excl}  "
            f"proof={proof}  expires={c['expires_at']}  heartbeat={c['heartbeat']}"
        )


@claim.command("list-sprint")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option(
    "--expiring-within", "expiring_within", type=int, default=None,
    help="Only show claims expiring within N seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list_sprint(obj, sprint_id, show_all, expiring_within, as_json) -> None:
    """List all claims across a sprint, optionally filtered by expiry window."""
    conn = _get_conn(obj)
    if sprint_id is not None:
        sprint = _db.get_sprint(conn, sprint_id)
    else:
        sprint = _db.get_active_sprint(conn)
    if sprint is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    claims = _db.list_claims_by_sprint(
        conn,
        sprint["id"],
        active_only=not show_all,
        expiring_within_seconds=expiring_within,
    )
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        label = "expiring" if expiring_within is not None else ("active " if not show_all else "")
        click.echo(f"No {label}claims in sprint #{sprint['id']} ({sprint['name']}).")
        return
    click.echo(f"Claims in sprint #{sprint['id']} ({sprint['name']}):")
    for c in claims:
        excl = "exclusive" if c["exclusive"] else "shared"
        click.echo(
            f"  #{c['claim_id']}  item #{c['work_item_id']} ({c['item_title']})  "
            f"{c['actor']}  [{c['claim_type']}]  {excl}  "
            f"proof={c['identity_status']}  expires={c['expires_at']}"
        )


@claim.command("show")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token (proves ownership; re-displays the token)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_show(obj, claim_id, claim_token, as_json) -> None:
    """Show a claim and re-display its token (useful after context loss).

    Requires the current claim_token to prove ownership before revealing it again.
    """
    conn = _get_conn(obj)
    row = conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        click.echo(f"Error: Claim #{claim_id} not found", err=True)
        sys.exit(1)
    try:
        from .db import _require_claim_proof
        _require_claim_proof(row, claim_token)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    claim = _db.get_claim(conn, claim_id, include_secret=True)
    assert claim is not None
    if as_json:
        click.echo(json.dumps(claim, indent=2))
        return
    click.echo(f"Claim #{claim_id}  actor={claim['actor']}  type={claim['claim_type']}")
    click.echo(f"  expires={claim['expires_at']}  identity_status={claim['identity_status']}")
    click.echo(f"  claim_token: {claim['claim_token']}")


@claim.command("resume")
@click.option("--instance-id", default=None, help="Your stable instance ID (preferred)")
@click.option("--runtime-session-id", default=None, help="Your runtime session ID")
@click.option("--hostname", default=None, help="Hostname (use with --pid)")
@click.option("--pid", type=int, default=None, help="PID (use with --hostname)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_resume(obj, instance_id, runtime_session_id, hostname, pid, as_json) -> None:
    """Find active claims matching your agent identity for session resumption.

    Use this when restarting after context loss to locate your existing claims.
    Claims are returned without the token — use 'claim show' with the token once
    recovered, or 'claim handoff --allow-legacy-adopt' to re-mint a fresh proof.
    Provide at least one of: --instance-id, --runtime-session-id, or --hostname + --pid.
    """
    conn = _get_conn(obj)
    try:
        claims = _db.find_claim_by_identity(
            conn,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            runtime_session_id=runtime_session_id,
            active_only=True,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        click.echo("No active claims found matching the provided identity.")
        return
    click.echo(f"Found {len(claims)} active claim(s) matching your identity:")
    for c in claims:
        click.echo(
            f"  #{c['claim_id']}  item #{c['work_item_id']}  {c['actor']}  "
            f"[{c['claim_type']}]  expires={c['expires_at']}  "
            f"proof={c['identity_status']}"
        )
    click.echo("Use 'claim show --id <id> --claim-token <token>' to re-display the token if still held.")
    click.echo("Use 'claim handoff --allow-legacy-adopt' if the token is lost and the claim has no secret.")


def _render_handoff_text(bundle: dict) -> str:
    """Render a handoff bundle as a human-readable text summary."""
    s = bundle["sprint"]
    items = bundle["items"]
    claims = bundle["active_claims"]
    events = bundle["events"]

    lines: list[str] = []
    lines.append(f"=== HANDOFF: {s['name']}  [{s['status']}] ===")
    lines.append(f"Generated: {bundle['generated_at']}")
    if s.get("goal"):
        lines.append(f"Goal: {s['goal']}")
    if s.get("start_date") and s.get("end_date"):
        lines.append(f"Dates: {s['start_date']} to {s['end_date']}")
    lines.append("")

    by_status: dict[str, list] = {"active": [], "pending": [], "blocked": [], "done": []}
    for it in items:
        by_status.setdefault(it["status"], []).append(it)

    for status in ("active", "pending", "blocked", "done"):
        group = by_status[status]
        if not group:
            continue
        lines.append(f"{status.upper()} ({len(group)}):")
        for it in group:
            assignee = it.get("assignee") or "-"
            lines.append(f"  #{it['id']}  {it['title']}  [track: {it['track_name']}]  assignee: {assignee}")
        lines.append("")

    if claims:
        lines.append(f"ACTIVE CLAIMS ({len(claims)}):")
        for c in claims:
            excl = "exclusive" if c["exclusive"] else "shared"
            lines.append(
                f"  #{c['claim_id']}  item #{c['work_item_id']} ({c.get('item_title', '')})  "
                f"{c['actor']}  [{c['claim_type']}]  {excl}  expires={c['expires_at']}"
            )
        lines.append("")
        lines.append("NOTE: Incoming agent must claim handoff or release each active claim.")
        lines.append("")

    if events:
        lines.append(f"RECENT EVENTS ({len(events)}):")
        for e in events[-10:]:
            item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
            lines.append(f"  [{e['event_type']}]  {e['actor']}  {e['created_at']}{item_label}")
        lines.append("")

    lines.append("SHUTDOWN PROTOCOL:")
    for step in bundle.get("agent_shutdown_protocol", {}).get("required_before_termination", []):
        lines.append(f"  - {step}")

    return "\n".join(lines)


@cli.command("handoff")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--output", "output_path", default=None, help="Output file path (default: handoff-N.json or handoff-N.txt)")
@click.option("--events", "events_limit", type=int, default=50, help="Recent events to include (default: 50)")
@click.option(
    "--format", "fmt",
    default="json",
    type=click.Choice(["json", "text"]),
    help="Output format: json (default) or text (human-readable summary)",
)
@click.pass_obj
def handoff_cmd(obj, sprint_id, output_path, events_limit, fmt) -> None:
    """Produce a handoff bundle: sprint, items, recent events, active claims.

    Use --format text for a human-readable summary suitable for LLM context injection.
    Use --format json (default) for a machine-parseable bundle.
    Pass --output - to write to stdout regardless of format.
    """
    conn = _get_conn(obj)
    if sprint_id is not None:
        s = _db.get_sprint(conn, sprint_id)
    else:
        s = _db.get_active_sprint(conn)
    if s is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    sid = s["id"]
    items = _db.list_work_items(conn, sprint_id=sid)
    recent_events = _db.list_events_limited(conn, sid, limit=events_limit)
    active_claims = _db.list_claims_by_sprint(conn, sid, active_only=True)
    bundle = {
        "sprintctl_version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": dict(s),
        "items": items,
        "events": recent_events,
        "active_claims": active_claims,
        "claim_identity_model": {
            "ownership_proof": "claim_id+claim_token",
            "claim_tokens_included": False,
            "ambiguous_identity_visible": True,
            "explicit_claim_handoff_command": "sprintctl claim handoff",
        },
        "agent_shutdown_protocol": {
            "required_before_termination": [
                "For each active claim you own: run 'sprintctl claim handoff --id <id> --claim-token <token> --actor <next-agent> --mode rotate' to pass ownership to the incoming session.",
                "If no incoming session: run 'sprintctl claim release --id <id> --claim-token <token>' to free each claim.",
                "If handing off the sprint: run 'sprintctl handoff' to produce a new bundle for the next agent.",
            ],
            "resumption_hint": (
                "Incoming agents: use 'sprintctl claim resume --instance-id <id>' or "
                "'--runtime-session-id <id>' to locate claims transferred to you."
            ),
        },
    }

    if fmt == "text":
        content = _render_handoff_text(bundle)
        ext = ".txt"
    else:
        content = json.dumps(bundle, indent=2)
        ext = ".json"

    dest = output_path or f"handoff-{sid}{ext}"
    if dest == "-":
        click.echo(content)
        return
    with open(dest, "w") as fh:
        fh.write(content)
        if not content.endswith("\n"):
            fh.write("\n")
    click.echo(f"Handoff bundle for sprint #{sid} written to {dest}")


@cli.command("agent-protocol")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def agent_protocol_cmd(as_json) -> None:
    """Print the claim lifecycle protocol for agent consumption.

    Outputs a structured summary of how agents should interact with sprintctl
    claims: startup, heartbeat, handoff, and shutdown steps. Suitable for
    injecting into an agent system prompt or reading programmatically.
    """
    protocol = {
        "sprintctl_agent_protocol_version": "1",
        "claim_model": {
            "ownership_proof": "claim_id + claim_token (both required; token is a server-minted opaque secret)",
            "ttl_seconds_default": 300,
            "claim_types": {
                "execute": "Exclusive. Agent is implementing work on the item.",
                "inspect": "Exclusive. Agent is reading item state.",
                "review": "Exclusive. Agent is reviewing completed work.",
                "coordinate": "Exclusive. Orchestrator managing sub-agents. Sub-agents may claim execute under it.",
            },
        },
        "lifecycle": {
            "1_startup": {
                "description": "Claim the item before beginning work.",
                "command": (
                    "sprintctl claim create --item-id <id> --actor <name> "
                    "[--type execute|coordinate] [--ttl <seconds>] "
                    "[--runtime-session-id <env-session-id>] [--instance-id <stable-per-process-uuid>] "
                    "[--branch <branch>] --json"
                ),
                "store": "Save claim_id and claim_token for the entire session. Treat claim_token as a secret.",
                "coordinator_note": (
                    "If acting as an orchestrator, claim with --type coordinate first, then spawn sub-agents "
                    "that call 'claim create' with --coordinate-claim-id and --coordinate-claim-token."
                ),
            },
            "2_heartbeat": {
                "description": "Refresh the claim TTL periodically (every ~half the TTL).",
                "command": (
                    "sprintctl claim heartbeat --id <claim_id> --claim-token <token> "
                    "[--ttl <seconds>] [--actor <name>]"
                ),
                "frequency": "Every 120s if TTL=300s. Increase --ttl for long-running tasks.",
            },
            "3_status_transition": {
                "description": "Transition item status. Claim proof is required.",
                "command": (
                    "sprintctl item status --id <item_id> --status active|done|blocked "
                    "--actor <name> --claim-id <claim_id> --claim-token <token>"
                ),
            },
            "4_handoff": {
                "description": "Pass claim ownership to an incoming agent session (required on shutdown if work continues).",
                "command": (
                    "sprintctl claim handoff --id <claim_id> --claim-token <token> "
                    "--actor <next-agent-name> --mode rotate "
                    "[--runtime-session-id <next-session-id>] [--instance-id <next-instance-id>] --json"
                ),
                "note": "The returned claim_token is the new agent's secret. The old token is invalidated.",
            },
            "5_release": {
                "description": "Release the claim when work is complete and no handoff is needed.",
                "command": "sprintctl claim release --id <claim_id> --claim-token <token> --actor <name>",
            },
        },
        "session_resumption": {
            "description": "If context is lost, locate your claims by identity before re-claiming.",
            "command": (
                "sprintctl claim resume --instance-id <your-instance-id> "
                "[--runtime-session-id <id>] [--hostname <host> --pid <pid>] --json"
            ),
            "recovery": (
                "If token is still held: use 'claim show --id <id> --claim-token <token>' to re-display it. "
                "If token is lost: use 'claim handoff --allow-legacy-adopt' to mint a fresh proof."
            ),
        },
        "shutdown_checklist": [
            "For each owned claim: handoff to next agent OR release.",
            "Run 'sprintctl handoff' to write a bundle for the incoming session.",
        ],
        "environment_hints": {
            "SPRINTCTL_RUNTIME_SESSION_ID": "Set to your runtime session ID (auto-detected from CODEX_THREAD_ID).",
            "SPRINTCTL_INSTANCE_ID": "Set to a stable per-process UUID; persisted across heartbeats.",
            "SPRINTCTL_DB": "Override the database path (default: ~/.local/share/sprintctl/sprintctl.db).",
        },
    }
    if as_json:
        click.echo(json.dumps(protocol, indent=2))
        return

    click.echo("=== sprintctl Agent Claim Protocol ===\n")
    click.echo(f"Ownership proof: {protocol['claim_model']['ownership_proof']}\n")
    click.echo("Lifecycle steps:")
    for step, info in protocol["lifecycle"].items():
        click.echo(f"\n  {step}: {info['description']}")
        click.echo(f"    $ {info['command']}")
        for key in ("store", "frequency", "note", "coordinator_note"):
            if key in info:
                click.echo(f"    [{key}] {info[key]}")
    click.echo("\nSession resumption:")
    click.echo(f"  $ {protocol['session_resumption']['command']}")
    click.echo(f"  {protocol['session_resumption']['recovery']}")
    click.echo("\nShutdown checklist:")
    for item in protocol["shutdown_checklist"]:
        click.echo(f"  - {item}")
    click.echo("\nEnvironment variables:")
    for var, desc in protocol["environment_hints"].items():
        click.echo(f"  {var}: {desc}")


@cli.command("next-work")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def next_work_cmd(obj, sprint_id, as_json) -> None:
    """Suggest pending items that are ready to start (no unresolved blocking deps).

    Items are listed in creation order. Items blocked by incomplete predecessors
    are excluded from the suggestion.
    """
    conn = _get_conn(obj)
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
    ready = _db.get_ready_items(conn, s["id"])
    if as_json:
        click.echo(json.dumps(ready, indent=2))
        return
    if not ready:
        click.echo(f"No pending items ready to start in sprint #{s['id']} ({s['name']}).")
        return
    click.echo(f"Ready to start in sprint #{s['id']} ({s['name']}):")
    for it in ready:
        assignee = it.get("assignee") or "-"
        click.echo(
            f"  #{it['id']}  {it['title']}  [track: {it['track_name']}]  assignee: {assignee}"
        )


@cli.command("usage")
@click.option(
    "--context",
    "as_context",
    is_flag=True,
    default=False,
    help="Emit current sprint context (active claims, stale/blocked items, ready work, recent decisions)",
)
@click.option("--sprint-id", type=int, default=None, help="Sprint ID for --context (defaults to active)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output --context as JSON")
@click.pass_obj
def usage_cmd(obj, as_context, sprint_id, as_json) -> None:
    """Print a compact command reference, or current sprint context with --context."""
    if as_context:
        conn = _get_conn(obj)
        s = _resolve_sprint(conn, sprint_id)
        now = datetime.now(timezone.utc)

        # active claims
        active_claims = _db.list_claims_by_sprint(conn, s["id"], active_only=True)

        # stale items (use env threshold)
        report = _maintain.check(conn, s["id"], now)
        stale_items = report["stale_items"]

        # all items — derive blocked and pending counts
        all_items = _db.list_work_items(conn, sprint_id=s["id"])
        blocked_items = [it for it in all_items if it["status"] == "blocked"]
        active_items = [it for it in all_items if it["status"] == "active"]
        done_items = [it for it in all_items if it["status"] == "done"]
        pending_items = [it for it in all_items if it["status"] == "pending"]

        # ready items (pending with no unresolved blockers)
        ready_items = _db.get_ready_items(conn, s["id"])

        # recent knowledge candidates (last 5)
        knowledge = _db.list_knowledge_candidates(conn, s["id"])
        recent_decisions = knowledge[-5:] if len(knowledge) > 5 else knowledge

        if as_json:
            out = {
                "sprint": {
                    "id": s["id"],
                    "name": s["name"],
                    "goal": s["goal"],
                    "status": s["status"],
                    "start_date": s.get("start_date"),
                    "end_date": s.get("end_date"),
                },
                "summary": {
                    "total": len(all_items),
                    "done": len(done_items),
                    "active": len(active_items),
                    "pending": len(pending_items),
                    "blocked": len(blocked_items),
                    "stale": len(stale_items),
                    "ready": len(ready_items),
                    "active_claims": len(active_claims),
                },
                "active_claims": active_claims,
                "stale_items": [
                    {"id": it["id"], "title": it["title"], "status": it["status"],
                     "track": it["track_name"], "idle_seconds": it["idle_seconds"]}
                    for it in stale_items
                ],
                "blocked_items": [
                    {"id": it["id"], "title": it["title"], "track": it["track_name"]}
                    for it in blocked_items
                ],
                "ready_items": [
                    {"id": it["id"], "title": it["title"], "track": it["track_name"]}
                    for it in ready_items
                ],
                "recent_decisions": recent_decisions,
            }
            click.echo(json.dumps(out, indent=2))
            return

        # text output
        risk = report["risk"]
        risk_tag = ""
        if risk["overdue"]:
            risk_tag = "  [OVERDUE]"
        elif risk["at_risk"]:
            risk_tag = "  [AT RISK]"

        click.echo(f"Sprint #{s['id']}: {s['name']}{risk_tag}")
        click.echo(f"Goal: {s['goal']}")
        if s.get("start_date") and s.get("end_date"):
            click.echo(f"Dates: {s['start_date']} → {s['end_date']}")
        click.echo(
            f"Items: {len(all_items)} total — "
            f"{len(done_items)} done, {len(active_items)} active, "
            f"{len(pending_items)} pending, {len(blocked_items)} blocked"
        )
        click.echo("")

        click.echo(f"Active claims ({len(active_claims)}):")
        if active_claims:
            for cl in active_claims:
                item_title = cl.get("item_title") or f"item #{cl['item_id']}"
                actor = cl.get("actor") or "-"
                expires = cl.get("expires_at") or "no expiry"
                click.echo(f"  claim #{cl['id']}  [{actor}]  {item_title}  expires: {expires}")
        else:
            click.echo("  (none)")
        click.echo("")

        click.echo(f"Stale items ({len(stale_items)}):")
        if stale_items:
            for it in stale_items:
                h, rem = divmod(it["idle_seconds"], 3600)
                m = rem // 60
                click.echo(f"  #{it['id']}  [{it['status']:8}]  {it['title']}  — idle {h}h{m:02d}m  (track: {it['track_name']})")
        else:
            click.echo("  (none)")
        click.echo("")

        click.echo(f"Blocked items ({len(blocked_items)}):")
        if blocked_items:
            for it in blocked_items:
                click.echo(f"  #{it['id']}  {it['title']}  (track: {it['track_name']})")
        else:
            click.echo("  (none)")
        click.echo("")

        click.echo(f"Ready to start ({len(ready_items)}):")
        if ready_items:
            for it in ready_items[:5]:
                click.echo(f"  #{it['id']}  {it['title']}  (track: {it['track_name']})")
            if len(ready_items) > 5:
                click.echo(f"  … {len(ready_items) - 5} more")
        else:
            click.echo("  (none)")
        click.echo("")

        click.echo(f"Recent decisions ({len(recent_decisions)}):")
        if recent_decisions:
            for ev in recent_decisions:
                payload = ev.get("payload") or {}
                summary = payload.get("summary") or ev.get("event_type", "")
                click.echo(f"  [{ev['event_type']}]  {summary}")
        else:
            click.echo("  (none)")
        return

    lines = [
        "sprintctl — agent-centric sprint coordination CLI",
        "",
        "SPRINT",
        "  sprint create  --name NAME [--goal GOAL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]",
        "                 [--status planned|active|closed] [--kind active_sprint|backlog|archive]",
        "  sprint show    [--id ID] [--detail] [--json]",
        "  sprint status  --id ID --status planned|active|closed",
        "  sprint list    [--include-backlog] [--include-archive] [--json]",
        "  sprint kind    --id ID --kind active_sprint|backlog|archive",
        "",
        "ITEM",
        "  item add       --sprint-id ID --track NAME --title TITLE [--assignee NAME]",
        "  item show      --id ID [--json]",
        "  item list      [--sprint-id ID] [--track NAME] [--status STATUS] [--json]",
        "  item note      --id ID --type TYPE --summary TEXT [--detail TEXT] [--tags T1,T2]",
        "                 [--actor NAME]",
        "  item status    --id ID --status pending|active|done|blocked [--actor NAME]",
        "                 [--claim-id N --claim-token TOKEN]",
        "  item ref add   --id ID --type pr|issue|doc|other --url URL [--label TEXT]",
        "  item ref list  --id ID [--json]",
        "  item ref remove --id ID --ref-id N",
        "  item dep add   --id BLOCKER_ID --blocks-item-id BLOCKED_ID",
        "  item dep list  --id ID [--json]",
        "  item dep remove --id ID --dep-id N",
        "",
        "EVENT",
        "  event add      --sprint-id ID --type TYPE --actor NAME [--item-id ID]",
        "                 [--source actor|daemon|system] [--payload JSON]",
        "  event list     --sprint-id ID [--item-id ID] [--type TYPE] [--limit N] [--json]",
        "",
        "MAINTAIN",
        "  maintain check    [--sprint-id ID] [--threshold Nh] [--json]",
        "  maintain sweep    [--sprint-id ID] [--threshold Nh] [--auto-close]",
        "  maintain carryover --from-sprint ID --to-sprint ID",
        "",
        "CLAIM",
        "  claim create   --item-id ID --actor NAME [--type execute|inspect|review|coordinate]",
        "                 [--ttl N] [--non-exclusive] [--branch B] [--worktree PATH]",
        "                 [--commit-sha SHA] [--pr-ref REF] [--runtime-session-id ID]",
        "                 [--instance-id ID] [--coordinate-claim-id N --coordinate-claim-token T]",
        "                 [--json]",
        "  claim heartbeat --id N --claim-token TOKEN [--ttl N] [--actor NAME] [--json]",
        "  claim release  --id N --claim-token TOKEN [--actor NAME]",
        "  claim handoff  --id N --claim-token TOKEN --actor NAME [--mode transfer|rotate]",
        "                 [--ttl N] [--note TEXT] [--allow-legacy-adopt] [--output PATH] [--json]",
        "  claim list     --item-id ID [--all] [--json]",
        "  claim list-sprint [--sprint-id ID] [--all] [--expiring-within N] [--json]",
        "  claim show     --id N --claim-token TOKEN [--json]",
        "  claim resume   [--instance-id ID] [--runtime-session-id ID]",
        "                 [--hostname H --pid N] [--json]",
        "",
        "TOP-LEVEL",
        "  export         --sprint-id ID [--output PATH]",
        "  import         --file PATH",
        "  handoff        [--sprint-id ID] [--output PATH] [--events N] [--format json|text]",
        "  render         [--sprint-id ID] [--output PATH]",
        "  next-work      [--sprint-id ID] [--json]",
        "  agent-protocol [--json]",
        "  usage          [--context] [--sprint-id ID] [--json]",
        "",
        "ENV",
        "  SPRINTCTL_DB                    Database path (default: ~/.sprintctl/sprintctl.db)",
        "  SPRINTCTL_STALE_THRESHOLD       Active item staleness in hours (default: 4)",
        "  SPRINTCTL_PENDING_STALE_THRESHOLD  Pending item staleness threshold (default: off)",
        "  SPRINTCTL_RUNTIME_SESSION_ID    Runtime session ID (auto-detected from CODEX_THREAD_ID)",
        "  SPRINTCTL_INSTANCE_ID           Stable per-process instance UUID",
    ]
    click.echo("\n".join(lines))


# ---------------------------------------------------------------------------
# git-context
# ---------------------------------------------------------------------------


@cli.command("git-context")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def git_context_cmd(as_json) -> None:
    """Show the current git branch, commit SHA, and worktree path."""
    import subprocess  # noqa: PLC0415

    def _run(args: list[str]) -> str:
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException("Not inside a git repository.")
        return r.stdout.strip()

    try:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        sha = _run(["git", "rev-parse", "HEAD"])
        worktree = _run(["git", "rev-parse", "--show-toplevel"])
    except click.ClickException:
        click.echo("Error: not a git repository.", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps({"branch": branch, "sha": sha, "worktree": worktree}))
        return
    click.echo(f"Branch:   {branch}")
    click.echo(f"SHA:      {sha}")
    click.echo(f"Worktree: {worktree}")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@cli.command("render")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--output", "output_path", default=None, help="Write rendered doc to a file instead of stdout")
@click.pass_obj
def render_cmd(obj, sprint_id, output_path) -> None:
    """Render a plain-text sprint document."""
    conn = _get_conn(obj)
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
    refs_by_item: dict[int, list[dict]] = {}
    for it in all_items:
        item_refs = _db.list_refs(conn, it["id"])
        if item_refs:
            refs_by_item[it["id"]] = item_refs
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = render_sprint_doc(s, tracks, items_by_track, rendered_at, refs_by_item=refs_by_item)
    if output_path:
        with open(output_path, "w") as fh:
            fh.write(doc + "\n")
        click.echo(f"Sprint #{s['id']} rendered to {output_path}")
    else:
        click.echo(doc)
