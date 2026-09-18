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
