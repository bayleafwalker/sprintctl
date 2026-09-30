"""Oracle (agentops#2542, H1-9 / FU-4): the shared idempotency ledger protocol
against real PostgreSQL.

R4 lease semantics 6 on agentops#253 (normative): the ledger is keyed by
``(workspace_id, principal_id, tool, key)`` and answers a typed
``begin(workspace_id, principal_id, tool, key, request_digest) -> LedgerEntry``.
The same tuple with the same digest is the same logical operation and
result; the same tuple with another digest is ``IDEMPOTENCY_KEY_REUSED``
(published as ``idempotency-conflict``, 409).  One behaviour suite covers run
registration, claim acquisition, outcome report, effect proposal and, where
appropriate, effect acceptance.

What sprintctl's ledger is held to here (``sprintctl.pg``):

``PgIdempotencyLedger(store).begin(...)`` runs in ``store.conn``'s current
transaction and neither commits nor rolls it back.

* No committed entry: the key is claimed for this transaction and the entry
  is ``replayed=False`` with ``result=None`` and the caller's digest.  A
  concurrent ``begin`` of the same tuple on another connection waits until
  that transaction ends: after a rollback it gets the key itself
  (``replayed=False``), after ``complete`` + commit it replays.
* ``complete(entry, result)`` records the result in the same transaction and
  returns the entry with that result.  Nothing is committed without its
  result: rolling back leaves the key free.
* A committed entry with the same digest: ``replayed=True`` and the stored
  result, equal (by value, never identity) on every read.
* A committed entry with another digest: ``IdempotencyConflict`` whose
  ``code`` is ``IDEMPOTENCY_KEY_REUSED``; the entry is unchanged.
* Workspace, principal and tool each scope the key.
* ``idempotent_write`` (the effect-in-one-transaction path the write tools
  use) and ``begin`` are one ledger: each replays what the other committed.

The behaviour suite drives each keyed write tool through
``WorkApplication.invoke`` (like tests/pg/test_work_lease.py) and then reads
its committed entry back through ``begin``.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    _uid,
    assert_disposable_connection,
    dict_row,
    psycopg,
)

pytestmark = PG_MARKS

_CONTRACTS = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}
_WORKER_AUTHORITIES = frozenset({
    "work:read", "work:claim", "work:evidence",
    "work.effect.propose", "work.effect.get", "work.effect.list-proposed",
})
_ACCEPTOR_AUTHORITIES = frozenset({
    "work.effect.accept", "work.effect.reject", "work.effect.mark-applied",
    "work.effect.get", "work.effect.list-proposed",
})

D1 = "1" * 64
D2 = "2" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sibling(store) -> pg.PgStore:
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return pg.PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)


def _ws() -> str:
    return f"ws-{uuid.uuid4().hex[:12]}"


def _key(prefix: str = "ledger") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _tuple(**overrides) -> dict:
    values = {
        "workspace_id": _ws(), "principal_id": "github:1:0", "tool": "register_run",
        "key": _key(),
    }
    values.update(overrides)
    return values


def _begin(store, key_tuple: Mapping[str, str], digest: str) -> "pg.LedgerEntry":
    return pg.PgIdempotencyLedger(store).begin(
        workspace_id=key_tuple["workspace_id"], principal_id=key_tuple["principal_id"],
        tool=key_tuple["tool"], key=key_tuple["key"], request_digest=digest,
    )


def _peek(store, key_tuple: Mapping[str, str], digest: str) -> "pg.LedgerEntry":
    """``begin`` in a transaction that is always rolled back."""
    try:
        return _begin(store, key_tuple, digest)
    finally:
        store.conn.rollback()


def _commit_entry(store, key_tuple: Mapping[str, str], digest: str, result: dict) -> "pg.LedgerEntry":
    entry = _begin(store, key_tuple, digest)
    assert entry.replayed is False
    completed = pg.PgIdempotencyLedger(store).complete(entry, result)
    store.conn.commit()
    return completed


def _conflict(call) -> "pg.IdempotencyConflict":
    with pytest.raises(pg.IdempotencyConflict) as excinfo:
        call()
    return excinfo.value


def _count(store, sql: str, params: tuple) -> int:
    with store.conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    store.conn.rollback()
    return int(next(iter(row.values())))


def _wait_until_lock_blocked(observer, pid: int, done: threading.Event) -> bool:
    """True once backend ``pid`` waits on a lock; False if it finished first."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not done.is_set():
        with observer.cursor() as cur:
            cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
            row = cur.fetchone()
        observer.rollback()
        if row is not None and row["wait_event_type"] == "Lock":
            return True
        time.sleep(0.02)
    return False


