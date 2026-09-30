"""Oracle (agentops#2541, M2-1): the sprintctl-owned effect-intent lifecycle.

Operator Decision 1 (A) on agentops#253 and INV-E1: sprintctl owns effect
intents beside the work, runs and leases they describe, and serves
``work.effect.{propose,get,list-proposed,accept,reject,mark-applied}-v1``.

What these tests hold the authority to (through ``WorkApplication.invoke``,
like tests/pg/test_work_lease.py, so identity binding, the write-tool
ledger and the rejection mapping are covered):

* a proposal is bound to the caller's own run and an existing item, gets
  ``revision`` and a ``canonical_intent_digest`` that is a function of its
  content (not of the idempotency key), and is idempotent per key;
* accept/reject/mark-applied are compare-and-set on the exact ``revision``
  and ``canonical_intent_digest``; a stale revision or mismatched digest is
  refused and changes nothing;
* each transition needs its own ``work.effect.*`` capability, so a principal
  with ordinary work authority (``work:write``, ``work:claim``, ...) but not
  ``work.effect.accept`` cannot accept;
* the acceptance record binds ``intent_id``, ``intent_revision``,
  ``canonical_intent_digest``, the authenticated ``acceptor_principal``
  (never a wire argument), ``acceptor_policy_version`` and ``accepted_at``;
* an accepted intent is immutable in storage (a database trigger refuses a
  content change on the ``work_effect_intent`` row);
* accepting an intent never changes the work item, and settling the work
  never accepts an intent.

Rejection codes pinned here: ``authority-required`` (403),
``effect-not-found`` (404), ``effect-revision-mismatch`` (409),
``effect-digest-mismatch`` (409), ``effect-invalid-transition`` (409),
``idempotency-conflict`` (409), ``run-not-found`` (404),
``work-not-found`` (404), ``invalid-arguments`` (422).
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from types import SimpleNamespace

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

N_RACERS = 8
_CONTRACTS = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}

PROPOSE = "work.effect.propose-v1"
GET = "work.effect.get-v1"
LIST_PROPOSED = "work.effect.list-proposed-v1"
ACCEPT = "work.effect.accept-v1"
REJECT = "work.effect.reject-v1"
MARK_APPLIED = "work.effect.mark-applied-v1"

_READ_EFFECTS = {"work:read", "work.effect.get", "work.effect.list-proposed"}
_ORDINARY_WORK = {"work:read", "work:write", "work:claim", "work:lifecycle", "work:evidence", "work:sprint"}


def _context(principal_id: str, authorities: set[str], *, workspace_id: str = "ws-1"):
    identity = SimpleNamespace(
        actor=f"actor-{principal_id}",
        environment="vuoro-dev",
        authorities=frozenset(authorities),
        principal_id=principal_id,
        workspace_id=workspace_id,
        client_id=None,
        grant_id=None,
    )
    return SimpleNamespace(
        identity=identity, request_id="request-1", basis_revision=None,
        catalog_revision="catalog-1", idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


# The proposer: a hosted worker that may claim work, record evidence and
# propose effects, but holds no acceptance authority.
PROPOSER = _context(
    "github:100:0", {"work:read", "work:claim", "work:evidence", "work.effect.propose"} | _READ_EFFECTS
)
OTHER_PROPOSER = _context(
    "github:200:0", {"work:read", "work:claim", "work:evidence", "work.effect.propose"} | _READ_EFFECTS
)
# The trusted-side acceptor (operator / reconciler identity).
ACCEPTOR = _context(
    "github:900:0",
    {"work.effect.accept", "work.effect.reject", "work.effect.mark-applied"} | _READ_EFFECTS,
)
SECOND_ACCEPTOR = _context(
    "github:901:0",
    {"work.effect.accept", "work.effect.reject", "work.effect.mark-applied"} | _READ_EFFECTS,
)


def _app(store) -> WorkApplication:
    return WorkApplication.postgres(store)


def _invoke(store, operation: str, arguments: dict, context) -> dict:
    """Invoke with arguments the published input schema accepts, and hold the
    result to the published result schema."""
    jsonschema.validate(arguments, _CONTRACTS[operation].input_schema)
    result = _app(store).invoke(operation, arguments, context)
    jsonschema.validate(result, _CONTRACTS[operation].result_schema)
    return result


def _refused(call) -> ApplicationRejection:
    with pytest.raises(ApplicationRejection) as excinfo:
        call()
    return excinfo.value


def _run(store, context) -> str:
    result = _app(store).invoke("work.run.register-v1", {
        "harness_id": "claude-code", "harness_build": "1.0.0", "model_id": "m",
        "recipe_id": "r",
        "observed_profile": {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []},
        "idempotency_key": f"run-{uuid.uuid4().hex}",
    }, context)
    return result["run"]["run_id"]


def _items(store, count: int = 1) -> list[int]:
    sprint_id = pg.create_sprint(store, f"Effect-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")
    track_id = pg.get_or_create_track(store, sprint_id, "effect")
    return [pg.create_work_item(store, sprint_id, track_id, f"item {i}") for i in range(count)]


def _diff(word: str = "the") -> str:
    return f"--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-teh\n+{word}\n"


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _propose_args(item_id: int, run_id: str, key: str, **overrides) -> dict:
    args = {
        "run_id": run_id,
        "item_id": item_id,
        "repository": "vuoro-e3-canary",
        "base_commit": "b" * 40,
        "title": "Fix a typo",
        "rationale": "The README misspells 'the'.",
        "unified_diff": _diff(),
        "idempotency_key": key,
    }
    args.update(overrides)
    return args


def _propose(store, item_id: int, run_id: str, key: str | None = None, *, context=PROPOSER,
             **overrides) -> dict:
    result = _invoke(store, PROPOSE, _propose_args(item_id, run_id, key or _key("propose"), **overrides),
                     context)
    return result["intent"]


def _get(store, intent_id: str, context=ACCEPTOR) -> dict:
    return _invoke(store, GET, {"intent_id": intent_id}, context)["intent"]


def _list_proposed(store, context=ACCEPTOR) -> list[dict]:
    return _invoke(store, LIST_PROPOSED, {}, context)["intents"]


def _binding(intent: dict, **overrides) -> dict:
    binding = {
        "intent_id": intent["intent_id"],
        "revision": intent["revision"],
        "canonical_intent_digest": intent["canonical_intent_digest"],
    }
    binding.update(overrides)
    return binding


def _accept(store, intent: dict, context=ACCEPTOR, **overrides) -> dict:
    return _invoke(store, ACCEPT, _binding(intent, **overrides), context)["intent"]


def _reject(store, intent: dict, context=ACCEPTOR, reason: str = "not wanted", **overrides) -> dict:
    return _invoke(store, REJECT, {**_binding(intent, **overrides), "reason": reason}, context)["intent"]


def _mark_applied(store, intent: dict, context=ACCEPTOR, **overrides) -> dict:
    args = {
        **_binding(intent, **overrides),
        "commit_sha": "c" * 40,
        "pr_url": "https://git.example.invalid/vuoro-e3-canary/pulls/1",
    }
    return _invoke(store, MARK_APPLIED, args, context)["intent"]


def _other_digest(digest: str) -> str:
    return ("0" if digest[0] != "0" else "1") + digest[1:]


def _status(store, item_id: int) -> str:
    return pg.get_work_item(store, item_id)["status"]


def _fresh(store):
    (item,) = _items(store)
    run = _run(store, PROPOSER)
    return item, run, _propose(store, item, run)


def _sibling(store) -> pg.PgStore:
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return pg.PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)


class TestPropose:
    def test_a_proposal_is_recorded_as_proposed_with_revision_and_digest(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        intent = _propose(store, item, run)
        assert isinstance(intent["intent_id"], str) and intent["intent_id"]
        assert intent["state"] == "proposed"
        assert isinstance(intent["revision"], int) and intent["revision"] >= 1
        digest = intent["canonical_intent_digest"]
        assert isinstance(digest, str) and len(digest) == 64
        assert all(ch in "0123456789abcdef" for ch in digest)
        assert intent["item_id"] == item and intent["run_id"] == run
        assert intent["proposer_principal"] == "github:100:0"
        assert intent["repository"] == "vuoro-e3-canary"
        assert intent["unified_diff"] == _diff()
        assert intent["acceptance"] is None

    def test_get_returns_the_same_intent_to_the_trusted_side(self, store):
        _item, _run_id, intent = _fresh(store)
        got = _get(store, intent["intent_id"], ACCEPTOR)
        for field in ("intent_id", "revision", "canonical_intent_digest", "state", "item_id",
                      "unified_diff", "proposer_principal"):
            assert got[field] == intent[field], field

    def test_the_digest_is_a_function_of_content_not_of_the_key(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        first = _propose(store, item, run, _key("digest-a"))
        same = _propose(store, item, run, _key("digest-b"))
        other_diff = _propose(store, item, run, _key("digest-c"), unified_diff=_diff("thee"))
        other_title = _propose(store, item, run, _key("digest-d"), title="Fix another typo")
        assert same["intent_id"] != first["intent_id"]
        assert same["canonical_intent_digest"] == first["canonical_intent_digest"]
        assert other_diff["canonical_intent_digest"] != first["canonical_intent_digest"]
        assert other_title["canonical_intent_digest"] != first["canonical_intent_digest"]

    def test_the_same_key_replays_one_proposal(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        key = _key("replay")
        first = _propose(store, item, run, key)
        again = _propose(store, item, run, key)
        assert again["intent_id"] == first["intent_id"]
        assert again["canonical_intent_digest"] == first["canonical_intent_digest"]
        ids = [i["intent_id"] for i in _list_proposed(store)]
        assert ids.count(first["intent_id"]) == 1

    def test_the_same_key_with_other_content_is_an_idempotency_conflict(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        key = _key("conflict")
        first = _propose(store, item, run, key)
        refused = _refused(lambda: _propose(store, item, run, key, unified_diff=_diff("thee")))
        assert (refused.code, refused.http_status) == ("idempotency-conflict", 409)
        assert _get(store, first["intent_id"])["unified_diff"] == _diff()

    def test_a_run_that_is_not_the_callers_is_run_not_found(self, store):
        (item,) = _items(store)
        run_b = _run(store, OTHER_PROPOSER)
        refused = _refused(lambda: _propose(store, item, run_b))
        assert (refused.code, refused.http_status) == ("run-not-found", 404)

    def test_an_unknown_item_is_work_not_found(self, store):
        run = _run(store, PROPOSER)
        refused = _refused(lambda: _propose(store, 10**9, run))
        assert (refused.code, refused.http_status) == ("work-not-found", 404)

    def test_propose_needs_the_propose_capability(self, store):
        (item,) = _items(store)
        no_propose = _context("github:300:0", _ORDINARY_WORK | _READ_EFFECTS)
        run = _run(store, no_propose)
        refused = _refused(lambda: _propose(store, item, run, context=no_propose))
        assert (refused.code, refused.http_status) == ("authority-required", 403)
        assert not [i for i in _list_proposed(store) if i["item_id"] == item]


class TestReads:
    def test_an_unknown_intent_is_effect_not_found(self, store):
        refused = _refused(lambda: _get(store, "intent-" + uuid.uuid4().hex))
        assert (refused.code, refused.http_status) == ("effect-not-found", 404)

    def test_list_proposed_shows_only_proposed_intents(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        waiting = _propose(store, item, run, title="waiting")
        accepted = _propose(store, item, run, title="accepted")
        rejected = _propose(store, item, run, title="rejected")
        _accept(store, accepted)
        _reject(store, rejected)
        listed = {i["intent_id"]: i for i in _list_proposed(store)}
        assert waiting["intent_id"] in listed
        assert accepted["intent_id"] not in listed
        assert rejected["intent_id"] not in listed
        assert all(i["state"] == "proposed" for i in listed.values())
        entry = listed[waiting["intent_id"]]
        assert entry["revision"] == waiting["revision"]
        assert entry["canonical_intent_digest"] == waiting["canonical_intent_digest"]


class TestAccept:
    def test_accept_binds_revision_digest_and_the_authenticated_principal(self, store):
        _item, _run_id, intent = _fresh(store)
        accepted = _accept(store, intent)
        assert accepted["state"] == "accepted"
        assert accepted["intent_id"] == intent["intent_id"]
        assert accepted["revision"] == intent["revision"]
        assert accepted["canonical_intent_digest"] == intent["canonical_intent_digest"]
        record = accepted["acceptance"]
        assert record["intent_id"] == intent["intent_id"]
        assert record["intent_revision"] == intent["revision"]
        assert record["canonical_intent_digest"] == intent["canonical_intent_digest"]
        assert record["acceptor_principal"] == "github:900:0"
        assert record["acceptor_policy_version"] is None
        datetime.fromisoformat(record["accepted_at"].replace("Z", "+00:00"))
        # The record is durable: a later read returns the same binding.
        assert _get(store, intent["intent_id"], PROPOSER)["acceptance"] == record

    def test_a_stale_or_wrong_revision_is_refused_and_changes_nothing(self, store):
        _item, _run_id, intent = _fresh(store)
        for revision in (intent["revision"] + 1, intent["revision"] + 7):
            refused = _refused(lambda r=revision: _accept(store, intent, revision=r))
            assert (refused.code, refused.http_status) == ("effect-revision-mismatch", 409)
        if intent["revision"] > 1:
            refused = _refused(lambda: _accept(store, intent, revision=intent["revision"] - 1))
            assert refused.code == "effect-revision-mismatch"
        current = _get(store, intent["intent_id"])
        assert current["state"] == "proposed" and current["acceptance"] is None

    def test_a_mismatched_digest_is_refused_and_changes_nothing(self, store):
        _item, _run_id, intent = _fresh(store)
        wrong = _other_digest(intent["canonical_intent_digest"])
        refused = _refused(lambda: _accept(store, intent, canonical_intent_digest=wrong))
        assert (refused.code, refused.http_status) == ("effect-digest-mismatch", 409)
        current = _get(store, intent["intent_id"])
        assert current["state"] == "proposed" and current["acceptance"] is None

    def test_the_digest_of_another_proposal_cannot_accept_this_one(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        intent = _propose(store, item, run)
        other = _propose(store, item, run, unified_diff=_diff("thee"))
        refused = _refused(
            lambda: _accept(store, intent, canonical_intent_digest=other["canonical_intent_digest"])
        )
        assert refused.code == "effect-digest-mismatch"
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_ordinary_work_authority_cannot_accept(self, store):
        """A principal that may change ordinary work state (and even reject
        or mark applied) does not thereby hold acceptance authority."""
        _item, _run_id, intent = _fresh(store)
        writer = _context(
            "github:400:0",
            _ORDINARY_WORK | _READ_EFFECTS
            | {"work.effect.propose", "work.effect.reject", "work.effect.mark-applied"},
        )
        refused = _refused(lambda: _accept(store, intent, writer))
        assert (refused.code, refused.http_status) == ("authority-required", 403)
        current = _get(store, intent["intent_id"])
        assert current["state"] == "proposed" and current["acceptance"] is None

    def test_the_acceptor_cannot_be_self_asserted(self, store):
        _item, _run_id, intent = _fresh(store)
        args = {**_binding(intent), "acceptor_principal": "github:999:0"}
        refused = _refused(lambda: _app(store).invoke(ACCEPT, args, ACCEPTOR))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_an_accepted_intent_cannot_be_accepted_again_or_rejected(self, store):
        _item, _run_id, intent = _fresh(store)
        record = _accept(store, intent)["acceptance"]
        current = _get(store, intent["intent_id"])
        refused = _refused(lambda: _accept(store, current, SECOND_ACCEPTOR))
        assert (refused.code, refused.http_status) == ("effect-invalid-transition", 409)
        refused = _refused(lambda: _reject(store, current))
        assert (refused.code, refused.http_status) == ("effect-invalid-transition", 409)
        after = _get(store, intent["intent_id"])
        assert after["state"] == "accepted" and after["acceptance"] == record

    def test_eight_racing_acceptances_accept_exactly_once(self, store):
        _item, _run_id, intent = _fresh(store)
        acceptors = [
            _context(f"github:race-{i}:0", {"work.effect.accept"} | _READ_EFFECTS)
            for i in range(N_RACERS)
        ]
        stores = [_sibling(store) for _ in acceptors]
        barrier = threading.Barrier(N_RACERS)
        outcomes: list = [None] * N_RACERS

        def run(index):
            try:
                barrier.wait(timeout=10)
                outcomes[index] = _accept(stores[index], intent, acceptors[index])
            except BaseException as exc:  # noqa: BLE001 - the outcome under test
                outcomes[index] = exc

        threads = [threading.Thread(target=run, args=(i,)) for i in range(N_RACERS)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        finally:
            for s in stores:
                s.conn.close()
        assert not any(thread.is_alive() for thread in threads)
        ok = [o for o in outcomes if isinstance(o, dict)]
        refused = [o.code for o in outcomes if isinstance(o, ApplicationRejection)]
        other = [o for o in outcomes if not isinstance(o, (dict, ApplicationRejection))]
        assert not other, [repr(o) for o in other]
        assert len(ok) == 1
        assert refused == ["effect-invalid-transition"] * (N_RACERS - 1)
        winner = ok[0]["acceptance"]["acceptor_principal"]
        assert _get(store, intent["intent_id"])["acceptance"]["acceptor_principal"] == winner


class TestReject:
    def test_reject_moves_proposed_to_rejected(self, store):
        _item, _run_id, intent = _fresh(store)
        rejected = _reject(store, intent, reason="out of scope")
        assert rejected["state"] == "rejected"
        assert rejected["acceptance"] is None
        assert _get(store, intent["intent_id"])["state"] == "rejected"

    def test_reject_is_bound_to_revision_and_digest(self, store):
        _item, _run_id, intent = _fresh(store)
        refused = _refused(lambda: _reject(store, intent, revision=intent["revision"] + 1))
        assert refused.code == "effect-revision-mismatch"
        wrong = _other_digest(intent["canonical_intent_digest"])
        refused = _refused(lambda: _reject(store, intent, canonical_intent_digest=wrong))
        assert refused.code == "effect-digest-mismatch"
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_reject_needs_the_reject_capability(self, store):
        _item, _run_id, intent = _fresh(store)
        accept_only = _context("github:500:0", _ORDINARY_WORK | _READ_EFFECTS | {"work.effect.accept"})
        refused = _refused(lambda: _reject(store, intent, accept_only))
        assert (refused.code, refused.http_status) == ("authority-required", 403)
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_a_rejected_intent_cannot_be_accepted(self, store):
        _item, _run_id, intent = _fresh(store)
        _reject(store, intent)
        current = _get(store, intent["intent_id"])
        refused = _refused(lambda: _accept(store, current))
        assert (refused.code, refused.http_status) == ("effect-invalid-transition", 409)
        assert _get(store, intent["intent_id"])["state"] == "rejected"


class TestMarkApplied:
    def test_an_accepted_intent_is_marked_applied_keeping_its_acceptance(self, store):
        _item, _run_id, intent = _fresh(store)
        record = _accept(store, intent)["acceptance"]
        applied = _mark_applied(store, _get(store, intent["intent_id"]))
        assert applied["state"] == "applied"
        assert applied["canonical_intent_digest"] == intent["canonical_intent_digest"]
        assert applied["acceptance"] == record
        assert _get(store, intent["intent_id"])["state"] == "applied"

    def test_a_proposed_intent_cannot_be_marked_applied(self, store):
        _item, _run_id, intent = _fresh(store)
        refused = _refused(lambda: _mark_applied(store, intent))
        assert (refused.code, refused.http_status) == ("effect-invalid-transition", 409)
        assert _get(store, intent["intent_id"])["state"] == "proposed"

    def test_mark_applied_is_bound_to_the_accepted_digest(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        current = _get(store, intent["intent_id"])
        wrong = _other_digest(current["canonical_intent_digest"])
        refused = _refused(lambda: _mark_applied(store, current, canonical_intent_digest=wrong))
        assert (refused.code, refused.http_status) == ("effect-digest-mismatch", 409)
        refused = _refused(lambda: _mark_applied(store, current, revision=current["revision"] + 1))
        assert (refused.code, refused.http_status) == ("effect-revision-mismatch", 409)
        assert _get(store, intent["intent_id"])["state"] == "accepted"

    def test_mark_applied_needs_the_mark_applied_capability(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        current = _get(store, intent["intent_id"])
        no_apply = _context("github:600:0", _ORDINARY_WORK | _READ_EFFECTS | {"work.effect.accept"})
        refused = _refused(lambda: _mark_applied(store, current, no_apply))
        assert (refused.code, refused.http_status) == ("authority-required", 403)
        assert _get(store, intent["intent_id"])["state"] == "accepted"

    def test_an_applied_intent_cannot_be_applied_or_accepted_again(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        _mark_applied(store, _get(store, intent["intent_id"]))
        current = _get(store, intent["intent_id"])
        for call in (lambda: _mark_applied(store, current), lambda: _accept(store, current)):
            refused = _refused(call)
            assert (refused.code, refused.http_status) == ("effect-invalid-transition", 409)


class TestImmutability:
    """INV-E1: an accepted intent is immutable; a change is a new proposal."""

    def _stored_digest(self, store, intent_id: str) -> str:
        """The digest column of the intent's storage row (one row, by id)."""
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_intent_digest FROM work_effect_intent "
                "WHERE repo_id = %s AND intent_id::text = %s",
                (store.repo_id, intent_id),
            )
            rows = cur.fetchall()
        store.conn.rollback()
        assert len(rows) == 1, rows
        return rows[0]["canonical_intent_digest"]

    def _update_digest(self, store, intent_id: str) -> None:
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE work_effect_intent SET canonical_intent_digest = %s "
                "WHERE repo_id = %s AND intent_id::text = %s",
                ("f" * 64, store.repo_id, intent_id),
            )
        store.conn.commit()

    def _assert_storage_refuses(self, store, intent: dict) -> None:
        # The row and column exist, so a refusal below is the trigger's and
        # not a missing-table or missing-column error.
        assert self._stored_digest(store, intent["intent_id"]) == intent["canonical_intent_digest"]
        try:
            with pytest.raises(psycopg.Error) as excinfo:
                self._update_digest(store, intent["intent_id"])
        finally:
            store.conn.rollback()
        assert not isinstance(
            excinfo.value,
            (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn,
             psycopg.errors.InsufficientPrivilege),
        ), repr(excinfo.value)
        assert self._stored_digest(store, intent["intent_id"]) == intent["canonical_intent_digest"]

    def test_storage_refuses_to_change_an_accepted_intent(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        self._assert_storage_refuses(store, intent)
        after = _get(store, intent["intent_id"])
        assert after["canonical_intent_digest"] == intent["canonical_intent_digest"]
        assert after["acceptance"]["canonical_intent_digest"] == intent["canonical_intent_digest"]

    def test_storage_refuses_to_change_an_applied_intent(self, store):
        _item, _run_id, intent = _fresh(store)
        _accept(store, intent)
        _mark_applied(store, _get(store, intent["intent_id"]))
        self._assert_storage_refuses(store, intent)
        assert _get(store, intent["intent_id"])["canonical_intent_digest"] == intent["canonical_intent_digest"]

    def test_a_changed_proposal_is_a_new_intent_and_leaves_the_accepted_one(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        original = _propose(store, item, run)
        record = _accept(store, original)["acceptance"]
        changed = _propose(store, item, run, unified_diff=_diff("thee"))
        assert changed["intent_id"] != original["intent_id"]
        assert changed["state"] == "proposed" and changed["acceptance"] is None
        assert changed["canonical_intent_digest"] != original["canonical_intent_digest"]
        kept = _get(store, original["intent_id"])
        assert kept["state"] == "accepted" and kept["acceptance"] == record
        assert kept["unified_diff"] == _diff()


class TestSeparateFromSettlement:
    """Accepting a proposal never settles work; settling work never accepts."""

    def test_accepting_an_intent_on_a_claimed_item_leaves_the_item_alone(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        lease = _app(store).invoke("work.lease.acquire-v1", {
            "item_id": item, "run_id": run, "idempotency_key": _key("lease"),
        }, PROPOSER)["lease"]
        assert _status(store, item) == "active"
        intent = _propose(store, item, run)
        _accept(store, intent)
        assert _status(store, item) == "active"
        current = _app(store).invoke("work.lease.read-v1", {"item_id": item}, PROPOSER)["current_lease"]
        assert current is not None and current["lease_id"] == lease["lease_id"]

    def test_settling_the_item_leaves_a_proposed_intent_proposed(self, store):
        (item,) = _items(store)
        run = _run(store, PROPOSER)
        intent = _propose(store, item, run)
        pg.set_work_item_status(store, item, "active")
        pg.record_decision(store, item, "accept", actor="operator")
        assert _status(store, item) == "done"
        after = _get(store, intent["intent_id"])
        assert after["state"] == "proposed" and after["acceptance"] is None
        assert intent["intent_id"] in {i["intent_id"] for i in _list_proposed(store)}
