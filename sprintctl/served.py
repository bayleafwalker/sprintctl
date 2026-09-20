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
import uuid
from typing import Any

from . import reservation as _reservation
from .backend import ServedProfile
from .served_routes import doctor_probe_command_paths, doctor_probe_operations
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
    arguments = _with_session_attribution(operation, arguments)
    async with _client(served_profile) as client:
        return await client.invoke(operation, arguments, **kwargs)


def _with_session_attribution(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Tell the authority which session performed an activity-bearing mutation.

    Served callers are the reason ``last_activity_at`` can advance without
    ceremony: the server cannot see the client's session, so the client has to
    say. This is attached here rather than in each facade so a newly served
    operation cannot quietly lose the attribution -- the operation set lives in
    :mod:`sprintctl.reservation`, shared with the application that consumes it.

    It names a session and authorizes nothing; an explicit argument always
    wins, and a client with no session simply omits it.
    """
    if operation not in _reservation.ACTIVITY_OPERATIONS or arguments.get("session_id"):
        return arguments
    session_id = _reservation.ambient_session_id()
    if session_id is None:
        return arguments
    return {**arguments, "session_id": session_id}


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


def identity_current(
    served_profile: ServedProfile, *, repo_id: str
) -> dict[str, Any]:
    """Return the authenticated work actor used by durable command records."""
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.identity.current", {}, repo_id=repo_id
        )
    )


def sprint_create(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    name: str,
    goal: str,
    start_date: str | None,
    end_date: str | None,
    status: str,
    kind: str,
) -> dict[str, Any]:
    """Create a sprint through the separately authorized served operation."""
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.sprint.create",
            {
                "name": name,
                "goal": goal,
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "kind": kind,
            },
            repo_id=repo_id,
        )
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


def decision_record(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    item_id: int,
    kind: str,
    rationale: str,
    evidence_digests: list[str],
    release_digest: str | None = None,
    superseded_by_item_id: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Invoke ``work.decision.record`` (``sprintctl item decide``).

    The decision actor is the authenticated identity; no actor is sent.  The
    operation is keyed: a caller retrying one logical decision passes the same
    key and gets the first attempt's decision back.
    """
    arguments: dict[str, Any] = {
        "item_id": item_id,
        "kind": kind,
        "rationale": rationale,
        "evidence_digests": list(evidence_digests),
        "release_digest": release_digest,
        "superseded_by_item_id": superseded_by_item_id,
    }
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.decision.record",
            arguments,
            repo_id=repo_id,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
        )
    )


def read_item_decisions(
    served_profile: ServedProfile, *, repo_id: str, item_id: int
) -> dict[str, Any]:
    """Invoke ``work.read.item-decisions`` (terminal decision in ``item show``)."""

    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.item-decisions", {"item_id": item_id}, repo_id=repo_id
        )
    )


def read_unbound(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    sprint_id: int | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Invoke ``work.read.unbound`` (``sprintctl item unbound``)."""

    arguments = {"sprint_id": sprint_id, "category": category, "limit": limit}
    return asyncio.run(
        _invoke_operation(served_profile, "work.read.unbound", arguments, repo_id=repo_id)
    )


def read_release(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    release_digest: str | None = None,
    item_id: int | None = None,
) -> dict[str, Any]:
    """Invoke ``work.read.release`` by digest, or for an item's current release."""

    arguments: dict[str, Any] = (
        {"release_digest": release_digest} if release_digest is not None else {"item_id": item_id}
    )
    return asyncio.run(
        _invoke_operation(served_profile, "work.read.release", arguments, repo_id=repo_id)
    )


def read_item_projection(
    served_profile: ServedProfile, *, repo_id: str, item_id: int
) -> dict[str, Any]:
    """Read one bounded, revision-bearing item projection."""

    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.read.item-projection",
            {"item_id": item_id},
            repo_id=repo_id,
        )
    )


def validate_item_status_mutation(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    item_id: int,
    expected_revision: str | None,
) -> dict[str, Any]:
    """Run the read-only early-feedback check for a status mutation."""

    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.validate.item-status-mutation",
            {"item_id": item_id, "expected_revision": expected_revision},
            repo_id=repo_id,
        )
    )


def read_items(served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None,
               track_name: str | None = None, status: str | None = None) -> dict[str, Any]:
    return asyncio.run(_invoke_operation(served_profile, "work.read.items", {
        "sprint_id": sprint_id, "track_name": track_name, "status": status,
    }, repo_id=repo_id))