def _held_begin_race(store, key_tuple, *, finish: Callable[[pg.PgStore, Any], None]):
    """Hold a fresh ``begin`` open on one connection, start a second ``begin``
    of the same tuple on another, observe that the second is blocked, then let
    the first ``finish`` (commit or roll back).  Returns (blocked, second)."""
    first, second = _sibling(store), _sibling(store)
    observer = psycopg.connect(_PG_URL, row_factory=dict_row)
    outcome: dict = {}
    done = threading.Event()

    def run_second():
        try:
            outcome["entry"] = _begin(second, key_tuple, D1)
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome["entry"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run_second, name="ledger-second")
    try:
        held = _begin(first, key_tuple, D1)
        assert held.replayed is False
        thread.start()
        blocked = _wait_until_lock_blocked(observer, second.conn.info.backend_pid, done)
        finish(first, held)
        thread.join(timeout=30)
        assert not thread.is_alive()
        second.conn.rollback()
    finally:
        if thread.is_alive():
            first.conn.rollback()
            thread.join(timeout=30)
        observer.close()
        first.conn.close()
        second.conn.close()
    return blocked, outcome["entry"]


# ---------------------------------------------------------------------------
# The typed begin protocol
# ---------------------------------------------------------------------------


class TestBegin:
    def test_an_unknown_key_begins_fresh_with_no_result(self, store):
        key_tuple = _tuple()
        entry = _peek(store, key_tuple, D1)
        assert isinstance(entry, pg.LedgerEntry)
        assert entry.replayed is False
        assert entry.result is None
        assert entry.request_digest == D1
        assert (entry.workspace_id, entry.principal_id, entry.tool, entry.key) == (
            key_tuple["workspace_id"], key_tuple["principal_id"], key_tuple["tool"], key_tuple["key"],
        )

    def test_a_completed_and_committed_entry_replays_its_result(self, store):
        key_tuple = _tuple()
        completed = _commit_entry(store, key_tuple, D1, {"run_id": "run-1", "n": [1, 2]})
        assert completed.replayed is False
        assert completed.result == {"run_id": "run-1", "n": [1, 2]}
        replay = _peek(store, key_tuple, D1)
        assert replay.replayed is True
        assert replay.request_digest == D1
        assert replay.result == {"run_id": "run-1", "n": [1, 2]}

    def test_replays_are_equal_by_value_on_every_read(self, store):
        """A durable ledger hands back a new object per read: equality, not
        identity, is the contract (the #2530 identity-vs-equality nit)."""
        key_tuple = _tuple()
        _commit_entry(store, key_tuple, D1, {"value": "same"})
        first = _peek(store, key_tuple, D1)
        second = _peek(store, key_tuple, D1)
        assert first == second
        assert first.result == second.result == {"value": "same"}

    def test_another_digest_is_idempotency_key_reused_and_changes_nothing(self, store):
        key_tuple = _tuple()
        _commit_entry(store, key_tuple, D1, {"value": "first"})
        refused = _conflict(lambda: _peek(store, key_tuple, D2))
        assert refused.code == pg.IDEMPOTENCY_KEY_REUSED == "idempotency-conflict"
        replay = _peek(store, key_tuple, D1)
        assert replay.replayed is True
        assert replay.request_digest == D1 and replay.result == {"value": "first"}

    def test_a_rolled_back_begin_commits_nothing(self, store):
        key_tuple = _tuple()
        entry = _begin(store, key_tuple, D1)
        pg.PgIdempotencyLedger(store).complete(entry, {"value": "never"})
        store.conn.rollback()
        # The key is free again: another digest is a fresh begin, not a conflict.
        again = _peek(store, key_tuple, D2)
        assert again.replayed is False and again.result is None

    def test_begin_neither_commits_nor_rolls_back(self, store):
        key_tuple = _tuple()
        _begin(store, key_tuple, D1)
        try:
            assert store.conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
        finally:
            store.conn.rollback()
        # The fresh claim was never committed.
        assert _peek(store, key_tuple, D2).replayed is False

    @pytest.mark.parametrize("part", ["workspace_id", "principal_id", "tool"])
    def test_workspace_principal_and_tool_each_scope_the_key(self, store, part):
        key_tuple = _tuple()
        _commit_entry(store, key_tuple, D1, {"owner": "first"})
        other = dict(key_tuple, **{part: key_tuple[part] + "-other"})
        entry = _peek(store, other, D1)
        assert entry.replayed is False and entry.result is None
        # Another digest under the other scope is no conflict either.
        assert _peek(store, other, D2).replayed is False

    def test_parts_that_would_collide_if_joined_stay_distinct(self, store):
        base = _ws()
        key = _key()
        joined_a = {"workspace_id": f"{base}:a", "principal_id": "b", "tool": "register_run", "key": key}
        joined_b = {"workspace_id": base, "principal_id": "a:b", "tool": "register_run", "key": key}
        _commit_entry(store, joined_a, D1, {"owner": "a"})
        assert _peek(store, joined_b, D2).replayed is False
        assert _peek(store, joined_b, D1).replayed is False

    def test_a_concurrent_begin_waits_and_then_replays_the_committed_result(self, store):
        key_tuple = _tuple()

        def commit(first, held):
            pg.PgIdempotencyLedger(first).complete(held, {"winner": "first"})
            first.conn.commit()

        blocked, second = _held_begin_race(store, key_tuple, finish=commit)
        assert blocked, "a same-tuple begin must wait for the open claim"
        assert isinstance(second, pg.LedgerEntry), repr(second)
        assert second.replayed is True
        assert second.result == {"winner": "first"}

    def test_a_concurrent_begin_gets_the_key_when_the_holder_rolls_back(self, store):
        key_tuple = _tuple()
        blocked, second = _held_begin_race(
            store, key_tuple, finish=lambda first, _held: first.conn.rollback(),
        )
        assert blocked, "a same-tuple begin must wait for the open claim"
        assert isinstance(second, pg.LedgerEntry), repr(second)
        assert second.replayed is False and second.result is None


