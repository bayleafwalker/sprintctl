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
    served_profile: ServedProfile,
    operation: str,
    arguments: dict[str, Any],
    **kwargs: Any,
) -> Any:
    async with _client(served_profile) as client:
        return await client.invoke(operation, arguments, **kwargs)


def read_sprints(
    served_profile: ServedProfile,
    *,
    repo_id: str,
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
    return asyncio.run(
        _invoke_operation(served_profile, "work.read.sprints", arguments, repo_id=repo_id)
    )


def read_item(
    served_profile: ServedProfile, *, repo_id: str, item_id: int
) -> dict[str, Any]:
    """Invoke ``work.read.item`` (``sprintctl item show --id ID --json``)."""

    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.item", {"item_id": item_id}, repo_id=repo_id
        )
    )


def read_next_work(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.read.next-work`` (``sprintctl next-work --json``, no ``--project``)."""

    arguments = {"sprint_id": sprint_id}
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.next-work", arguments, repo_id=repo_id
        )
    )


def read_events(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    sprint_id: int,
    work_item_id: int | None = None,
    after_offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Invoke ``work.read.events`` (``sprintctl event list --sprint-id ID``).

    ``work_item_id`` filters server-side (cheap, matches the ``work.read.item``
    pattern). ``event_type``/``--knowledge`` filtering stays client-side in the
    CLI layer that calls this facade, to avoid catalog schema churn -- see
    sprintctl item #1247.
    """

    arguments = {
        "sprint_id": sprint_id,
        "work_item_id": work_item_id,
        "after_offset": after_offset,
        "limit": limit,
    }
    return asyncio.run(
        _invoke_operation(served_profile, "work.read.events", arguments, repo_id=repo_id)
    )


def project_next_work(
    served_profile: ServedProfile, *, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.project.next-work`` (``sprintctl next-work --json --project X``)."""

    arguments = {"sprint_id": sprint_id}
    return asyncio.run(
        _invoke_operation(served_profile, "work.project.next-work", arguments)
    )


def cutover_evidence(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    parity: dict[str, Any] | None = None,
    max_watermark_age_seconds: int = 300,
    rehearse: bool = True,
) -> dict[str, Any]:
    """Invoke ``work.pilot.cutover-evidence`` (``sprintctl pilot cutover-evidence``).

    ``parity`` must already be computed by the caller (mirroring
    ``cutover.build_cutover_evidence``'s own contract, which never fetches
    parity itself). This function itself still never fetches parity --
    ``work.read.events`` (added by #1247, see :func:`read_events`) exposes the
    sprint-wide event log a caller would need to compute it, but no caller of
    ``cutover_evidence`` has been updated to use it yet, so a served caller
    wanting full parity should still pass ``None`` (the ``--skip-parity``
    path, or whenever the pilot is disabled) unless/until that wiring lands.
    See ``sprintctl.cli._served_cutover_evidence`` for the CLI-side guard
    that enforces this today.
    """

    arguments = {
        "parity": parity,
        "max_watermark_age_seconds": max_watermark_age_seconds,
        "rehearse": rehearse,
    }
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.pilot.cutover-evidence",
            arguments,
            repo_id=repo_id,
        )
    )


def batch_apply(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    records: list[dict[str, Any]],
    idempotency_key: str,
    transient_credentials: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke ``work.batch.apply`` (``sprintctl authority sync``).

    This is the entire served sync mechanism: a mixed batch of OBSERVATION
    and AUTHORITY_COMMAND records self-routes server-side by
    ``record_class`` (``WorkApplication.apply_records``,
    application.py:612-644) -- consecutive observations are ingested
    together and each authority command is arbitrated individually, all
    against one shared ``transient_credentials`` map for the whole batch
    (omitted entirely when empty, since an observation-only batch needs no
    credential material at all). ``idempotency_key`` must equal
    ``application.batch_idempotency_key(records)`` computed over the exact
    same records in the exact same order the server will see. See
    ``sprintctl.cli._served_authority_sync`` for the chunking,
    credential-resolution, and sidecar-cleanup this wraps -- and for why
    ``capability-receipt.accept`` records are never included here (excluded
    from the server's ``SUPPORTED_BATCH_TYPES``, application.py:29-42).
    """

    arguments = {"records": records}
    kwargs: dict[str, Any] = {"idempotency_key": idempotency_key, "repo_id": repo_id}
    if transient_credentials:
        kwargs["transient_credentials"] = transient_credentials
    return asyncio.run(
        _invoke_operation(served_profile, "work.batch.apply", arguments, **kwargs)
    )


def claim_start(
    served_profile: ServedProfile,
    *,
    repo_id: str,
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
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.claim.start", arguments, repo_id=repo_id
        )
    )


def item_note(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    item_id: int,
    note_type: str,
    summary: str,
    detail: str | None = None,
    tags: list[str] | None = None,
    evidence_item_id: int | None = None,
    evidence_event_id: int | None = None,
    git_branch: str | None = None,
    git_sha: str | None = None,
    git_worktree: str | None = None,
) -> dict[str, Any]:
    """Invoke ``work.item.note`` (``sprintctl item note``).

    The recording actor is always the authenticated identity the server
    resolves from the credential, not a caller-supplied argument -- same
    rule as :func:`claim_start`.
    """

    arguments = {
        "item_id": item_id,
        "note_type": note_type,
        "summary": summary,
        "detail": detail,
        "tags": tags,
        "evidence_item_id": evidence_item_id,
        "evidence_event_id": evidence_event_id,
        "git_branch": git_branch,
        "git_sha": git_sha,
        "git_worktree": git_worktree,
    }
    return asyncio.run(
        _invoke_operation(served_profile, "work.item.note", arguments, repo_id=repo_id)
    )


def claim_context(
    served_profile: ServedProfile, *, repo_id: str, claim_id: int
) -> dict[str, Any]:
    """Invoke ``work.claim.context`` (authenticated-actor/authority-uuid/claim-
    snapshot/claim-revision read backing served ``claim heartbeat``/``claim
    release``/``claim handoff``).

    A plain v1 read: no ``transient_credentials``, no idempotency key, no
    basis revision -- this never mutates anything, so there is nothing to
    retry-guard. See the "Approved authority-context contract" section of
    ``docs/plans/agentops/vuoro-claim-proof-transport-clarification-2026-07-23.md``
    for the exact non-secret result shape this returns.
    """

    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.claim.context",
            {"claim_id": claim_id},
            repo_id=repo_id,
        )
    )