def read_context(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke the atomic ContextContract v1 aggregate for ``usage --context``."""
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.context", {"sprint_id": sprint_id}, repo_id=repo_id
        )
    )


def context_candidates(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    sprint_id: int | None,
    item_id: int | None,
    target_paths: list[str],
    query: str | None,
    limit: int,
) -> dict[str, Any]:
    """Read the authoritative Tier-1 dispatch packet without opening a store."""
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.read.context-candidates",
            {
                "sprint_id": sprint_id,
                "item_id": item_id,
                "target_paths": target_paths,
                "query": query,
                "limit": limit,
            },
            repo_id=repo_id,
        )
    )


def read_handoff(served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None, events_limit: int, git_context: dict | None) -> dict[str, Any]:
    """Fetch one server-built handoff bundle; client git state is explicit."""
    return asyncio.run(_invoke_operation(served_profile, "work.read.handoff", {"sprint_id": sprint_id, "events_limit": events_limit, "git_context": git_context}, repo_id=repo_id))


def handoff_record(served_profile: ServedProfile, *, repo_id: str, sprint_id: int, bundle: dict[str, Any]) -> dict[str, Any]:
    """Record a generated bundle after the client has successfully emitted it."""
    return asyncio.run(_invoke_operation(served_profile, "work.handoff.record", {"sprint_id": sprint_id, "bundle": bundle}, repo_id=repo_id))


def item_ref_add(served_profile: ServedProfile, *, repo_id: str, item_id: int, ref_type: str, url: str, label: str = "") -> dict[str, Any]:
    return asyncio.run(_invoke_operation(served_profile, "work.item.ref.add", {"item_id": item_id, "ref_type": ref_type, "url": url, "label": label}, repo_id=repo_id))


def item_ref_remove(served_profile: ServedProfile, *, repo_id: str, item_id: int, ref_id: int) -> dict[str, Any]:
    return asyncio.run(_invoke_operation(served_profile, "work.item.ref.remove", {"item_id": item_id, "ref_id": ref_id}, repo_id=repo_id))


def item_dep_add(served_profile: ServedProfile, *, repo_id: str, item_id: int, blocked_item_id: int) -> dict[str, Any]:
    return asyncio.run(_invoke_operation(served_profile, "work.item.dep.add", {"item_id": item_id, "blocked_item_id": blocked_item_id}, repo_id=repo_id))


def item_dep_remove(served_profile: ServedProfile, *, repo_id: str, item_id: int, dep_id: int) -> dict[str, Any]:
    return asyncio.run(_invoke_operation(served_profile, "work.item.dep.remove", {"item_id": item_id, "dep_id": dep_id}, repo_id=repo_id))


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


def reservation_operation(
    served_profile: ServedProfile,
    operation: str,
    arguments: dict[str, Any],
    *,
    repo_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Invoke one v0.3 reservation operation through the served authority.

    Every ``work.reservation.*`` operation is declared ``idempotency: required``
    by the adapter, so the authority rejects a call without a key
    (``idempotency-key-required``) *after* the authority check has passed --
    which is how a correctly granted identity was first seen failing here. A
    reservation is a coordination signal, and ``touch`` in particular must be
    able to advance ``last_activity_at`` on every call, so the default key is
    unique per invocation (the same convention as the per-event key used by
    ``publish_events``) rather than derived from the arguments. Callers that
    retry a single logical call pass their own key to deduplicate. Reads
    (``work.read.reservation``) are never keyed.
    """
    kwargs: dict[str, Any] = {"repo_id": repo_id}
    if operation.startswith("work.reservation."):
        # Only the mutations are keyed: ``work.read.reservation`` travels
        # through here too and the authority rejects a key on a read.
        kwargs["idempotency_key"] = idempotency_key or uuid.uuid4().hex
    return asyncio.run(_invoke_operation(served_profile, operation, arguments, **kwargs))


def read_records(
    served_profile: ServedProfile, *, repo_id: str, after_offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read the authoritative served ledger for local recovery/audit."""
    return asyncio.run(_invoke_operation(
        served_profile, "work.read.records",
        {"after_offset": after_offset, "limit": limit}, repo_id=repo_id,
    ))


def read_decisions(
    served_profile: ServedProfile, *, repo_id: str, after_offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read served authority decisions for local recovery/audit."""
    return asyncio.run(_invoke_operation(
        served_profile, "work.read.decisions",
        {"after_offset": after_offset, "limit": limit}, repo_id=repo_id,
    ))


def read_next_work_explain(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke the atomic served aggregate for ``next-work --explain``."""
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.next-work-explain", {"sprint_id": sprint_id}, repo_id=repo_id
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


def read_sprint(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.read.sprint`` (plain ``sprintctl sprint show``)."""
    return asyncio.run(
        _invoke_operation(served_profile, "work.read.sprint", {"sprint_id": sprint_id}, repo_id=repo_id)
    )


def read_sprint_detail(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke the atomic server-built ``sprint show --detail`` aggregate."""
    return asyncio.run(
        _invoke_operation(
            served_profile, "work.read.sprint-detail", {"sprint_id": sprint_id}, repo_id=repo_id
        )
    )


def event_add(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int, event_type: str,
    work_item_id: int | None = None, source_type: str = "actor", payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke direct ``work.event.add``; the server chooses the actor."""
    return asyncio.run(_invoke_operation(served_profile, "work.event.add", {
        "sprint_id": sprint_id, "event_type": event_type, "work_item_id": work_item_id,
        "source_type": source_type, "payload": payload,
    }, repo_id=repo_id))


def item_create(
    served_profile: ServedProfile, *, repo_id: str, sprint_id: int, track_name: str, title: str,
    description: str | None = None, assignee: str | None = None, priority: int | None = None,
) -> dict[str, Any]:
    """Invoke direct ``work.item.create``; track resolution is server-side."""
    return asyncio.run(_invoke_operation(served_profile, "work.item.create", {
        "sprint_id": sprint_id, "track_name": track_name, "title": title,
        "description": description, "assignee": assignee, "priority": priority,
    }, repo_id=repo_id))


def item_edit(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    item_id: int,
    description: str,
    expected_revision: str,
) -> dict[str, Any]:
    """Invoke CAS-protected ``work.item.edit`` as the authenticated actor."""
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.item.edit",
            {
                "item_id": item_id,
                "description": description,
                "expected_revision": expected_revision,
            },
            repo_id=repo_id,
        )
    )


def project_next_work(
    served_profile: ServedProfile, *, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke ``work.project.next-work`` (``sprintctl next-work --json --project X``)."""

    arguments = {"sprint_id": sprint_id}
    return asyncio.run(
        _invoke_operation(served_profile, "work.project.next-work", arguments)
    )


def project_items(
    served_profile: ServedProfile,
    *,
    sprint_id: int | None = None,
    track_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List items across the canonical, server-authorized project binding."""

    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.project.items",
            {
                "sprint_id": sprint_id,
                "track_name": track_name,
                "status": status,
            },
        )
    )


def project_context(
    served_profile: ServedProfile, *, sprint_id: int | None = None
) -> dict[str, Any]:
    """Invoke the server-authorized project ContextContract aggregate."""

    return asyncio.run(
        _invoke_operation(served_profile, "work.project.context", {"sprint_id": sprint_id})
    )


def project_sprints(
    served_profile: ServedProfile,
    *,
    include_backlog: bool = False,
    include_archive: bool = False,
    active_only: bool = False,
) -> dict[str, Any]:
    """List server-authorized project member sprints in canonical member order."""

    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.project.sprints",
            {
                "include_backlog": include_backlog,
                "include_archive": include_archive,
                "active_only": active_only,
            },
        )
    )