class TestOneLedgerWithIdempotentWrite:
    """``idempotent_write`` and ``begin`` read and write the same ledger."""

    def _write(self, store, key_tuple, digest, result, calls: list):
        def effect(_cur):
            calls.append(digest)
            return result

        return pg.idempotent_write(
            store, workspace_id=key_tuple["workspace_id"], principal_id=key_tuple["principal_id"],
            tool=key_tuple["tool"], key=key_tuple["key"], request_digest=digest, effect=effect,
        )

    def test_begin_replays_what_idempotent_write_committed(self, store):
        key_tuple = _tuple(tool="write_session_note")
        calls: list = []
        written = self._write(store, key_tuple, D1, {"note_id": 7}, calls)
        entry = _peek(store, key_tuple, D1)
        assert entry.replayed is True and entry.result == written == {"note_id": 7}
        assert _conflict(lambda: _peek(store, key_tuple, D2)).code == "idempotency-conflict"

    def test_idempotent_write_replays_what_begin_completed_without_a_second_effect(self, store):
        key_tuple = _tuple(tool="write_session_note")
        _commit_entry(store, key_tuple, D1, {"note_id": 8})
        calls: list = []
        assert self._write(store, key_tuple, D1, {"note_id": 999}, calls) == {"note_id": 8}
        refused = _conflict(lambda: self._write(store, key_tuple, D2, {"note_id": 999}, calls))
        assert refused.code == "idempotency-conflict"
        assert calls == []


