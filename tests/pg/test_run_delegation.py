"""PostgreSQL conformance for owner-held run delegations (schema 27)."""
from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from sprintctl import agent_inbox, delegation, delegation_pg, pg, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.pg_testing import freeze_inbox_clock
from tests.pg._shared import PG_MARKS
from tests.pg.test_run_evidence import _context, _register
from tests.agent_inbox_schemas import valid

pytestmark = PG_MARKS


def _key() -> str:
    return "delegation-" + uuid.uuid4().hex[:16]


def _instance() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    raw = uuid.uuid4().int
    return "ins_" + "".join(alphabet[(raw >> (5 * i)) & 31] for i in range(26))


def _declaration() -> dict:
    return valid("declaration", {
        "schema": "agent-declaration/v1",
        "harness": {"name": "test", "version": "1"},
        "adapter": None,
        "host_class": "workstation",
        "interactivity": "interactive",
        "accepted_kinds": ["continue"],
        "channels": [{"channel": "mcp", "wake_modes": ["read-while-running"]}],
    })


def _world(store):
    store.remote_schema_version = pg_migrations.CURRENT_SCHEMA_VERSION
    app = WorkApplication.postgres(store)
    ctx = _context()
    run = _register(app, idempotency_key=_key(), context=ctx)["run"]["run_id"]
    instance = _instance()
    app.invoke("work.agent.register-v1", {
        "instance_id": instance, "declaration": _declaration(), "idempotency_key": _key(),
    }, ctx)
    return app, ctx, run, instance


def _mint(app, ctx, run, instance, *, key=None, ttl=300):
    return app.invoke(delegation.OP_DELEGATE, {
        "run_id": run, "creator_instance_id": instance,
        "operations": ["work.run.resolve-v1", "work.session-note.write-v1"],
        "ttl_seconds": ttl, "idempotency_key": key or _key(),
    }, ctx)


def _rejection(call, code):
    with pytest.raises(ApplicationRejection) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_mint_is_digest_only_single_live_and_idempotent_without_revealing_replay(store):
    app, ctx, run, instance = _world(store)
    key = _key()
    created = _mint(app, ctx, run, instance, key=key)
    assert created["token"].startswith("vrd_") and created["replayed"] is False
    replay = _mint(app, ctx, run, instance, key=key)
    assert replay["delegation_id"] == created["delegation_id"]
    assert replay["token"] is None and replay["replayed"] is True
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT token_sha256 FROM work_run_delegation WHERE repo_id=%s AND delegation_id=%s",
            (store.repo_id, created["delegation_id"]),
        )
        persisted = cur.fetchone()["token_sha256"]
        cur.execute(
            "SELECT result::text FROM work_idempotency_ledger WHERE repo_id=%s AND principal_id=%s "
            "AND tool='delegate_run' AND idempotency_key=%s",
            (store.repo_id, ctx.identity.principal_id, key),
        )
        ledger = cur.fetchone()["result"]
    store.conn.rollback()
    assert persisted == hashlib.sha256(created["token"].encode("ascii")).hexdigest()
    assert created["token"] not in ledger
    _rejection(lambda: _mint(app, ctx, run, instance), "delegation-exists")


def test_resolver_context_rechecks_run_operation_authority_and_revocation(store):
    app, ctx, run, instance = _world(store)
    created = _mint(app, ctx, run, instance)
    digest = hashlib.sha256(created["token"].encode("ascii")).hexdigest()
    # The shared owner store may be initialized with a seed repo. Resolution
    # searches the owner's digest index globally and returns the record scope.
    resolved = WorkApplication.postgres(store).resolve_run_delegation_digest(digest)
    assert resolved.repo_id == store.repo_id
    assert delegation_pg.resolve_token(store, created["token"]) == resolved
    assert resolved.delegation.run_id == run
    assert resolved.delegation.operations == (
        "work.run.resolve-v1", "work.session-note.write-v1",
    )
    delegated_ctx = SimpleNamespace(**vars(ctx))
    delegated_ctx.identity = SimpleNamespace(**vars(ctx.identity))
    delegated_ctx.identity.delegation = resolved.delegation
    assert app.invoke("work.run.resolve-v1", {"run_id": run}, delegated_ctx)["run_id"] == run
    _rejection(
        lambda: app.invoke("work.run.resolve-v1", {"run_id": "run_" + "A" * 26}, delegated_ctx),
        "delegation-run-mismatch",
    )
    _rejection(
        lambda: app.invoke("work.run.delegate-v1", {}, delegated_ctx),
        "delegation-operation-not-allowed",
    )
    app.invoke(delegation.OP_REVOKE, {
        "delegation_id": created["delegation_id"], "reason": "operator-revoked",
        "idempotency_key": _key(),
    }, ctx)
    with pytest.raises(delegation.DelegationRefused, match="unknown, revoked or expired"):
        delegation_pg.resolve_token(store, created["token"])
    _rejection(
        lambda: app.invoke("work.run.resolve-v1", {"run_id": run}, delegated_ctx),
        "credential-invalid",
    )