def claim_arbitrate(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    record: dict[str, Any],
    transient_credentials: dict[str, str],
) -> dict[str, Any]:
    """Invoke ``work.claim.arbitrate`` (served ``claim heartbeat``/``claim
    release``/``claim handoff``) with the claim proof carried over the
    ``invocation/v2`` transient-credential channel, not as a catalog
    argument.

    Per the approved transport contract, ``transient_credentials`` is a
    transport-level facility outside the operation's ``arguments`` --
    forwarded here to ``_invoke_operation``'s ``**kwargs``, which passes it
    straight through to ``client.invoke(...)``. As with
    :func:`lifecycle_arbitrate`, the idempotency key and basis revision must
    equal the record's own ``event_id``/``basis_revision``.
    """

    arguments = {"record": record}
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.claim.arbitrate",
            arguments,
            idempotency_key=record["event_id"],
            basis_revision=record["basis_revision"],
            repo_id=repo_id,
            transient_credentials=transient_credentials,
        )
    )


def lifecycle_arbitrate(
    served_profile: ServedProfile, *, repo_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    """Invoke ``work.lifecycle.arbitrate`` (``sprintctl item status`` /
    ``sprintctl sprint status``, for the ``item.transition``, ``item.done``,
    ``sprint.activate`` and ``sprint.close`` record types only -- claim
    arbitration is a separate, not-yet-wired operation).

    Per the "Authority and retry semantics" section of
    ``docs/reference/vuoro-work-adapter.md``, a single-command invocation's
    idempotency key and basis revision must equal the canonical command
    record's ``event_id`` and ``basis_revision``; this sends both alongside
    the record so the served application can enforce that match. ``record``
    is the exact JSON shape described by ``_RECORD_DEFINITION`` in
    :mod:`sprintctl.vuoro_adapter` -- a durable outbox-appended envelope, not
    a value this facade fabricates itself.
    """

    arguments = {"record": record}
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.lifecycle.arbitrate",
            arguments,
            idempotency_key=record["event_id"],
            basis_revision=record["basis_revision"],
            repo_id=repo_id,
        )
    )


# The subset of served_routes.py's allowlist that doctor's served probe
# checks for -- the exact catalog operations #1195 (and its #1247 completion
# gap) wire through this facade (next-work contributes two:
# work.read.next-work and work.project.next-work; item.status and
# sprint.status share one operation, work.lifecycle.arbitrate;
# claim.heartbeat, claim.handoff, and claim.release share one operation,
# work.claim.arbitrate). Excludes event.observation.add: it is a registered
# route in served_routes.py, but no served CLI path invokes work.evidence.ingest
# directly -- `event observation add` always appends to the local outbox and
# is only ever flushed through authority.sync's work.batch.apply (see
# _served_authority_sync in cli.py), so it stays out of this probe list.
#
# Every operation added to the served catalog must be added here in the same
# change -- the #1195 postmortem found this list had already silently drifted
# out of sync with newly-wired routes once (missing claim.handoff, then
# pilot.cutover-evidence), meaning `doctor` was not actually verifying the
# catalog before commands ran. See docs/plans/served-mode-gaps-plan.md.
_DOCTOR_PROBE_COMMAND_PATHS = (
    "sprint.list",
    "item.show",
    "next-work",
    "claim.start",
    "item.status",
    "sprint.status",
    "claim.heartbeat",
    "claim.handoff",
    "claim.release",
    "item.note",
    "pilot.cutover-evidence",
    "authority.sync",
    "event.list",
)

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
    "batch_apply",
    "catalog_operation_names",
    "claim_arbitrate",
    "claim_context",
    "claim_start",
    "cutover_evidence",
    "lifecycle_arbitrate",
    "project_next_work",
    "read_events",
    "read_item",
    "read_next_work",
    "read_sprints",
]