# ---------------------------------------------------------------------------
# One behaviour suite over the keyed write tools
# ---------------------------------------------------------------------------


def _context(principal_id: str, authorities=_WORKER_AUTHORITIES, *, workspace_id: str = "ws-1"):
    identity = SimpleNamespace(
        actor=f"actor-{principal_id}", environment="vuoro-dev", authorities=frozenset(authorities),
        principal_id=principal_id, workspace_id=workspace_id, client_id=None, grant_id=None,
    )
    return SimpleNamespace(
        identity=identity, request_id="request-1", basis_revision=None,
        catalog_revision="catalog-1", idempotency_requirement="not-allowed", idempotency_key=None,
    )


def _invoke(store, operation: str, arguments: dict, context) -> dict:
    jsonschema.validate(arguments, _CONTRACTS[operation].input_schema)
    result = WorkApplication.postgres(store).invoke(operation, arguments, context)
    jsonschema.validate(result, _CONTRACTS[operation].result_schema)
    return result


def _refused(call) -> ApplicationRejection:
    with pytest.raises(ApplicationRejection) as excinfo:
        call()
    return excinfo.value


def _run_args(key: str, model_id: str = "m") -> dict:
    return {
        "harness_id": "claude-code", "harness_build": "1.0.0", "model_id": model_id, "recipe_id": "r",
        "observed_profile": {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []},
        "idempotency_key": key,
    }


def _run(store, context) -> str:
    return _invoke(store, "work.run.register-v1", _run_args(_key("run")), context)["run"]["run_id"]


def _items(store, count: int) -> list[int]:
    sprint_id = pg.create_sprint(store, f"Ledger-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")
    track_id = pg.get_or_create_track(store, sprint_id, "ledger")
    return [pg.create_work_item(store, sprint_id, track_id, f"item {i}") for i in range(count)]


def _diff(word: str = "the") -> str:
    return f"--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-teh\n+{word}\n"


def _propose_args(item_id: int, run_id: str, key: str, **overrides) -> dict:
    args = {
        "run_id": run_id, "item_id": item_id, "repository": "vuoro-e3-canary",
        "base_commit": "b" * 40, "title": "Fix a typo", "rationale": "The README misspells 'the'.",
        "unified_diff": _diff(), "idempotency_key": key,
    }
    args.update(overrides)
    return args


@dataclass(frozen=True)
class KeyedWrite:
    """One keyed write, ready to invoke: its ledger tuple, the request, a
    changed request under the same key, the identity of what it wrote and a
    count of the records it can have written."""

    context: Any
    operation: str
    tool: str
    arguments: dict
    changed: dict
    identity: Callable[[Mapping[str, Any]], Any]
    effects: Callable[[], int]

    @property
    def key_tuple(self) -> dict:
        return {
            "workspace_id": self.context.identity.workspace_id,
            "principal_id": self.context.identity.principal_id,
            "tool": self.tool,
            "key": self.arguments["idempotency_key"],
        }


def _register_run_write(store) -> KeyedWrite:
    principal = f"github:ledger-run-{uuid.uuid4().hex[:8]}:0"
    context = _context(principal)
    key = _key("register")
    return KeyedWrite(
        context=context, operation="work.run.register-v1", tool="register_run",
        arguments=_run_args(key), changed=_run_args(key, model_id="m-other"),
        identity=lambda result: result["run"]["run_id"],
        effects=lambda: _count(
            store, "SELECT count(*) AS n FROM run WHERE repo_id = %s AND principal_id = %s",
            (store.repo_id, principal),
        ),
    )


