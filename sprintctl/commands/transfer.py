"""CLI boundaries for sprint export and import."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import click

from .. import __version__
from .. import contracts as _contracts
from .. import db as _db


GetConn = Callable[[dict[str, Any]], Any]
_get_conn: GetConn | None = None


def register(root: click.Group, *, get_conn: GetConn) -> None:
    """Attach the export and import commands with their connection seam."""
    global _get_conn
    _get_conn = get_conn
    root.add_command(export_cmd)
    root.add_command(import_cmd)


def _registered_conn(obj: dict[str, Any]) -> Any:
    if _get_conn is None:
        raise AssertionError("transfer commands must be registered before invocation")
    return _get_conn(obj)


@click.command("export")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID to export")
@click.option("--output", "output_path", default=None, help="Output file path (default: sprint-N.json)")
@click.pass_obj
def export_cmd(obj: dict[str, Any], sprint_id: int, output_path: str | None) -> None:
    """Export a sprint (sprint, tracks, items, events) to a JSON file."""
    conn = _registered_conn(obj)
    sprint = _db.get_sprint(conn, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    tracks = _db.list_tracks(conn, sprint_id)
    items = _db.list_work_items(conn, sprint_id=sprint_id)
    events = _db.list_events(conn, sprint_id)
    item_ids = [item["id"] for item in items]
    reservations = [
        reservation
        for item_id in item_ids
        for reservation in _db.list_reservations(conn, item_id, active_only=False)
    ]
    claim_history = []
    if item_ids:
        placeholders = ", ".join("?" for _ in item_ids)
        claim_history = [dict(row) for row in conn.execute(
            f"SELECT * FROM claim_history WHERE work_item_id IN ({placeholders})", item_ids
        )]
    refs_by_item: dict[int, list[dict]] = {}
    for item in items:
        item_refs = _db.list_refs(conn, item["id"])
        if item_refs:
            refs_by_item[item["id"]] = item_refs
    envelope = {
        "sprintctl_version": __version__,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": dict(sprint),
        "tracks": [dict(track) for track in tracks],
        "items": [dict(item) for item in items],
        "events": [dict(event) for event in events],
        "refs": refs_by_item,
        # Historical claims are archive evidence only; reservations retain
        # their advisory lifecycle state across a local backup/restore.
        "claim_history": claim_history,
        "reservations": reservations,
    }
    dest = output_path or f"sprint-{sprint_id}.json"
    with open(dest, "w") as file:
        json.dump(envelope, file, indent=2)
    click.echo(f"Exported sprint #{sprint_id} to {dest}")


@click.command("import")
@click.option("--file", "input_path", required=True, help="Path to exported sprint JSON file")
@click.pass_obj
def import_cmd(obj: dict[str, Any], input_path: str) -> None:
    """Import a sprint from an export JSON file (re-sequences all IDs)."""
    conn = _registered_conn(obj)
    try:
        with open(input_path) as file:
            envelope = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Failed to read {input_path}: {exc}", err=True)
        sys.exit(1)

    src_sprint = envelope["sprint"]

    # Pre-flight: all track_ids referenced by items must be present in tracks list.
    # Validate before writing anything so the DB is never left in a partial state.
    exported_track_ids = {track["id"] for track in envelope.get("tracks", [])}
    missing: list[str] = []
    for item in envelope.get("items", []):
        if item["track_id"] not in exported_track_ids:
            missing.append(
                f"  item '{item['title']}' references track_id {item['track_id']} "
                "not found in export"
            )
    if missing:
        click.echo("Import aborted — items reference tracks missing from the export file:", err=True)
        for message in missing:
            click.echo(message, err=True)
        sys.exit(1)

    # Validate and prepare every event before creating any rows. Local-authority
    # events are retained under explicit imported event types rather than replayed.
    prepared_events: list[dict] = []
    imported_history_count = 0
    try:
        for event in envelope.get("events", []):
            try:
                payload = json.loads(event.get("payload", "{}"))
            except (json.JSONDecodeError, TypeError):
                payload = {}
            event_type = event["event_type"]
            source_event_id = event["id"]
            archive_only = _contracts.requires_archive_import_handling(event_type)
            if archive_only:
                _contracts.canonicalize_event_for_archive_import(
                    event_type,
                    payload,
                    source_event_id,
                )
                imported_history_count += 1
            else:
                payload["source_id"] = source_event_id
                payload = _contracts.canonicalize_event_payload(event_type, payload)
            prepared_events.append({
                "event": event,
                "payload": payload,
                "archive_only": archive_only,
            })
    except (KeyError, TypeError, ValueError) as exc:
        click.echo(f"Import aborted — invalid event history: {exc}", err=True)
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
    for track in envelope.get("tracks", []):
        new_track_id = _db.get_or_create_track(
            conn,
            new_sprint_id,
            track["name"],
            track.get("description", ""),
        )
        track_id_map[track["id"]] = new_track_id

    # Map old item IDs → new item IDs
    item_id_map: dict[int, int] = {}
    for item in envelope.get("items", []):
        new_track_id = track_id_map[item["track_id"]]  # guaranteed present after pre-flight
        new_item_id = _db.create_work_item(
            conn,
            new_sprint_id,
            new_track_id,
            item["title"],
            description=item.get("description", ""),
            assignee=item.get("assignee"),
        )
        # Restore status via raw update (bypasses transition guard for import)
        imported_status = item.get("status", "pending")
        if imported_status != "pending":
            conn.execute(
                "UPDATE work_item SET status = ?, updated_at = ? WHERE id = ?",
                (imported_status, item.get("updated_at", item.get("created_at")), new_item_id),
            )
        item_id_map[item["id"]] = new_item_id

    # Re-insert generic events with source_id. Reserved typed events use an
    # explicit non-authoritative imported representation with an exact wrapper.
    for prepared in prepared_events:
        event = prepared["event"]
        old_item_id = event.get("work_item_id")
        new_item_id = item_id_map.get(old_item_id) if old_item_id is not None else None
        if prepared["archive_only"]:
            _db.create_archive_import_event(
                conn,
                new_sprint_id,
                actor=event["actor"],
                event_type=event["event_type"],
                source_event_id=event["id"],
                work_item_id=new_item_id,
                payload=prepared["payload"],
            )
        else:
            _db.create_event(
                conn,
                new_sprint_id,
                actor=event["actor"],
                event_type=event["event_type"],
                source_type=event.get("source_type", "system"),
                work_item_id=new_item_id,
                payload=prepared["payload"],
            )

    # Re-insert refs, remapping old item IDs to new item IDs
    refs_by_item = envelope.get("refs", {})
    for old_item_id_str, item_refs in refs_by_item.items():
        new_item_id = item_id_map.get(int(old_item_id_str))
        if new_item_id is None:
            continue
        for ref in item_refs:
            _db.add_ref(
                conn,
                new_item_id,
                ref["ref_type"],
                ref["url"],
                ref.get("label", ""),
            )

    for reservation in envelope.get("reservations", []):
        new_item_id = item_id_map.get(reservation.get("work_item_id"))
        if new_item_id is None:
            continue
        conn.execute(
            "INSERT INTO reservation(work_item_id, session_id, actor, role, state, created_at, last_activity_at, released_at, interruption_reason, correlation_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_item_id, reservation["session_id"], reservation["actor"], reservation["role"],
             reservation["state"], reservation["created_at"], reservation["last_activity_at"],
             reservation.get("released_at"), reservation.get("interruption_reason"), reservation.get("correlation_ref")),
        )

    history_columns = [row["name"] for row in conn.execute("PRAGMA table_info(claim_history)")]
    for historical_claim in envelope.get("claim_history", []):
        values = dict(historical_claim)
        new_item_id = item_id_map.get(values.get("work_item_id"))
        if new_item_id is None:
            continue
        values["work_item_id"] = new_item_id
        columns = [column for column in history_columns if column != "id" and column in values]
        conn.execute(
            f"INSERT INTO claim_history ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )

    conn.commit()
    click.echo(
        f"Imported sprint '{src_sprint['name']}' as #{new_sprint_id} "
        f"({len(item_id_map)} items, {len(envelope.get('events', []))} events)"
    )
    if imported_history_count:
        click.echo(
            f"Retained {imported_history_count} reserved typed event(s) as "
            "non-authoritative imported history."
        )