def test_expiry_and_parent_authority_bounds_are_owner_enforced(store):
    app, ctx, run, instance = _world(store)
    ctx.identity.expires_at = "2026-10-17T10:01:00Z"
    now = freeze_inbox_clock(store, "2026-10-17T10:00:00Z")
    result = _mint(app, ctx, run, instance, ttl=3600)
    assert result["expires_at"] == "2026-10-17T10:01:00Z"
    assert result["state"] == "live"
    freeze_inbox_clock(store, "2026-10-17T10:01:01Z")
    with pytest.raises(delegation.DelegationRefused, match="unknown, revoked or expired"):
        delegation_pg.resolve_token(store, result["token"])
    got = app.invoke(delegation.OP_GET, {"delegation_id": result["delegation_id"]}, ctx)
    assert got["state"] == "expired"
    assert now is not None
    second_run = _register(app, idempotency_key=_key(), context=ctx)["run"]["run_id"]
    ctx.identity.expires_at = "2026-10-17T09:59:59Z"
    _rejection(lambda: _mint(app, ctx, second_run, instance), "identity-expired")


def test_exact_creator_retirement_and_explicit_result_link_revoke(store):
    app, ctx, run, instance = _world(store)
    retired_credential = _mint(app, ctx, run, instance)
    app.invoke(agent_inbox.OPERATION_RETIRE, {
        "instance_id": instance, "reason": "test retirement", "idempotency_key": _key(),
    }, ctx)
    with pytest.raises(delegation.DelegationRefused):
        delegation_pg.resolve_token(store, retired_credential["token"])

    # Register a second exact creator and settle a run-linked inbox message.
    second_instance = _instance()
    app.invoke(agent_inbox.OPERATION_REGISTER, {
        "instance_id": second_instance, "declaration": _declaration(), "idempotency_key": _key(),
    }, ctx)
    scoped = pg.PgStore(conn=store.conn, repo_id=store.repo_id)
    sprint = pg.create_sprint(scoped, "Delegation test", "Inbox result", "2026-01-01", "2026-12-31", "active")
    track = pg.get_or_create_track(scoped, sprint, "delegation-test")
    item_id = int(pg.create_work_item(scoped, sprint, track, "Delegation receipt item"))
    store.conn.commit()
    message_id = pg._mint_prefixed_id("msg_")
    app.invoke(agent_inbox.OPERATION_SEND, {"message": {
        "schema": "agent-message/v1", "message_id": message_id,
        "recipient": {"principal_id": ctx.identity.principal_id, "instance_id": second_instance},
        "kind": "continue", "mode": "request", "context": {"item_id": item_id},
        "body": {"text": "settle the run"}, "created_at": "2026-10-17T10:00:00Z",
    }}, ctx)
    app.invoke(agent_inbox.OPERATION_RECEIPT, {
        "message_id": message_id, "instance_id": second_instance, "kind": "accepted",
    }, ctx)
    app.invoke(agent_inbox.OPERATION_RECEIPT, {
        "message_id": message_id, "instance_id": second_instance,
        "kind": "run-linked", "run_id": run,
    }, ctx)
    result_credential = _mint(app, ctx, run, second_instance)
    app.invoke(agent_inbox.OPERATION_RECEIPT, {
        "message_id": message_id, "instance_id": second_instance,
        "kind": "result-linked", "outcome": "completed", "evidence_refs": [],
        "summary": "run completed",
    }, ctx)
    with pytest.raises(delegation.DelegationRefused):
        delegation_pg.resolve_token(store, result_credential["token"])


@pytest.mark.parametrize("ttl", [True, False, 0, -1, 14_401, 1.0, "60"])
def test_invalid_ttls_are_refused_before_owner_writes(store, ttl):
    app, ctx, run, instance = _world(store)
    with pytest.raises(ApplicationRejection) as caught:
        app.invoke(delegation.OP_DELEGATE, {
            "run_id": run, "creator_instance_id": instance,
            "operations": ["work.run.resolve-v1"], "ttl_seconds": ttl,
            "idempotency_key": _key(),
        }, ctx)
    assert caught.value.code == "invalid-arguments"
