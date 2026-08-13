"""CLI boundaries for direct PostgreSQL repository administration."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import click


GetStore = Callable[[dict[str, Any]], tuple[Any, Any]]
_get_store: GetStore | None = None


@click.group()
def repo() -> None:
    """Manage repositories in remote postgres."""


def register(root: click.Group, *, get_store: GetStore) -> None:
    """Attach the repo group and its injected store boundary to the root CLI."""
    global _get_store
    _get_store = get_store
    root.add_command(repo)


def _registered_store(obj: dict[str, Any]) -> tuple[Any, Any]:
    if _get_store is None:
        raise AssertionError("repo commands must be registered before invocation")
    return _get_store(obj)


@repo.command("list")
@click.pass_obj
def repo_list(obj: dict[str, Any]) -> None:
    """List all repo_ids present in remote postgres."""
    store, _module = _registered_store(obj)
    if not hasattr(store, "conn"):
        click.echo(
            "Error: repo list requires remote backend "
            "(SPRINTCTL_BACKEND=remote)",
            err=True,
        )
        sys.exit(1)
    from .. import pg as _pg

    for repo_id in _pg.list_repos(store.conn):
        click.echo(repo_id)


@repo.command("delete")
@click.argument("repo_id")
@click.option(
    "--yes",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt",
)
@click.pass_obj
def repo_delete(obj: dict[str, Any], repo_id: str, skip_confirm: bool) -> None:
    """Delete all data for a repository from remote postgres."""
    store, _module = _registered_store(obj)
    if not hasattr(store, "conn"):
        click.echo(
            "Error: repo delete requires remote backend "
            "(SPRINTCTL_BACKEND=remote)",
            err=True,
        )
        sys.exit(1)
    from .. import pg as _pg

    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM sprint WHERE repo_id = %s",
            (repo_id,),
        )
        row = cur.fetchone()
        sprint_count = row["cnt"] if row else 0

    if sprint_count == 0:
        click.echo(f"No data found for repo_id '{repo_id}'.", err=True)
        sys.exit(1)

    if not skip_confirm:
        click.confirm(
            f"Delete all data for '{repo_id}' ({sprint_count} sprint(s))? "
            "This cannot be undone.",
            abort=True,
        )

    counts = _pg.delete_repo(store.conn, repo_id)
    total = sum(counts.values())
    deleted = {table: count for table, count in counts.items() if count > 0}
    click.echo(f"Deleted {total} rows for '{repo_id}': {deleted}")
