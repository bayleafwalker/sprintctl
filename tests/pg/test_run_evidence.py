"""PostgreSQL integration tests: run handles, the evidence chain, session
notes and the write-tool idempotency ledger (agentops#2466, E2:
vuoro-mcp-edge record bucket -- ``work.run.*`` / ``work.evidence.*`` /
``work.session-note.*``).

These exercise the served operations through ``WorkApplication.invoke()``
(the same path vuoro_service dispatches through), not the ``sprintctl.pg``
backend functions directly, so the schema validation, identity binding and
idempotency-ledger wiring in ``work_application.py`` are covered too.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sprintctl.application import ApplicationRejection, WorkApplication

from tests.pg._shared import PG_MARKS

pytestmark = PG_MARKS


def _context(
    *,
    principal_id: str | None = "github:1:0",
    workspace_id: str | None = "ws-1",
    actor: str = "e2-test",
    request_id: str = "request-1",
):
    identity = SimpleNamespace(
        actor=actor,
        environment="vuoro-dev",
        authorities=frozenset({"work:evidence"}),
        principal_id=principal_id,
        workspace_id=workspace_id,
    )
    return SimpleNamespace(
        identity=identity,
        request_id=request_id,
        basis_revision=None,
        catalog_revision="catalog-1",
        idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


def _app(store) -> WorkApplication:
    return WorkApplication.postgres(store)


def _observed_profile() -> dict:
    return {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []}


def _validity(at: str = "2026-09-26T00:00:00Z") -> dict:
    return {
        "basis": "indefinite",
        "valid_from": at,
        "valid_until": None,
        "component_digests": {},
    }


def _register(app, *, idempotency_key, context=None, **overrides):
    args = {
        "harness_id": "claude-code",
        "harness_build": "1.0.0",
        "model_id": "claude-sonnet-5",
        "recipe_id": "recipe-1",
        "observed_profile": _observed_profile(),
        "idempotency_key": idempotency_key,
    }
    args.update(overrides)
    return app.invoke("work.run.register-v1", args, context or _context())


def _new_run(app, store_suffix: str, **kwargs) -> str:
    result = _register(app, idempotency_key=f"register-{store_suffix}", **kwargs)
    return result["run"]["run_id"]


class TestRunRegister:
    def test_register_mints_a_run_bound_to_the_caller(self, store):
        app = _app(store)
        result = _register(app, idempotency_key="register-key-mint-1")
        run = result["run"]
        assert run["run_id"].startswith("run_")
        assert len(run["run_id"]) == 30  # "run_" + 26 chars
        assert run["principal_id"] == "github:1:0"
        assert run["workspace_id"] == "ws-1"
        assert run["harness_id"] == "claude-code"
        assert run["harness_build"] == "1.0.0"
        assert run["model_id"] == "claude-sonnet-5"
        assert run["recipe_id"] == "recipe-1"
        assert run["observed_profile"] == _observed_profile()
        assert run["grant_ids"] == []
        assert run["claim_ids"] == []
        assert result["repo_id"] == store.repo_id

    def test_same_key_and_arguments_replay_the_same_run_with_no_second_row(self, store):
        app = _app(store)
        first = _register(app, idempotency_key="register-key-replay-1")
        second = _register(app, idempotency_key="register-key-replay-1")
        assert second["run"]["run_id"] == first["run"]["run_id"]
        assert second == first

    def test_same_key_different_arguments_is_an_idempotency_conflict(self, store):
        app = _app(store)
        _register(app, idempotency_key="register-key-conflict-1")
        with pytest.raises(ApplicationRejection) as excinfo:
            _register(
                app, idempotency_key="register-key-conflict-1", model_id="a-different-model"
            )
        assert excinfo.value.code == "idempotency-conflict"
        assert excinfo.value.http_status == 409

    def test_different_principals_reusing_a_key_mint_independent_runs(self, store):
        # register_run's own binding key includes principal_id (unlike the
        # generic idempotency ledger, which is workspace-scoped only -- see
        # the E2 final report for the cross-principal caveat that follows
        # from the shared contract's literal (workspace, tool, key) scoping).
        app = _app(store)
        first = _register(
            app, idempotency_key="register-key-shared-9",
            context=_context(principal_id="github:1:0"),
        )
        second = _register(
            app, idempotency_key="register-key-shared-9",
            context=_context(principal_id="github:2:0"),
        )
        assert first["run"]["run_id"] != second["run"]["run_id"]
        assert second["run"]["principal_id"] == "github:2:0"

    def test_caller_with_no_bound_identity_is_refused(self, store):
        app = _app(store)
        context = _context(principal_id=None)
        with pytest.raises(ApplicationRejection) as excinfo:
            _register(app, idempotency_key="register-key-unbound-1", context=context)
        assert excinfo.value.code == "identity-unbound"
        assert excinfo.value.http_status == 403


class TestRunResolve:
    def test_resolve_returns_the_caller_s_own_binding(self, store):
        app = _app(store)
        run_id = _new_run(app, "resolve-1")
        result = app.invoke("work.run.resolve-v1", {"run_id": run_id}, _context())
        assert result["run_id"] == run_id
        assert result["principal_id"] == "github:1:0"
        assert result["workspace_id"] == "ws-1"

    def test_unknown_run_id_is_run_not_found(self, store):
        app = _app(store)
        unknown = "run_" + "0" * 26
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke("work.run.resolve-v1", {"run_id": unknown}, _context())
        assert excinfo.value.code == "run-not-found"
        assert excinfo.value.http_status == 404

    def test_a_run_bound_to_a_different_principal_is_the_same_run_not_found(self, store):
        app = _app(store)
        run_id = _new_run(app, "resolve-2")
        other = _context(principal_id="github:9:0")
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke("work.run.resolve-v1", {"run_id": run_id}, other)
        assert excinfo.value.code == "run-not-found"

    def test_a_run_bound_to_a_different_workspace_is_the_same_run_not_found(self, store):
        app = _app(store)
        run_id = _new_run(app, "resolve-3")
        other = _context(workspace_id="ws-other")
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke("work.run.resolve-v1", {"run_id": run_id}, other)
        assert excinfo.value.code == "run-not-found"


class TestEvidenceChain:
    def test_tail_of_a_fresh_run_is_none(self, store):
        app = _app(store)
        run_id = _new_run(app, "evidence-tail-1")
        result = app.invoke("work.evidence.tail-v1", {"run_id": run_id}, _context())
        assert result["item"] is None

    def test_appends_extend_the_chain_in_order(self, store):
        app = _app(store)
        run_id = _new_run(app, "evidence-chain-1")
        first = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id,
                "item_id": "evidence_1",
                "kind": "test",
                "ref": "ref-1",
                "digest": "sha256:" + "b" * 64,
                "collector": "tester",
                "validity": _validity(),
                "claims": [],
                "provenance": {},
                "chain_seq": 0,
                "chain_prev_digest": None,
                "idempotency_key": "evidence-append-key-1",
            },
            _context(),
        )
        assert first["item"]["chain_seq"] == 0
        assert first["item"]["chain_prev_digest"] is None

        second = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id,
                "item_id": "evidence_2",
                "kind": "test",
                "ref": "ref-2",
                "digest": "sha256:" + "c" * 64,
                "collector": "tester",
                "validity": _validity("2026-09-26T00:01:00Z"),
                "claims": [],
                "provenance": {},
                "chain_seq": 1,
                "chain_prev_digest": "sha256:" + "d" * 64,
                "idempotency_key": "evidence-append-key-2",
            },
            _context(),
        )
        assert second["item"]["chain_seq"] == 1
        assert second["item"]["chain_prev_digest"] == "sha256:" + "d" * 64

        tail = app.invoke("work.evidence.tail-v1", {"run_id": run_id}, _context())
        assert tail["item"]["item_id"] == "evidence_2"
        assert tail["item"]["chain_seq"] == 1

    def test_a_stale_chain_seq_is_refused_as_a_chain_conflict(self, store):
        app = _app(store)
        run_id = _new_run(app, "evidence-conflict-1")
        app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_a", "kind": "test", "ref": "ref-a",
                "digest": "sha256:" + "1" * 64, "collector": "tester", "validity": _validity(),
                "claims": [], "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
                "idempotency_key": "evidence-conflict-key-1",
            },
            _context(),
        )
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke(
                "work.evidence.append-v1",
                {
                    "run_id": run_id, "item_id": "evidence_b", "kind": "test", "ref": "ref-b",
                    "digest": "sha256:" + "2" * 64, "collector": "tester", "validity": _validity(),
                    # Stale: 0 was already taken, the caller should have
                    # re-fetched the tail and submitted 1.
                    "claims": [], "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
                    "idempotency_key": "evidence-conflict-key-2",
                },
                _context(),
            )
        assert excinfo.value.code == "evidence-chain-conflict"
        assert excinfo.value.http_status == 409

    def test_a_conflicting_append_can_be_retried_under_the_same_idempotency_key(self, store):
        """A caller that recomputed against a fresher tail after a chain
        conflict may reuse the same idempotency_key: the failed attempt was
        never recorded in the ledger, so the retry is not itself treated as
        an idempotency conflict."""
        app = _app(store)
        run_id = _new_run(app, "evidence-conflict-retry-1")
        app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_a", "kind": "test", "ref": "ref-a",
                "digest": "sha256:" + "3" * 64, "collector": "tester", "validity": _validity(),
                "claims": [], "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
                "idempotency_key": "evidence-retry-key-1",
            },
            _context(),
        )
        key = "evidence-retry-key-2"
        with pytest.raises(ApplicationRejection):
            app.invoke(
                "work.evidence.append-v1",
                {
                    "run_id": run_id, "item_id": "evidence_b", "kind": "test", "ref": "ref-b",
                    "digest": "sha256:" + "4" * 64, "collector": "tester", "validity": _validity(),
                    "claims": [], "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
                    "idempotency_key": key,
                },
                _context(),
            )
        retried = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_b", "kind": "test", "ref": "ref-b",
                "digest": "sha256:" + "4" * 64, "collector": "tester", "validity": _validity(),
                "claims": [], "provenance": {}, "chain_seq": 1, "chain_prev_digest": "sha256:" + "5" * 64,
                "idempotency_key": key,
            },
            _context(),
        )
        assert retried["item"]["chain_seq"] == 1

    def test_a_successful_append_replays_cleanly_even_if_the_chain_moved_since(
        self, store
    ):
        """A retry of an already-committed append must not be judged against
        chain_seq/chain_prev_digest: those are computed by the edge from the
        tail it observed, not supplied by the original tool caller, and an
        unrelated concurrent append moves the tail between the original call
        and a client-side retry. The retry recomputes a *different*
        chain_seq/chain_prev_digest (as the real edge would) but must still
        replay the original stored item, not conflict and not double-append.
        """
        app = _app(store)
        run_id = _new_run(app, "evidence-replay-despite-move")
        key = "evidence-replay-key-1"
        first = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_first", "kind": "test",
                "ref": "ref-first", "digest": "sha256:" + "6" * 64, "collector": "tester",
                "validity": _validity(), "claims": [], "provenance": {},
                "chain_seq": 0, "chain_prev_digest": None, "idempotency_key": key,
            },
            _context(),
        )
        # An unrelated append (different key, different item) moves the tail.
        app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_other", "kind": "test",
                "ref": "ref-other", "digest": "sha256:" + "7" * 64, "collector": "tester",
                "validity": _validity(), "claims": [], "provenance": {},
                "chain_seq": 1, "chain_prev_digest": "sha256:" + "8" * 64,
                "idempotency_key": "evidence-replay-key-unrelated",
            },
            _context(),
        )
        # The retry: same item_id and key as the first call (as the edge
        # would resend for the same logical request), but a freshly
        # recomputed chain_seq/chain_prev_digest against the now-moved tail.
        retried = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_first", "kind": "test",
                "ref": "ref-first", "digest": "sha256:" + "6" * 64, "collector": "tester",
                "validity": _validity(), "claims": [], "provenance": {},
                "chain_seq": 2, "chain_prev_digest": "sha256:" + "9" * 64,
                "idempotency_key": key,
            },
            _context(),
        )
        assert retried == first
        assert retried["item"]["chain_seq"] == 0
        tail = app.invoke("work.evidence.tail-v1", {"run_id": run_id}, _context())
        # No third row: the replay performed no second effect.
        assert tail["item"]["item_id"] == "evidence_other"

    def test_appending_to_a_run_owned_by_another_caller_is_run_not_found(self, store):
        app = _app(store)
        run_id = _new_run(app, "evidence-owner-1")
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke(
                "work.evidence.append-v1",
                {
                    "run_id": run_id, "item_id": "evidence_x", "kind": "test", "ref": "ref-x",
                    "digest": "sha256:" + "6" * 64, "collector": "tester", "validity": _validity(),
                    "claims": [], "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
                    "idempotency_key": "evidence-owner-key-1",
                },
                _context(principal_id="github:9:0"),
            )
        assert excinfo.value.code == "run-not-found"

    def test_claims_and_provenance_round_trip(self, store):
        app = _app(store)
        run_id = _new_run(app, "evidence-claims-1")
        claim = {
            "claim_type": "observation",
            "subject": "effect-1",
            "grant_id": None,
            "freshness": {"scope": "repo", "position": 3},
            "confirms": True,
            "detail": {"note": "looked fine"},
        }
        result = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_claims", "kind": "test", "ref": "ref-c",
                "digest": "sha256:" + "7" * 64, "collector": "tester", "validity": _validity(),
                "claims": [claim], "provenance": {"session": "abc"}, "chain_seq": 0,
                "chain_prev_digest": None, "idempotency_key": "evidence-claims-key-1",
            },
            _context(),
        )
        assert result["item"]["claims"] == [claim]
        assert result["item"]["provenance"] == {"session": "abc"}


class TestSessionNote:
    def test_write_creates_a_note_bound_to_the_run(self, store):
        app = _app(store)
        run_id = _new_run(app, "note-1")
        result = app.invoke(
            "work.session-note.write-v1",
            {"run_id": run_id, "note": "started work", "idempotency_key": "note-key-1"},
            _context(),
        )
        assert result["run_id"] == run_id
        assert result["note"] == "started work"
        assert result["note_id"] >= 1

    def test_same_key_and_note_replays_without_a_second_row(self, store):
        app = _app(store)
        run_id = _new_run(app, "note-2")
        first = app.invoke(
            "work.session-note.write-v1",
            {"run_id": run_id, "note": "same note", "idempotency_key": "note-key-2"},
            _context(),
        )
        second = app.invoke(
            "work.session-note.write-v1",
            {"run_id": run_id, "note": "same note", "idempotency_key": "note-key-2"},
            _context(),
        )
        assert second["note_id"] == first["note_id"]

    def test_writing_to_a_run_owned_by_another_caller_is_run_not_found(self, store):
        app = _app(store)
        run_id = _new_run(app, "note-3")
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke(
                "work.session-note.write-v1",
                {"run_id": run_id, "note": "not yours", "idempotency_key": "note-key-3"},
                _context(principal_id="github:9:0"),
            )
        assert excinfo.value.code == "run-not-found"


class TestIdempotencyLedgerCrossTool:
    def test_the_same_key_string_used_by_two_different_tools_does_not_collide(self, store):
        """The ledger is keyed by (workspace, tool, key); the same literal
        key string is a distinct row per tool."""
        app = _app(store)
        shared_key = "shared-across-tools-1"
        run_result = _register(app, idempotency_key=shared_key)
        run_id = run_result["run"]["run_id"]
        note_result = app.invoke(
            "work.session-note.write-v1",
            {"run_id": run_id, "note": "distinct ledger row", "idempotency_key": shared_key},
            _context(),
        )
        assert note_result["run_id"] == run_id
        # Replaying register with the same key still returns the run, not
        # something confused with the note tool's row.
        replay = _register(app, idempotency_key=shared_key)
        assert replay["run"]["run_id"] == run_id