def batch_apply(
    served_profile: ServedProfile,
    *,
    repo_id: str,
    records: list[dict[str, Any]],
    idempotency_key: str,
) -> dict[str, Any]:
    """Invoke ``work.batch.apply`` (``sprintctl authority sync``).

    This is the entire served sync mechanism: a mixed batch of OBSERVATION
    and AUTHORITY_COMMAND records self-routes server-side by
    ``record_class`` (``WorkApplication.apply_records``,
    application.py:612-644) -- consecutive observations are ingested
    together and each authority command is arbitrated individually, all
    individually. ``idempotency_key`` must equal
    ``application.batch_idempotency_key(records)`` computed over the exact
    same records in the exact same order the server will see. See
    ``sprintctl.cli._served_authority_sync`` for the chunking this
    wraps -- and for why authority commands outside the server's
    ``SUPPORTED_BATCH_TYPES`` (application_common.py) are never included
    here.
    """

    arguments = {"records": records}
    return asyncio.run(
        _invoke_operation(
            served_profile,
            "work.batch.apply",
            arguments,
            idempotency_key=idempotency_key,
            repo_id=repo_id,
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
    release_digest: str | None = None,
    worktree_host: str | None = None,
    predecessor_session: str | None = None,
    acked_by: str | None = None,
) -> dict[str, Any]:
    """Invoke ``work.item.note`` (``sprintctl item note``).

    The recording actor is always the authenticated identity the server
    resolves from the credential, not a caller-supplied argument.

    ``release_digest``, ``worktree_host``, ``predecessor_session`` and
    ``acked_by`` are the S6 ledger-checkpoint fields (agentops #2450,
    docs/plans/2450-s6-ledger-checkpoint.md); they ride through as ordinary
    optional payload keys the same way ``git_branch``/``git_sha``/
    ``git_worktree`` already do, and are only meaningful for
    ``lane.checkpoint`` notes.
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
        "release_digest": release_digest,
        "worktree_host": worktree_host,
        "predecessor_session": predecessor_session,
        "acked_by": acked_by,
    }
    return asyncio.run(
        _invoke_operation(served_profile, "work.item.note", arguments, repo_id=repo_id)
    )


def lifecycle_arbitrate(
    served_profile: ServedProfile, *, repo_id: str, record: dict[str, Any],
) -> dict[str, Any]:
    """Invoke ``work.lifecycle.arbitrate`` (``sprintctl item status`` /
    ``sprintctl sprint status``, for the ``item.transition``, ``item.done``,
    ``sprint.activate`` and ``sprint.close`` record types only).

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
# gap) wire through this facade (next-work contributes three:
# work.read.next-work, work.read.next-work-explain, and work.project.next-work; item.status and
# sprint.status share one operation, work.lifecycle.arbitrate;
# Excludes event.observation.add: it is a registered
# route in served_routes.py, but no served CLI path invokes work.evidence.ingest
# directly -- `event observation add` always appends to the local outbox and
# is only ever flushed through authority.sync's work.batch.apply (see
# _served_authority_sync in cli.py), so it stays out of this probe list.
#
# Every operation added to the served catalog must be added here in the same
# change -- the #1195 postmortem found this list had already silently drifted
# out of sync with newly-wired routes once (it was missing the then-live
# pilot cutover-evidence route, since retired), meaning `doctor` was not
# actually verifying the catalog before commands ran. See
# docs/plans/served-mode-gaps-plan.md.
EXPECTED_OPERATIONS = doctor_probe_operations()
# Compatibility for consumers that diagnosed the precise route keys. The
# tuple itself remains owned by the route registry.
_DOCTOR_PROBE_COMMAND_PATHS = doctor_probe_command_paths()


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


def batch_record_types_from_catalog(catalog: Any) -> frozenset[str] | None:
    """The record types a catalog's ``work.batch.apply`` advertises.

    The server publishes its accepted batch record types as the ``enum`` of
    the record ``event_type`` in that operation's input schema.  ``None``
    means the catalog does not say (a server that predates the
    advertisement); callers treat that as "only the types every server has
    always accepted", never as "anything goes".
    """
    if not isinstance(catalog, dict):
        return None
    for operation in catalog.get("operations", ()):
        if not isinstance(operation, dict) or operation.get("name") != "work.batch.apply":
            continue
        try:
            enum = operation["input_schema"]["$defs"]["record"]["properties"]["event_type"]["enum"]
        except (KeyError, TypeError):
            return None
        if not isinstance(enum, list):
            return None
        return frozenset(value for value in enum if isinstance(value, str))
    return None


async def _batch_record_types(served_profile: ServedProfile) -> frozenset[str] | None:
    async with _client(served_profile) as client:
        catalog = await client.catalog()
    return batch_record_types_from_catalog(catalog)


def batch_record_types(served_profile: ServedProfile) -> frozenset[str] | None:
    """Record types the server's ``work.batch.apply`` accepts, from the
    unauthenticated catalog (``None`` when the server does not advertise
    them). One ``asyncio.run(...)`` with a fresh client, like every function
    above."""

    return asyncio.run(_batch_record_types(served_profile))


__all__ = [
    "EXPECTED_OPERATIONS",
    "batch_apply",
    "batch_record_types",
    "batch_record_types_from_catalog",
    "catalog_operation_names",
    "context_candidates",
    "handoff_record",
    "event_add",
    "item_create",
    "item_dep_add",
    "item_dep_remove",
    "item_ref_add",
    "item_ref_remove",
    "lifecycle_arbitrate",
    "project_next_work",
    "project_items",
    "project_context",
    "project_sprints",
    "read_events",
    "read_item",
    "read_items",
    "identity_current",
    "read_context",
    "read_handoff",
    "read_next_work",
    "read_sprint",
    "read_sprints",
    "sprint_create",
]
