"""Agent directory and durable inbox v1 against PostgreSQL (schema 25).

Contract: vuoro docs/architecture/agent-directory-and-inbox.md.  Every call
goes through WorkApplication.invoke with the catalog's input and result
schemas checked, and every receipt, directory entry and stamped message is
validated against the contract's own JSON schemas.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import agent_inbox_pg, pg, pg_migrations, pg_testing
from sprintctl.agent_inbox import canonical_bytes
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.agent_inbox_schemas import STAMPS, valid, valid_stamped
from tests.pg._shared import _PG_URL, PG_MARKS, assert_disposable_connection, dict_row, psycopg

pytestmark = PG_MARKS
CONTRACTS = {item.name: item for item in WORK_OPERATION_CONTRACTS}
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
WS = "ws-inbox"


def _ulid() -> str:
    raw = uuid.uuid4().int
    return "".join(_ALPHABET[(raw >> (5 * i)) & 31] for i in range(26))


def new_instance() -> str:
    return "ins_" + _ulid()


def new_message_id() -> str:
    return "msg_" + _ulid()


def _connect(**options) -> pg.PgStore:
    conn = psycopg.connect(_PG_URL, row_factory=dict_row, **options)
    assert_disposable_connection(conn)
    store = pg.PgStore(conn=conn, repo_id="itest-agent-inbox-bootstrap")
    pg_migrations.migrate_schema(store)  # no-op past 25; records the handshake version
    return store


def context(principal_id, repo_id, workspace_id=WS):
    identity = SimpleNamespace(
        actor=principal_id, environment="pg-test",
        authorities=frozenset({"work:evidence", "work:read", "work:claim"}),
        principal_id=principal_id, workspace_id=workspace_id, client_id=None, grant_id=None)
    return SimpleNamespace(identity=identity, request_id="req-" + uuid.uuid4().hex, basis_revision=None,
                           catalog_revision="test", idempotency_requirement="not-allowed",
                           idempotency_key=None, repo_id=repo_id)


class Refused(Exception):
    def __init__(self, rejection: ApplicationRejection):
        super().__init__(rejection.code)
        self.code = rejection.code
        self.details = dict(rejection.details or {})


class Client:
    """WorkApplication.invoke with catalog schemas checked both ways."""

    def __init__(self, store):
        self.store = store
        self.app = WorkApplication.postgres(store)

    def __call__(self, ctx, operation, _unchecked=False, **arguments):
        if not _unchecked:  # _unchecked: send what the catalog would refuse, to reach the owner
            jsonschema.validate(arguments, CONTRACTS[operation].input_schema)
        try:
            result = self.app.invoke(operation, arguments, ctx)
        except ApplicationRejection as error:
            raise Refused(error) from error
        jsonschema.validate(result, CONTRACTS[operation].result_schema)
        return result


def declaration(*, interactive=True, channels=("mcp",)):
    return {"schema": "agent-declaration/v1", "harness": {"name": "claude-code", "version": "1.0"},
            "adapter": None, "host_class": "workstation",
            "interactivity": "interactive" if interactive else "non-interactive",
            "accepted_kinds": ["continue"],
            "channels": [{"channel": c, "wake_modes": ["read-while-running"]} for c in channels]}


def expect(code, fn, *args, **kwargs):
    with pytest.raises(Refused) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, f"expected {code}, got {caught.value.code}"
    return caught.value


class World:
    """One repository: sender S, recipient R with instances ins1/ins2, stranger X."""

    def __init__(self, client, repo_id):
        self.c, self.repo = client, repo_id
        self.S = context("github:1:0", repo_id)
        self.R = context("github:2:0", repo_id)
        self.X = context("github:3:0", repo_id)
        self.ins1, self.ins2 = new_instance(), new_instance()
        self._keys = 0
        self.register(self.R, self.ins1)
        self.register(self.R, self.ins2)
        scoped = pg.PgStore(conn=client.store.conn, repo_id=repo_id)
        sprint = pg.create_sprint(scoped, "Inbox", "Contract", "2026-01-01", "2026-12-31", "active")
        track = pg.get_or_create_track(scoped, sprint, "inbox")
        self.item = int(pg.create_work_item(scoped, sprint, track, "Inbox item"))
        client.store.conn.commit()
        self.run = self.new_run(self.R)

    def key(self):
        self._keys += 1
        return f"key-{uuid.uuid4().hex[:8]}-{self._keys}"

    def new_run(self, ctx):
        return self.c(ctx, "work.run.register-v1", harness_id="itest", harness_build="b", model_id="m",
                      recipe_id="r", observed_profile={"instruction_digest": "sha256:" + "a" * 64,
                                                       "skill_digests": []},
                      idempotency_key=self.key())["run"]["run_id"]

    def register(self, ctx, instance, decl=None, key=None):
        return self.c(ctx, "work.agent.register-v1", instance_id=instance,
                      declaration=decl or declaration(), idempotency_key=key or self.key())

    def observe(self, ctx, instance, fact="observed", seconds=60, key=None, **extra):
        return self.c(ctx, "work.agent.observe-v1", instance_id=instance, fact=fact,
                      valid_for_seconds=seconds, idempotency_key=key or self.key(), **extra)

    def message(self, *, instance=None, **overrides):
        recipient = {"principal_id": self.R.identity.principal_id}
        if instance:
            recipient["instance_id"] = instance
        message = {"schema": "agent-message/v1", "message_id": new_message_id(), "recipient": recipient,
                   "kind": "continue", "mode": "request", "context": {"item_id": self.item},
                   "body": {"text": "please continue"}, "created_at": "2026-10-14T10:00:00Z"}
        message.update(overrides)
        return message

    def send(self, ctx=None, message=None, **kw):
        message = message or self.message(**kw)
        result = self.c(ctx or self.S, "work.message.send-v1", message=message)
        assert set(STAMPS) <= set(result)
        return result

    def sent(self, **kw):
        return self.send(**kw)["message_id"]

    def rcpt(self, message_id, kind, instance=None, ctx=None, **fields):
        result = self.c(ctx or self.R, "work.message.receipt-v1", message_id=message_id,
                        instance_id=instance or self.ins1, kind=kind, **fields)
        return valid("message-receipt", result)

    def read(self, ctx=None, **kw):
        kw.setdefault("after_seq", 0)
        kw.setdefault("limit", 100)
        result = self.c(ctx or self.R, "work.message.read-v1", **kw)
        for message in result["messages"]:
            valid_stamped(message)
        return result

    def status(self, message_id, ctx=None):
        result = self.c(ctx or self.S, "work.message.status-v1", message_id=message_id)
        valid_stamped(result["message"])
        assert result["receipts"] == result["message"]["receipts"]
        return result

    def kinds(self, message_id):
        return [r["kind"] for r in self.status(message_id)["receipts"]]

    def describe(self, principal_id=None, ctx=None):
        return valid("directory-entry", self.c(ctx or self.S, "work.agent.describe-v1",
                                               principal_id=principal_id or self.R.identity.principal_id))

    def instance(self, instance_id):
        entry = self.describe()
        return entry, next(i for i in entry["instances"] if i["instance_id"] == instance_id)


@pytest.fixture(scope="module")
def client(store):
    """A module client on its own connection; repositories come per test."""
    del store  # the shared fixture migrated the public schema
    own = _connect()
    try:
        yield Client(own)
    finally:
        own.conn.rollback()
        own.conn.close()


@pytest.fixture
def w(client, pg_test_scope):
    pg_testing.release_inbox_clock(client.store)
    client.store.conn.commit()
    return World(client, pg_test_scope("agent-inbox"))


# -- scope: identity comes from the authenticated call -----------------------

def test_stamps_come_from_the_authenticated_context(w):
    message = w.message()
    stamps = w.send(message=message)
    assert (stamps["sender_principal_id"], stamps["workspace_id"], stamps["repo_id"]) == (
        "github:1:0", WS, w.repo)
    assert stamps["envelope_digest"] == hashlib.sha256(canonical_bytes(message)).hexdigest()
    assert stamps["replayed"] is False
    for field in ("principal_id", "workspace_id", "repo_id", "sender_principal_id"):
        expect("invalid-arguments", w.c, w.S, "work.message.send-v1", message=message,
               **{field: "spoofed"}, _unchecked=True)
        expect("invalid-arguments", w.c, w.R, "work.agent.register-v1", instance_id=new_instance(),
               declaration=declaration(), idempotency_key=w.key(), **{field: "spoofed"}, _unchecked=True)


def test_out_of_scope_records_are_not_found_never_forbidden(w):
    m = w.sent()
    w.rcpt(m, "observed")
    foreign = [context("github:2:0", w.repo, "ws-other"), context("github:2:0", w.repo + "-b"),
               context("github:1:0", w.repo + "-b")]
    for ctx in foreign:
        expect("not-found", w.describe, ctx=ctx)
        expect("not-found", w.status, m, ctx=ctx)
        expect("not-found", w.read, ctx=ctx, instance_id=w.ins1)
        expect("not-found", w.rcpt, m, "acknowledged", ctx=ctx)
        expect("not-found", w.c, ctx, "work.message.cancel-v1", message_id=m, reason="x")
        expect("not-found", w.observe, ctx, w.ins1)
        assert w.read(ctx=ctx)["messages"] == []
        assert w.c(ctx, "work.agent.list-v1")["entries"] == []
    # A stranger in scope sees nothing and writes nothing.
    expect("not-found", w.status, m, ctx=w.X)
    expect("not-found", w.rcpt, m, "observed", ctx=w.X)
    assert w.kinds(m) == ["observed"]


def test_instance_ids_are_per_scope_and_bound_to_their_first_principal(w):
    other = context("github:9:0", w.repo, "ws-other")
    w.register(other, w.ins1)  # same id, other workspace: a different record
    expect("instance-not-owned", w.register, w.X, w.ins1)
    expect("instance-not-owned", w.observe, w.X, w.ins1)
    expect("instance-not-owned", w.c, w.X, "work.agent.retire-v1", instance_id=w.ins1, reason="takeover",
           idempotency_key=w.key())
    assert [e["principal_id"] for e in w.c(w.R, "work.agent.list-v1")["entries"]] == ["github:2:0"]
    assert [e["principal_id"] for e in w.c(other, "work.agent.list-v1")["entries"]] == ["github:9:0"]


# -- inbox_seq: commit order equals sequence order ---------------------------

class _GatedConnection:
    """Delegates to a psycopg connection but holds commit() until released."""

    def __init__(self, conn, reached, release):
        self._conn, self._reached, self._release = conn, reached, release

    def commit(self):
        self._reached.set()
        assert self._release.wait(30), "held commit was never released"
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _reversed_sends(w, message_a, message_b):
    """A allocates and holds its commit; B then sends; a reader polls between.

    Returns (b_committed_while_a_held, first_poll, second_poll)."""
    store_a, store_b, reader = _connect(), _connect(), _connect()
    reached, release = threading.Event(), threading.Event()
    store_a.conn = _GatedConnection(store_a.conn, reached, release)
    failures, done_b = [], threading.Event()

    def send(store, message, done=None):
        try:
            WorkApplication.postgres(store).invoke("work.message.send-v1", {"message": message}, w.S)
        except Exception as error:  # surfaced after join
            failures.append(error)
        finally:
            if done is not None:
                done.set()

    try:
        thread_a = threading.Thread(target=send, args=(store_a, message_a))
        thread_a.start()
        assert reached.wait(30), "send A never reached commit"
        thread_b = threading.Thread(target=send, args=(store_b, message_b, done_b))
        thread_b.start()
        b_first = done_b.wait(2)
        poll = Client(reader)
        first = poll(w.R, "work.message.read-v1", after_seq=0, limit=100)
        release.set()
        thread_a.join(30)
        thread_b.join(30)
        assert not failures, failures
        second = poll(w.R, "work.message.read-v1", after_seq=first["next_after_seq"], limit=100)
        return b_first, first, second
    finally:
        release.set()
        for s in (store_a, store_b, reader):
            s.conn.close()


def test_concurrent_sends_commit_in_sequence_order_and_a_polling_reader_never_skips(w):
    w.sent()  # the counter row exists; allocation contends on it
    a, b = w.message(), w.message()
    b_first, first, second = _reversed_sends(w, a, b)
    assert not b_first, "B committed while A held the counter row"
    seen = [m["message_id"] for m in first["messages"] + second["messages"]]
    assert a["message_id"] in seen and b["message_id"] in seen, "the polling reader skipped a message"
    seqs = [m["inbox_seq"] for m in first["messages"] + second["messages"]]
    assert seqs == sorted(set(seqs))
    final = [m["message_id"] for m in w.read()["messages"]]
    assert final[-2:] == [a["message_id"], b["message_id"]]


def test_negative_control_allocation_outside_the_send_transaction_skips(w, monkeypatch):
    """Falsification kept in the suite: allocate like a sequence (committed
    at once, outside the send's lock) and the reader of the test above does
    skip.  Proves that test detects the bug it guards."""
    w.sent()

    def sequence_like(cur, store, workspace_id):
        side = _connect()
        try:
            with side.conn.cursor() as own:
                own.execute("UPDATE agent_inbox_counter SET last_seq = last_seq + 1 "
                            "WHERE repo_id=%s AND workspace_id=%s RETURNING last_seq",
                            (store.repo_id, workspace_id))
                value = int(own.fetchone()["last_seq"])
            side.conn.commit()
            return value
        finally:
            side.conn.close()

    monkeypatch.setattr(agent_inbox_pg, "_allocate_inbox_seq", sequence_like)
    a, b = w.message(), w.message()
    b_first, first, second = _reversed_sends(w, a, b)
    assert b_first
    seen = [m["message_id"] for m in first["messages"] + second["messages"]]
    assert b["message_id"] in seen and a["message_id"] not in seen, "the broken allocator was not exposed"


def test_inbox_seq_is_positive_dense_and_per_repository(w, pg_test_scope):
    ids = [w.send()["inbox_seq"] for _ in range(3)]
    assert ids == list(range(ids[0], ids[0] + 3)) and ids[0] == 1
    other = World(w.c, pg_test_scope("agent-inbox-b"))
    assert other.send()["inbox_seq"] == 1


# -- send ---------------------------------------------------------------------

def test_send_replays_by_message_id_and_refuses_conflicts(w):
    message = w.message()
    first = w.send(message=message)
    again = w.send(message=message)
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert {k: first[k] for k in (*STAMPS, "message_id")} == {k: again[k] for k in (*STAMPS, "message_id")}
    expect("message-id-conflict", w.send, message={**message, "body": {"text": "changed"}})
    expect("message-id-conflict", w.send, ctx=w.X, message=message)
    page = w.read()
    assert [m["message_id"] for m in page["messages"]] == [message["message_id"]]
    assert page["messages"][0]["body"] == {"text": "please continue"}


def _sized(w, target):
    message = w.message(artifacts=[{"uri": "a", "sha256": "0" * 64, "size_bytes": 1} for _ in range(16)])
    remaining = target - len(canonical_bytes(message))
    for artifact in message["artifacts"]:
        extra = min(2040, remaining // 4)
        artifact["uri"] += "\U0001F600" * extra
        remaining -= 4 * extra
    message["artifacts"][0]["uri"] += "b" * remaining
    assert len(canonical_bytes(message)) == target
    return message


def test_send_limits_mode_recipient_and_context(w, pg_test_scope):
    w.send(message=_sized(w, 64 * 1024))
    expect("payload-too-large", w.send, message=_sized(w, 64 * 1024 + 1))
    expect("mode-not-supported", w.send, message=w.message(mode="delegation"))
    expect("recipient-unknown", w.send, message=w.message(recipient={"principal_id": "github:77:0"}))
    expect("recipient-unknown", w.send, message=w.message(instance=new_instance()))
    stranger = new_instance()
    w.register(w.X, stranger)
    expect("recipient-unknown", w.send, message=w.message(instance=stranger))
    elsewhere = World(w.c, pg_test_scope("agent-inbox-elsewhere"))
    for ctx in ({"item_id": w.item + 100000}, {"predecessor_run_id": "run_" + "1" * 26},
                {"predecessor_run_id": elsewhere.run}, {"item_id": w.item + 100000, "predecessor_run_id": w.run}):
        message = w.message(context=ctx)
        expect("context-not-found", w.send, message=message)
        expect("not-found", w.status, message["message_id"])
    w.send(message=w.message(context={"predecessor_run_id": w.run}))
    assert len(w.read()["messages"]) == 2


# -- receipts -----------------------------------------------------------------

def test_single_decision_names_the_decider(w):
    m = w.sent()
    won = w.rcpt(m, "accepted", w.ins1)
    error = expect("already-decided", w.rcpt, m, "accepted", w.ins2)
    assert error.details == {"instance_id": w.ins1, "kind": "accepted"}
    error = expect("already-decided", w.rcpt, m, "refused", w.ins1, reason={"code": "busy", "text": "no"})
    assert error.details == {"instance_id": w.ins1, "kind": "accepted"}
    replay = w.rcpt(m, "accepted", w.ins1)
    assert replay["replayed"] is True and replay["recorded_at"] == won["recorded_at"]
    assert w.kinds(m) == ["accepted"]


def test_concurrent_decisions_leave_exactly_one(w):
    instances = [w.ins1, w.ins2] + [new_instance() for _ in range(4)]
    for instance in instances[2:]:
        w.register(w.R, instance)
    m = w.sent()
    stores = [_connect() for _ in instances]
    barrier, outcomes = threading.Barrier(len(instances)), []

    def decide(store, instance):
        barrier.wait()
        try:
            outcomes.append(("ok", WorkApplication.postgres(store).invoke(
                "work.message.receipt-v1", {"message_id": m, "instance_id": instance, "kind": "accepted"}, w.R)))
        except ApplicationRejection as error:
            outcomes.append((error.code, dict(error.details or {})))

    threads = [threading.Thread(target=decide, args=pair) for pair in zip(stores, instances)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        for store in stores:
            store.conn.close()
    winners = [result for code, result in outcomes if code == "ok"]
    assert len(winners) == 1 and len(outcomes) == len(instances)
    losers = [details for code, details in outcomes if code != "ok"]
    assert all(code in ("ok", "already-decided") for code, _ in outcomes)
    assert all(d == {"instance_id": winners[0]["instance_id"], "kind": "accepted"} for d in losers)
    assert w.kinds(m) == ["accepted"]


def test_targeted_message_is_decided_only_by_its_instance(w):
    m = w.sent(instance=w.ins1)
    expect("not-found", w.rcpt, m, "accepted", w.ins2)
    assert [x["message_id"] for x in w.read(instance_id=w.ins2)["messages"]] == []
    w.rcpt(m, "accepted", w.ins1)


def test_run_linked_rules(w):
    m = w.sent()
    expect("not-accepted", w.rcpt, m, "run-linked", run_id=w.run)
    w.rcpt(m, "accepted")
    expect("run-not-found", w.rcpt, m, "run-linked", run_id="run_" + "0" * 26)
    expect("not-accepted", w.rcpt, m, "run-linked", w.ins2, run_id=w.run)
    first = w.rcpt(m, "run-linked", run_id=w.run)
    replay = w.rcpt(m, "run-linked", run_id=w.run)
    assert replay["replayed"] is True and replay["recorded_at"] == first["recorded_at"]
    expect("run-already-linked", w.rcpt, m, "run-linked", run_id=w.new_run(w.R))
    assert w.kinds(m) == ["accepted", "run-linked"]


def test_launch_ambiguous_requires_acceptance_and_is_one_per_instance(w):
    """vuoro#219: requires accepted by the same instance; run_id optional."""
    m = w.sent()
    expect("not-accepted", w.rcpt, m, "launch-ambiguous", detail="x")
    w.rcpt(m, "accepted")
    expect("not-accepted", w.rcpt, m, "launch-ambiguous", w.ins2, detail="x")
    expect("run-not-found", w.rcpt, m, "launch-ambiguous", detail="x", run_id="run_" + "0" * 26)
    first = w.rcpt(m, "launch-ambiguous", detail="spawn timed out", run_id=w.run)
    assert w.rcpt(m, "launch-ambiguous", detail="spawn timed out", run_id=w.run)["replayed"] is True
    expect("already-recorded", w.rcpt, m, "launch-ambiguous", detail="other")
    expect("already-recorded", w.rcpt, m, "launch-ambiguous", detail="spawn timed out")
    assert first["run_id"] == w.run and w.kinds(m) == ["accepted", "launch-ambiguous"]
    unlinked = w.sent()
    w.rcpt(unlinked, "accepted")
    assert "run_id" not in w.rcpt(unlinked, "launch-ambiguous", detail="registration uncertain")


def test_result_linked_outcome_rules(w):
    m = w.sent()
    expect("not-accepted", w.rcpt, m, "result-linked", outcome="not-started", evidence_refs=[], summary="",
           reason={"code": "x", "text": "never accepted"})
    w.rcpt(m, "accepted")
    for outcome in ("completed", "failed", "canceled"):
        expect("not-accepted", w.rcpt, m, "result-linked", outcome=outcome, evidence_refs=[], summary="")
    expect("invalid-arguments", w.rcpt, m, "result-linked", outcome="not-started", evidence_refs=[], summary="")
    closed = w.rcpt(m, "result-linked", outcome="not-started", evidence_refs=[], summary="policy",
                    reason={"code": "policy-recheck", "text": "grant removed"})
    assert closed["reason"]["code"] == "policy-recheck"
    expect("already-resulted", w.rcpt, m, "result-linked", outcome="abandoned", evidence_refs=[], summary="")
    linked = w.sent()
    w.rcpt(linked, "accepted")
    w.rcpt(linked, "run-linked", run_id=w.run)
    refs = [{"kind": "commit", "ref": "abc123"}]
    w.rcpt(linked, "result-linked", outcome="completed", evidence_refs=refs, summary="merged")
    assert w.rcpt(linked, "result-linked", outcome="completed", evidence_refs=refs, summary="merged")["replayed"]
    expect("already-resulted", w.rcpt, linked, "result-linked", outcome="completed", evidence_refs=refs,
           summary="other")


def test_abandoned_by_a_sibling_only_after_the_acceptor_retires(w):
    m = w.sent()
    w.rcpt(m, "accepted", w.ins1)
    gone = dict(outcome="abandoned", evidence_refs=[], summary="host lost")
    expect("not-accepted", w.rcpt, m, "result-linked", w.ins2, **gone)
    w.c(w.R, "work.agent.retire-v1", instance_id=w.ins1, reason="host lost", idempotency_key=w.key())
    expect("instance-retired", w.rcpt, m, "result-linked", w.ins1, **gone)
    expect("not-accepted", w.rcpt, m, "result-linked", w.ins2, outcome="not-started", evidence_refs=[],
           summary="", reason={"code": "x", "text": "y"})
    closed = w.rcpt(m, "result-linked", w.ins2, **gone)
    assert (closed["instance_id"], closed["principal_id"]) == (w.ins2, "github:2:0")


def test_only_the_recipient_writes_receipts_and_only_the_sender_cancels(w):
    m = w.sent()
    expect("not-found", w.rcpt, m, "observed", ctx=w.S)
    other = new_instance()
    w.register(w.X, other)
    expect("instance-not-owned", w.rcpt, m, "observed", other)
    expect("not-found", w.rcpt, m, "observed", new_instance())
    expect("not-sender", w.c, w.R, "work.message.cancel-v1", message_id=m, reason="x")
    expect("not-sender", w.c, w.X, "work.message.cancel-v1", message_id=m, reason="x")
    first = valid("message-receipt", w.c(w.S, "work.message.cancel-v1", message_id=m, reason="stop"))
    assert (first["kind"], first["instance_id"], first["replayed"]) == ("cancel-requested", None, False)
    again = w.c(w.S, "work.message.cancel-v1", message_id=m, reason="a different reason")
    assert again["replayed"] is True and again["reason"] == "stop"
    assert {k: v for k, v in again.items() if k != "replayed"} == {k: v for k, v in first.items() if k != "replayed"}
    w.rcpt(m, "refused", reason={"code": "canceled-by-sender", "text": "sender canceled"})
    assert w.kinds(m) == ["cancel-requested", "refused"]


def test_per_instance_receipts_replay_and_never_conflict(w):
    m = w.sent()
    first = w.rcpt(m, "observed", w.ins1)
    w.rcpt(m, "observed", w.ins2)
    assert w.rcpt(m, "observed", w.ins1) == {**first, "replayed": True}
    w.rcpt(m, "acknowledged", w.ins1)
    assert w.kinds(m) == ["observed", "observed", "acknowledged"]


# -- observations and facts ----------------------------------------------------

def test_observations_are_owner_stamped_bounded_and_owned(w):
    frozen = pg_testing.freeze_inbox_clock(w.c.store, "2026-10-14T12:00:00Z")
    w.c.store.conn.commit()
    assert frozen
    seen = w.observe(w.R, w.ins1, seconds=60)
    assert (seen["observed_at"], seen["valid_until"], seen["source"]) == (
        "2026-10-14T12:00:00Z", "2026-10-14T12:01:00Z", "self-reported")
    for seconds in (0, 3601):
        expect("invalid-arguments", w.observe, w.R, w.ins1, seconds=seconds, _unchecked=True)
    expect("not-found", w.observe, w.R, new_instance())
    robot = new_instance()
    w.register(w.R, robot, declaration(interactive=False, channels=()))
    expect("fact-not-applicable", w.observe, w.R, robot, fact="attended")
    same = dict(key="same-key-0001")
    first = w.observe(w.R, w.ins1, **same)
    replay = w.observe(w.R, w.ins1, **same)
    assert replay["replayed"] is True and replay["observed_at"] == first["observed_at"]
    expect("idempotency-conflict", w.observe, w.R, w.ins1, seconds=61, **same)


def test_facts_are_computed_at_read_time_and_never_false(w):
    pg_testing.freeze_inbox_clock(w.c.store, "2026-10-14T12:00:00Z")
    w.c.store.conn.commit()
    _, fresh = w.instance(w.ins1)
    assert fresh["observed"] == {"state": "unknown", "observed_at": None, "valid_until": None, "source": None}
    assert fresh["reachable"] == [{"state": "unknown", "observed_at": None, "valid_until": None, "source": None,
                                   "channel": "mcp"}]
    assert fresh["executing"] == [] and fresh["attended"]["state"] == "unknown"
    other_run = w.new_run(w.R)
    w.observe(w.R, w.ins1, seconds=60)
    w.observe(w.R, w.ins1, fact="reachable", channel="mcp", seconds=30)
    w.observe(w.R, w.ins1, fact="attended", seconds=30)
    w.observe(w.R, w.ins1, fact="executing", run_id=w.run, seconds=120)
    w.observe(w.R, w.ins1, fact="executing", run_id=other_run, seconds=10)
    entry, now = w.instance(w.ins1)
    assert entry["evaluated_at"] == "2026-10-14T12:00:00Z"
    assert {r["run_id"]: r["state"] for r in now["executing"]} == {w.run: "current", other_run: "current"}
    pg_testing.shift_inbox_clock(w.c.store, 10)  # evaluated_at == valid_until is not current
    w.c.store.conn.commit()
    entry, aged = w.instance(w.ins1)
    assert entry["evaluated_at"] == "2026-10-14T12:00:10Z"
    assert {r["run_id"]: r["state"] for r in aged["executing"]} == {w.run: "current", other_run: "stale"}
    pg_testing.shift_inbox_clock(w.c.store, 120)
    w.c.store.conn.commit()
    _, aged = w.instance(w.ins1)
    assert aged["observed"]["state"] == aged["attended"]["state"] == aged["reachable"][0]["state"] == "stale"
    assert {r["state"] for r in aged["executing"]} == {"stale"} and len(aged["executing"]) == 2
    assert "false" not in json.dumps(w.describe()).lower()
    robot = new_instance()
    w.register(w.R, robot, declaration(interactive=False, channels=()))
    assert w.instance(robot)[1]["attended"] == {
        "state": "not-applicable", "observed_at": None, "valid_until": None, "source": None}
    with w.c.store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM agent_observation WHERE repo_id=%s", (w.repo,))
        assert cur.fetchone()["n"] == 5, "expiry deletes nothing"
    w.c.store.conn.rollback()


def test_retired_instance_is_listed_but_inert(w):
    m = w.sent()
    w.observe(w.R, w.ins1, seconds=600)
    retired = w.c(w.R, "work.agent.retire-v1", instance_id=w.ins1, reason="decommissioned", idempotency_key="retire-key-1")
    assert w.c(w.R, "work.agent.retire-v1", instance_id=w.ins1, reason="decommissioned",
               idempotency_key="retire-key-1")["replayed"] is True
    expect("instance-retired", w.c, w.R, "work.agent.retire-v1", instance_id=w.ins1, reason="again",
           idempotency_key=w.key())
    expect("instance-retired", w.observe, w.R, w.ins1)
    expect("instance-retired", w.register, w.R, w.ins1)
    for kind in ("observed", "acknowledged", "accepted"):
        expect("instance-retired", w.rcpt, m, kind, w.ins1)
    expect("recipient-unknown", w.send, message=w.message(instance=w.ins1))
    _, entry = w.instance(w.ins1)
    assert entry["registered"] == {"state": "retired", "registered_at": entry["registered"]["registered_at"],
                                   "retired_at": retired["retired_at"], "retire_reason": "decommissioned"}
    assert entry["observed"]["state"] == "current"
    active = w.c(w.S, "work.agent.list-v1", include_retired=False)["entries"]
    assert [i["instance_id"] for i in active[0]["instances"]] == [w.ins2]


def test_registration_revisions_and_ledger_idempotency(w):
    revised = declaration(channels=("mcp", "tmux"))
    first = w.register(w.R, w.ins1, revised, key="revision-key-1")
    assert (first["declaration_revision"], first["replayed"]) == (2, False)
    assert w.register(w.R, w.ins1, revised, key="revision-key-1") == {**first, "replayed": True}
    expect("idempotency-conflict", w.register, w.R, w.ins1, declaration(), key="revision-key-1")
    assert w.register(w.R, w.ins1, revised)["declaration_revision"] == 2  # unchanged: no revision
    _, instance = w.instance(w.ins1)
    assert instance["declaration"] == revised and instance["declaration_revision"] == 2
    with w.c.store.conn.cursor() as cur:
        cur.execute("SELECT revision FROM agent_declaration_revision WHERE repo_id=%s AND instance_id=%s "
                    "ORDER BY revision", (w.repo, w.ins1))
        assert [r["revision"] for r in cur.fetchall()] == [1, 2], "history is kept"
    w.c.store.conn.rollback()


# -- reads ------------------------------------------------------------------------

def _table_state(w):
    with w.c.store.conn.cursor() as cur:
        state = {}
        for table in ("agent_instance", "agent_declaration_revision", "agent_observation",
                      "agent_message", "agent_message_receipt", "agent_inbox_counter"):
            cur.execute(f"SELECT md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) AS h "  # noqa: S608
                        f"FROM {table} t WHERE repo_id=%s", (w.repo,))
            state[table] = cur.fetchone()["h"]
    w.c.store.conn.rollback()
    return state


def test_read_cursor_filters_and_mutates_nothing(w):
    general = w.sent()
    targeted = w.sent(instance=w.ins1)
    w.rcpt(general, "observed")
    before = _table_state(w)
    ids = lambda **kw: [m["message_id"] for m in w.read(**kw)["messages"]]  # noqa: E731
    assert ids() == [general]
    assert ids(instance_id=w.ins1) == [general, targeted]
    assert ids(instance_id=w.ins2) == [general]
    assert w.read(ctx=w.S)["messages"] == []
    page = w.read(limit=1, instance_id=w.ins1)
    assert page["next_after_seq"] == page["messages"][0]["inbox_seq"]
    rest = w.read(after_seq=page["next_after_seq"], instance_id=w.ins1)
    assert [m["message_id"] for m in rest["messages"]] == [targeted]
    empty = w.read(after_seq=rest["next_after_seq"] + 5)
    assert empty == {"messages": [], "next_after_seq": rest["next_after_seq"] + 5}
    assert w.read()["messages"][0]["receipts"][0]["kind"] == "observed"
    assert w.read() == w.read(), "reading twice returns the same page"
    for bad in ({"after_seq": -1}, {"limit": 0}, {"limit": 101}):
        expect("invalid-arguments", w.read, _unchecked=True, **bad)
    expect("not-found", w.read, instance_id=new_instance())
    w.status(general)
    w.c(w.S, "work.agent.list-v1")
    w.describe()
    assert _table_state(w) == before, "reads changed stored state"


def test_status_is_for_sender_and_recipient_only(w):
    m = w.sent()
    assert w.status(m, ctx=w.S)["message"]["message_id"] == m
    assert w.status(m, ctx=w.R)["receipts"] == []
    expect("not-found", w.status, m, ctx=w.X)
    expect("not-found", w.status, new_message_id())


# -- storage guards -------------------------------------------------------------------

def test_append_only_records_refuse_update_and_delete(w):
    m = w.sent()
    w.rcpt(m, "observed")
    w.observe(w.R, w.ins1)
    for statement in ("UPDATE agent_message SET envelope = envelope WHERE repo_id=%s",
                      "DELETE FROM agent_message_receipt WHERE repo_id=%s",
                      "UPDATE agent_observation SET source = source WHERE repo_id=%s",
                      "DELETE FROM agent_declaration_revision WHERE repo_id=%s",
                      "DELETE FROM agent_instance WHERE repo_id=%s",
                      "UPDATE agent_instance SET principal_id = 'x' WHERE repo_id=%s"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with w.c.store.conn.cursor() as cur:
                cur.execute(statement, (w.repo,))
        w.c.store.conn.rollback()


def test_schema_25_handshake_reports_the_agent_inbox_catalog(w):
    compatibility = pg_migrations.compatibility_handshake(w.c.store)
    storage = compatibility["capabilities"]["agent_inbox_storage"]
    assert compatibility["compatible"] and compatibility["remote_schema"]["actual"] == 25
    assert storage["available"] and storage["complete"] and storage["table_count"] == 6
    assert storage["catalog_fingerprint"] == storage["expected_catalog_fingerprint"]


def test_owner_clock_is_the_database_clock_unless_a_test_freezes_it(w):
    with w.c.store.conn.cursor() as cur:
        real = agent_inbox_pg.owner_now(cur)
        cur.execute("SELECT clock_timestamp() AS now")
        assert abs((cur.fetchone()["now"] - real).total_seconds()) < 5
    w.c.store.conn.rollback()
    assert isinstance(real, datetime)


# -- review fixes (owner clock, send race, receipt/retire race, run scope) -----

def test_a_session_setting_alone_never_moves_the_owner_clock(w, monkeypatch):
    """The test clock needs the process-local flag only pg_testing sets: a
    setting injected through connection options, PGOPTIONS or ALTER ROLE/
    DATABASE SET is ignored by the production path."""
    monkeypatch.setattr(agent_inbox_pg, "_TEST_CLOCK_ENABLED", False, raising=False)
    injected = _connect(options="-c sprintctl.inbox_test_now=2001-01-01T00:00:00Z")
    try:
        with injected.conn.cursor() as cur:
            cur.execute("SELECT current_setting('sprintctl.inbox_test_now') AS v")
            assert cur.fetchone()["v"].startswith("2001-01-01")
            assert agent_inbox_pg.owner_now(cur).year >= 2026
        injected.conn.rollback()
        stamps = Client(injected)(w.S, "work.message.send-v1", message=w.message())
        assert not stamps["persisted_at"].startswith("2001"), stamps["persisted_at"]
        with injected.conn.cursor() as cur:
            cur.execute("SELECT set_config('sprintctl.inbox_test_now', 'not a time', false)")
            assert agent_inbox_pg.owner_now(cur).year >= 2026  # garbage is ignored, not blamed on the caller
        injected.conn.rollback()
    finally:
        injected.conn.close()


def test_the_test_clock_is_honoured_once_test_support_enables_it(w):
    pg_testing.freeze_inbox_clock(w.c.store, "2026-10-14T12:00:00Z")
    w.c.store.conn.commit()
    assert w.send()["persisted_at"] == "2026-10-14T12:00:00Z"


def test_two_senders_racing_on_one_message_id_get_message_id_conflict(w):
    """The first sender holds its commit; the second's insert must resolve to
    message-id-conflict, never a raw unique violation."""
    message = w.message()
    first, second = _connect(), _connect()
    reached, release = threading.Event(), threading.Event()
    first.conn = _GatedConnection(first.conn, reached, release)
    results = {}

    def send(name, store, ctx):
        try:
            results[name] = ("ok", WorkApplication.postgres(store).invoke(
                "work.message.send-v1", {"message": message}, ctx))
        except ApplicationRejection as error:
            results[name] = (error.code, None)
        except Exception as error:  # noqa: BLE001 - the regression under test
            results[name] = (type(error).__name__, None)

    try:
        a = threading.Thread(target=send, args=("S", first, w.S))
        a.start()
        assert reached.wait(30)
        b = threading.Thread(target=send, args=("X", second, w.X))
        b.start()
        b.join(1)  # X waits behind S's counter row
        release.set()
        a.join(30)
        b.join(30)
    finally:
        release.set()
        first.conn.close()
        second.conn.close()
    assert results["S"][0] == "ok"
    assert results["X"] == ("message-id-conflict", None), results["X"]
    assert [m["message_id"] for m in w.read()["messages"]] == [message["message_id"]]


def test_a_retirement_cannot_commit_between_a_receipts_check_and_its_insert(w):
    m = w.sent()
    writer, retirer = _connect(), _connect()
    reached, release = threading.Event(), threading.Event()
    writer.conn = _GatedConnection(writer.conn, reached, release)
    retired, failures = threading.Event(), []

    def receipt():
        try:
            WorkApplication.postgres(writer).invoke(
                "work.message.receipt-v1", {"message_id": m, "instance_id": w.ins1, "kind": "accepted"}, w.R)
        except Exception as error:  # noqa: BLE001
            failures.append(error)

    def retire():
        try:
            WorkApplication.postgres(retirer).invoke(
                "work.agent.retire-v1", {"instance_id": w.ins1, "reason": "gone", "idempotency_key": w.key()}, w.R)
        except Exception as error:  # noqa: BLE001
            failures.append(error)
        finally:
            retired.set()

    try:
        r = threading.Thread(target=receipt)
        r.start()
        assert reached.wait(30)
        t = threading.Thread(target=retire)
        t.start()
        retired_while_held = retired.wait(1.5)
        release.set()
        r.join(30)
        t.join(30)
    finally:
        release.set()
        writer.conn.close()
        retirer.conn.close()
    assert not failures, failures
    assert not retired_while_held, "the instance retired while its receipt was being written"
    _, entry = w.instance(w.ins1)
    assert entry["registered"]["state"] == "retired"
    assert w.kinds(m) == ["accepted"]  # written before the retirement, not after it


def test_run_lookups_are_scoped_to_the_callers_workspace(w):
    """A run of the same repository in another workspace does not exist."""
    elsewhere = context("github:2:0", w.repo, "ws-other")
    foreign_run = w.new_run(elsewhere)
    expect("context-not-found", w.send, message=w.message(context={"predecessor_run_id": foreign_run}))
    m = w.sent()
    w.rcpt(m, "accepted")
    expect("run-not-found", w.rcpt, m, "run-linked", run_id=foreign_run)
    expect("run-not-found", w.rcpt, m, "launch-ambiguous", detail="x", run_id=foreign_run)
    w.rcpt(m, "run-linked", run_id=w.run)


def test_stored_digests_use_the_canonical_encoding(w):
    """The owner stamps and stores exactly the golden digests."""
    from tests.test_agent_inbox_contract import (GOLDEN_ENVELOPE, GOLDEN_ENVELOPE_BYTES,
                                                 GOLDEN_REFUSED_DIGEST, GOLDEN_REFUSED_FIELDS)

    message = {**GOLDEN_ENVELOPE, "message_id": new_message_id(), "context": {"item_id": w.item}}
    expected = hashlib.sha256(GOLDEN_ENVELOPE_BYTES.replace(
        b"msg_BBBBBBBBBBBBBBBBBBBBBBBBBB", message["message_id"].encode()).replace(
        b'"item_id":7', b'"item_id":%d' % w.item)).hexdigest()
    assert w.send(message=message)["envelope_digest"] == expected
    w.rcpt(message["message_id"], "refused", **GOLDEN_REFUSED_FIELDS)
    with w.c.store.conn.cursor() as cur:
        cur.execute("SELECT field_digest FROM agent_message_receipt WHERE repo_id=%s AND message_id=%s",
                    (w.repo, message["message_id"]))
        assert cur.fetchone()["field_digest"] == GOLDEN_REFUSED_DIGEST
    w.c.store.conn.rollback()