def _claim_write(store) -> KeyedWrite:
    context = _context(f"github:ledger-claim-{uuid.uuid4().hex[:8]}:0")
    item, other_item = _items(store, 2)
    run_id = _run(store, context)
    key = _key("claim")
    return KeyedWrite(
        context=context, operation="work.lease.acquire-v1", tool="claim_work",
        arguments={"item_id": item, "run_id": run_id, "idempotency_key": key},
        changed={"item_id": other_item, "run_id": run_id, "idempotency_key": key},
        identity=lambda result: result["lease"]["lease_id"],
        effects=lambda: _count(
            store, "SELECT count(*) AS n FROM work_lease WHERE repo_id = %s AND work_item_id = ANY(%s)",
            (store.repo_id, [item, other_item]),
        ),
    )


def _report_args(lease_id: str, run_id: str, key: str, summary: str = "done") -> dict:
    return {
        "lease_id": lease_id, "run_id": run_id, "outcome": "succeeded", "summary": summary,
        "payload": {"result": "ok"}, "checks": [{"name": "tests", "status": "passed"}],
        "idempotency_key": key,
    }


def _report_write(store) -> KeyedWrite:
    context = _context(f"github:ledger-report-{uuid.uuid4().hex[:8]}:0")
    (item,) = _items(store, 1)
    run_id = _run(store, context)
    lease_id = _invoke(store, "work.lease.acquire-v1", {
        "item_id": item, "run_id": run_id, "idempotency_key": _key("claim"),
    }, context)["lease"]["lease_id"]
    key = _key("report")
    return KeyedWrite(
        context=context, operation="work.lease.report-outcome-v1", tool="report_outcome",
        arguments=_report_args(lease_id, run_id, key),
        changed=_report_args(lease_id, run_id, key, summary="done, differently"),
        identity=lambda result: result["report"]["report_id"],
        effects=lambda: _count(
            store, "SELECT count(*) AS n FROM work_outcome_report WHERE repo_id = %s AND work_item_id = %s",
            (store.repo_id, item),
        ),
    )


def _propose_write(store) -> KeyedWrite:
    context = _context(f"github:ledger-propose-{uuid.uuid4().hex[:8]}:0")
    (item,) = _items(store, 1)
    run_id = _run(store, context)
    key = _key("propose")
    return KeyedWrite(
        context=context, operation="work.effect.propose-v1", tool="propose_effect",
        arguments=_propose_args(item, run_id, key),
        changed=_propose_args(item, run_id, key, unified_diff=_diff("thee")),
        identity=lambda result: result["intent"]["intent_id"],
        effects=lambda: _count(
            store, "SELECT count(*) AS n FROM work_effect_intent WHERE repo_id = %s AND work_item_id = %s",
            (store.repo_id, item),
        ),
    )


KEYED_WRITES: dict[str, Callable[[Any], KeyedWrite]] = {
    "register_run": _register_run_write,
    "claim_work": _claim_write,
    "report_outcome": _report_write,
    "propose_effect": _propose_write,
}


@pytest.fixture(params=sorted(KEYED_WRITES))
def keyed_write(request, store) -> KeyedWrite:
    return KEYED_WRITES[request.param](store)


def _committed_digest(store, write: KeyedWrite) -> str:
    """The digest the ledger committed for this write's tuple."""
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT request_digest FROM work_idempotency_ledger WHERE repo_id = %s "
            "AND workspace_id = %s AND principal_id = %s AND tool = %s AND idempotency_key = %s",
            (store.repo_id, *write.key_tuple.values()),
        )
        rows = cur.fetchall()
    store.conn.rollback()
    assert len(rows) == 1, f"{write.tool}: expected one committed ledger entry, found {len(rows)}"
    return rows[0]["request_digest"]


