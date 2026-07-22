"""Operation-invocation facade for ``SPRINTCTL_BACKEND=served`` (#1195).

Each public function below performs exactly one ``asyncio.run(...)`` call
that constructs a *fresh* ``vuoro_client.AsyncVuoroClient`` inside that call,
invokes exactly one catalog operation from the served-mode allowlist
described in :mod:`sprintctl.served_routes`, and returns the operation's
JSON-safe result (or lets whatever the client raised propagate).

A client's underlying ``httpx.AsyncClient`` is bound to the event loop it was
built in, so a client must never be constructed outside ``asyncio.run(...)``
and never reused across separate ``asyncio.run(...)`` calls -- reusing one
across dead event loops breaks its transport on the next call. There is
exactly one client construction site (:func:`_client`) and every operation
below goes through it inside its own ``asyncio.run(...)``.

``vuoro_client`` is imported lazily, inside the coroutines that need it, so
importing this module -- and transitively ``sprintctl.cli`` -- never requires
the ``served`` extra to be installed; only invoking a served operation does.
This module also never imports ``psycopg``, ``sprintctl.pg`` or
``sprintctl.pg_migrations``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .backend import ServedProfile
from .served_routes import SERVED_COMMAND_ROUTES
from .vuoro_credentials import resolve_file_credential


def _client_profile(served_profile: ServedProfile) -> Any:
    from vuoro_client import Profile  # noqa: PLC0415 - optional extra, lazy import

    return Profile(
        name=served_profile.name,
        endpoint=served_profile.endpoint,
        credential_ref=served_profile.credential_ref,
        expected_environment=served_profile.expected_environment,
    )


def _client(served_profile: ServedProfile) -> Any:
    """Construct one fresh ``AsyncVuoroClient``. Callers must use it as an
    ``async with`` block inside the coroutine passed to a single
    ``asyncio.run(...)`` call -- never store or reuse the instance it
    returns."""

    from vuoro_client import AsyncVuoroClient  # noqa: PLC0415 - optional extra, lazy import

    return AsyncVuoroClient(_client_profile(served_profile), resolve_file_credential)


async def _invoke_operation(
    served_profile: ServedProfile, operation: str, arguments: dict[str, Any]
) -> Any:
    async with _client(served_profile) as client:
        return await client.invoke(operation, arguments)


def read_sprints(
    served_profile: ServedProfile,
    *,
    include_backlog: bool = False,
    include_archive: bool = False,
    active_only: bool = False,
) -> dict[str, Any]:
    """Invoke ``work.read.sprints`` (``sprintctl sprint list --json``)."""

    arguments = {
        "include_backlog": include_backlog,
        "include_archive": include_archive,
        "active_only": active_only,
    }
    return asyncio.run(_invoke_operation(served_profile, "work.read.sprints", arguments))


def read_item(served_profile: ServedProfile, *, item_id: int) -> dict[str, Any]:
    """Invoke ``work.read.item`` (``sprintctl item show --id ID --json``)."""

    return asyncio.run(
        _invoke_operation(served_profile, "work.read.item", {"item_id": item_id})
    )


def read_next_work(
    served_profile: ServedProfile, *, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.read.next-work`` (``sprintctl next-work --json``, no ``--project``)."""

    arguments = {"sprint_id": sprint_id}
    return asyncio.run(_invoke_operation(served_profile, "work.read.next-work", arguments))


def project_next_work(
    served_profile: ServedProfile, *, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.project.next-work`` (``sprintctl next-work --json --project X``)."""

    arguments = {"sprint_id": sprint_id}
    return asyncio.run(
        _invoke_operation(served_profile, "work.project.next-work", arguments)
    )


def claim_start(
    served_profile: ServedProfile,
    *,
    item_id: int,
    ttl_seconds: int = 300,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Invoke ``work.claim.start`` (``sprintctl claim start ...``).

    Per the "Authority and retry semantics" section of
    ``docs/reference/vuoro-work-adapter.md``, ``work.claim.start``'s catalog
    contract forbids an idempotency key and callers must not retry an
    unknown outcome -- so this performs exactly one invocation with no
    idempotency key and no retry wrapper around it. The claim's owning actor
    is the authenticated identity the server resolves from the credential,
    not a caller-supplied argument, so no ``actor``/``agent`` field is sent.
    """

    arguments = {
        "item_id": item_id,
        "ttl_seconds": ttl_seconds,
        "branch": branch,
        "worktree_path": worktree_path,
        "commit_sha": commit_sha,
        "pr_ref": pr_ref,
        "runtime_session_id": runtime_session_id,
        "instance_id": instance_id,
        "hostname": hostname,
        "pid": pid,
    }
    return asyncio.run(_invoke_operation(served_profile, "work.claim.start", arguments))


# The subset of served_routes.py's allowlist that doctor's served probe
# checks for -- the exact 5 catalog operations #1195 wires through this
# facade (next-work contributes two: work.read.next-work and
# work.project.next-work).
_DOCTOR_PROBE_COMMAND_PATHS = ("sprint.list", "item.show", "next-work", "claim.start")

EXPECTED_OPERATIONS: frozenset[str] = frozenset(
    route.operation
    for route in SERVED_COMMAND_ROUTES
    if route.command_path in _DOCTOR_PROBE_COMMAND_PATHS
)


async def _catalog_operation_names(served_profile: ServedProfile) -> frozenset[str]:
    async with _client(served_profile) as client:
        catalog = await client.catalog()
    return frozenset(operation["name"] for operation in catalog.get("operations", []))


def catalog_operation_names(served_profile: ServedProfile) -> frozenset[str]:
    """Return the served catalog's operation names.

    Used by ``sprintctl doctor``'s served probe to confirm the catalog
    exposes the operations this facade depends on. One ``asyncio.run(...)``
    call with a fresh client, matching every function above; this performs
    no authenticated invocation (catalog discovery is unauthenticated), so it
    never touches credential resolution.
    """

    return asyncio.run(_catalog_operation_names(served_profile))


__all__ = [
    "EXPECTED_OPERATIONS",
    "catalog_operation_names",
    "claim_start",
    "project_next_work",
    "read_item",
    "read_next_work",
    "read_sprints",
]
