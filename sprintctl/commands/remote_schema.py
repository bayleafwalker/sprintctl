"""CLI boundaries for runtime schema checks and deployment migrations."""

from __future__ import annotations

import json
import os

import click

from .. import cli_support


@click.group("remote-schema")
def remote_schema() -> None:
    """Check or migrate the shared PostgreSQL work schema."""


def register(root: click.Group) -> None:
    """Attach the remote-schema group to the root CLI."""
    root.add_command(remote_schema)


def _remote_schema_store(pg_url: str | None):
    # Keep PostgreSQL optional for local-only command invocations.
    from .. import pg as _pg

    url = pg_url or os.environ.get("SPRINTCTL_URL")
    if not url:
        raise click.ClickException(
            "Postgres URL required. Pass --url or set SPRINTCTL_URL."
        )
    try:
        return _pg.get_connection(url)
    except Exception as exc:
        detail = cli_support._redacted_postgres_error(exc, url)
        raise click.ClickException(
            f"could not connect to PostgreSQL for remote schema operation: {detail}"
        ) from exc


@remote_schema.command("check")
@click.option("--url", "pg_url", default=None, help="Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the compatibility handshake as JSON")
def remote_schema_check_cmd(pg_url: str | None, as_json: bool) -> None:
    """Read the work API/schema handshake without attempting DDL."""
    from .. import pg_migrations as _pg_migrations

    store = _remote_schema_store(pg_url)
    try:
        handshake = _pg_migrations.compatibility_handshake(store)
        store.conn.rollback()
    finally:
        store.conn.close()
    if as_json:
        click.echo(json.dumps(handshake, indent=2, sort_keys=True))
    else:
        actual = handshake["remote_schema"]["actual"]
        click.echo(
            f"work_api={handshake['work_api_version']} "
            f"remote_schema={actual if actual is not None else 'missing'} "
            f"compatible={'yes' if handshake['compatible'] else 'no'}"
        )
    if not handshake["compatible"]:
        raise click.exceptions.Exit(1)


@remote_schema.command("migrate")
@click.option("--url", "pg_url", default=None, help="Migration-role Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the migration result as JSON")
def remote_schema_migrate_cmd(pg_url: str | None, as_json: bool) -> None:
    """Apply serialized idempotent migrations with a migration-role credential."""
    from .. import pg_migrations as _pg_migrations

    store = _remote_schema_store(pg_url)
    try:
        result = _pg_migrations.migrate_schema(store)
    finally:
        store.conn.close()
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        versions = ",".join(str(value) for value in result["applied_versions"])
        click.echo(
            f"remote schema {result['from_version']} -> {result['to_version']}; "
            f"applied={versions or 'none'}"
        )


@remote_schema.command("stage-maintenance-bridge")
@click.option("--url", "pg_url", default=None, help="Migration-role Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the staging result as JSON")
def remote_schema_stage_maintenance_bridge_cmd(pg_url: str | None, as_json: bool) -> None:
    """Pre-provision complete maintenance storage on an exact v5 ledger."""
    from .. import pg_migrations as _pg_migrations

    store = _remote_schema_store(pg_url)
    try:
        result = _pg_migrations.stage_schema5_maintenance_bridge(store)
    finally:
        store.conn.close()
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            "schema-5 maintenance bridge "
            + ("installed" if result["installed"] else "already complete")
        )