class TestKeyedWriteBehaviour:
    """The same checks for every keyed write tool sprintctl serves."""

    def test_the_same_request_replays_one_effect(self, store, keyed_write):
        first = _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        again = _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        assert keyed_write.identity(again) == keyed_write.identity(first)
        assert keyed_write.effects() == 1

    def test_a_changed_request_under_the_same_key_is_idempotency_conflict(self, store, keyed_write):
        first = _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        refused = _refused(
            lambda: _invoke(store, keyed_write.operation, keyed_write.changed, keyed_write.context)
        )
        assert (refused.code, refused.http_status) == ("idempotency-conflict", 409)
        assert keyed_write.effects() == 1
        entry = _peek(store, keyed_write.key_tuple, _committed_digest(store, keyed_write))
        assert keyed_write.identity(entry.result) == keyed_write.identity(first)

    def test_the_committed_write_is_a_ledger_entry_that_begin_replays(self, store, keyed_write):
        first = _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        digest = _committed_digest(store, keyed_write)
        entry = _peek(store, keyed_write.key_tuple, digest)
        assert isinstance(entry, pg.LedgerEntry)
        assert entry.replayed is True
        assert (entry.workspace_id, entry.principal_id, entry.tool, entry.key) == tuple(
            keyed_write.key_tuple.values()
        )
        assert entry.request_digest == digest
        assert isinstance(entry.result, Mapping)
        assert keyed_write.identity(entry.result) == keyed_write.identity(first)
        assert _peek(store, keyed_write.key_tuple, digest) == entry
        # begin changed nothing: the write still replays.
        again = _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        assert keyed_write.identity(again) == keyed_write.identity(first)
        assert keyed_write.effects() == 1

    def test_begin_with_another_digest_is_idempotency_key_reused(self, store, keyed_write):
        _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        digest = _committed_digest(store, keyed_write)
        other = ("0" if digest[0] != "0" else "1") + digest[1:]
        refused = _conflict(lambda: _peek(store, keyed_write.key_tuple, other))
        assert refused.code == pg.IDEMPOTENCY_KEY_REUSED == "idempotency-conflict"

    @pytest.mark.parametrize("part", ["workspace_id", "principal_id"])
    def test_another_workspace_or_principal_does_not_share_the_key(self, store, keyed_write, part):
        _invoke(store, keyed_write.operation, keyed_write.arguments, keyed_write.context)
        digest = _committed_digest(store, keyed_write)
        other = dict(keyed_write.key_tuple, **{part: keyed_write.key_tuple[part] + "-other"})
        entry = _peek(store, other, digest)
        assert entry.replayed is False and entry.result is None


class TestEffectAcceptance:
    """Acceptance is digest-bound (``work.effect.accept-v1`` names the intent,
    revision and canonical digest, and takes no idempotency key); what the
    ledger owes it is that the proposal's entry keeps naming the one intent
    across acceptance, so a proposer's retry never proposes again."""

    def test_a_retried_proposal_after_acceptance_names_the_accepted_intent(self, store):
        write = _propose_write(store)
        intent = _invoke(store, write.operation, write.arguments, write.context)["intent"]
        acceptor = _context(f"github:ledger-acceptor-{uuid.uuid4().hex[:8]}:0", _ACCEPTOR_AUTHORITIES)
        accepted = _invoke(store, "work.effect.accept-v1", {
            "intent_id": intent["intent_id"], "revision": intent["revision"],
            "canonical_intent_digest": intent["canonical_intent_digest"],
        }, acceptor)["intent"]
        assert accepted["state"] == "accepted"

        retried = _invoke(store, write.operation, write.arguments, write.context)["intent"]
        assert retried["intent_id"] == intent["intent_id"]
        assert retried["state"] == "accepted"
        assert write.effects() == 1

        entry = _peek(store, write.key_tuple, _committed_digest(store, write))
        assert entry.replayed is True
        assert entry.result["intent"]["intent_id"] == intent["intent_id"]

        refused = _refused(lambda: _invoke(store, write.operation, write.changed, write.context))
        assert (refused.code, refused.http_status) == ("idempotency-conflict", 409)
        assert write.effects() == 1
