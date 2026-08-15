"""CLI boundaries for local database maintenance and remote recovery."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from .. import backend as _backend
from .. import db as _db
from .. import cli_support


GetStore = Callable[[dict[str, Any]], tuple[Any, Any]]
_get_store: GetStore | None = None


@click.group("db")
def db_group() -> None:
    """Database maintenance (vacuum, integrity)."""


def register(root: click.Group, *, get_store: GetStore) -> None:
    """Attach the database group and inject its store lookup seam."""
    global _get_store
    _get_store = get_store
    root.add_command(db_group)


def _registered_store(obj: dict[str, Any]) -> tuple[Any, Any]:
    if _get_store is None:
        raise AssertionError("db commands must be registered before invocation")
    return _get_store(obj)


@db_group.command("vacuum")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def db_vacuum(obj: dict[str, Any], as_json: bool) -> None:
    """Reclaim space: VACUUM the local sqlite DB or the remote repo tables."""
    store, module = _registered_store(obj)
    report = module.vacuum_database(store)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    if report["backend"] == "local":
        click.echo(
            f"VACUUM complete: {report['pages_before']} -> {report['pages_after']} pages"
            f" ({report['bytes_reclaimed']} bytes reclaimed)."
        )
    else:
        click.echo(
            "VACUUM (ANALYZE) complete on: " + ", ".join(report["vacuumed_tables"])
        )


@db_group.command("integrity")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def db_integrity(obj: dict[str, Any], as_json: bool) -> None:
    """Check structural integrity and referential consistency (read-only)."""
    store, module = _registered_store(obj)
    report = module.check_integrity(store)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        if not report["ok"]:
            sys.exit(1)
        return
    status = "ok" if report["ok"] else "FAILED"
    click.echo(f"Integrity: {status}")
    for line in report["integrity_check"]:
        if line != "ok":
            click.echo(f"  {line}")
    for violation in report["foreign_key_violations"]:
        click.echo(f"  fk violation: {violation}")
    counts = ", ".join(f"{table}={count}" for table, count in report["table_counts"].items())
    click.echo(f"Rows: {counts}")
    if not report["ok"]:
        sys.exit(1)


@db_group.command("recover-from-remote")
@click.option("--output", "output_path", required=True, help="Path to write the recovered SQLite database")
@click.option(
    "--verify",
    "run_verify",
    is_flag=True,
    default=False,
    help="Run parity and integrity checks on the recovered database after writing it",
)
def db_recover_from_remote(output_path: str, run_verify: bool) -> None:
    """Read every repo-scoped row from the served remote authority (Postgres)
    and write a fresh, ID-preserving recovery SQLite database. Read-only
    against Postgres; refuses to overwrite an existing --output file."""
    dest = Path(output_path)
    if dest.exists():
        click.echo(f"Error: output path already exists: {dest}", err=True)
        sys.exit(1)

    try:
        config = _backend.require_remote_backend()
    except _backend.BackendConfigError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    from .. import pg as _pg  # noqa: PLC0415

    try:
        store = _pg.get_connection(config.url)
    except Exception as exc:
        detail = cli_support._redacted_postgres_error(exc, config.url)
        click.echo(f"Error: could not connect to postgres from SPRINTCTL_URL: {detail}", err=True)
        sys.exit(1)
    try:
        snapshot = _pg.recover_repo_snapshot(store)
    finally:
        store.conn.close()

    conn = _db.get_connection(dest)
    write_error: Exception | None = None
    try:
        _db.init_db(conn)
        recovered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        counts = _db.write_recovery_snapshot(
            conn,
            snapshot,
            provenance={
                "recovered_at": recovered_at,
                "source_repo_id": config.repo_id,
            },
        )
    except Exception as exc:
        write_error = exc
    finally:
        conn.close()

    if write_error is not None:
        dest.unlink(missing_ok=True)
        if isinstance(write_error, _db.RecoverySchemaMismatch):
            click.echo(f"Error: {write_error}", err=True)
            click.echo(
                "Remote and local schemas have drifted; recovery fails closed "
                "rather than writing partial rows.",
                err=True,
            )
            sys.exit(1)
        raise write_error

    reservations_interrupted = sum(
        1 for row in snapshot.get("reservation", []) if row.get("state") == "active"
    )
    click.echo(f"Recovered repo '{config.repo_id}' to {dest}")
    for table, count in counts.items():
        click.echo(f"  {table}: {count}")
    click.echo(
        f"  active reservations interrupted: {reservations_interrupted} "
        "(a recovered database is a new authority instance; work must be "
        "re-reserved against it)"
    )

    if not run_verify:
        return

    verify_conn = _db.get_connection(dest)
    try:
        report = _db.check_integrity(verify_conn)
    finally:
        verify_conn.close()

    click.echo("")
    click.echo("Parity report (Postgres source vs recovered SQLite):")
    parity_ok = True
    for table in (
        "sprint", "track", "work_item", "reservation",
        "claim_history", "ref", "dep",
    ):
        source_count = len(snapshot.get(table, []))
        destination_count = report["table_counts"].get(table, 0)
        status = "ok" if source_count == destination_count else "MISMATCH"
        if status != "ok":
            parity_ok = False
        click.echo(f"  {table}: source={source_count} recovered={destination_count} [{status}]")
    # event count in the recovered DB includes one synthetic recovery.completed
    # event per recovered sprint, on top of the recovered source events.
    source_events = len(snapshot.get("event", []))
    expected_events = source_events + len(snapshot.get("sprint", []))
    destination_events = report["table_counts"].get("event", 0)
    status = "ok" if expected_events == destination_events else "MISMATCH"
    if status != "ok":
        parity_ok = False
    click.echo(
        f"  event: source={source_events} (+{len(snapshot.get('sprint', []))} recovery.completed) "
        f"recovered={destination_events} [{status}]"
    )

    click.echo("")
    click.echo(f"Integrity: {'ok' if report['ok'] else 'FAILED'}")
    for line in report["integrity_check"]:
        if line != "ok":
            click.echo(f"  {line}")
    for violation in report["foreign_key_violations"]:
        click.echo(f"  fk violation: {violation}")

    if not parity_ok or not report["ok"]:
        sys.exit(1)
