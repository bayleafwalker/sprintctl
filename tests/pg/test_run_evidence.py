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

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    assert_disposable_connection,
    dict_row,
    psycopg,
)

pytestmark = PG_MARKS


def _context(
    *,
    principal_id: str | None = "github:1:0",
    workspace_id: str | None = "ws-1",
    client_id: str | None = None,
    grant_id: str | None = None,
    actor: str = "e2-test",
    request_id: str = "request-1",
):
    identity = SimpleNamespace(
        actor=actor,
        environment="vuoro-dev",
        authorities=frozenset({"work:evidence"}),
        principal_id=principal_id,
        workspace_id=workspace_id,
        client_id=client_id,
        grant_id=grant_id,
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


class TestRunGrantBinding:
    """A run binds the caller's OAuth client and grant too (E2/E3 shared
    contract section 4; vuoro_mcp_edge.runs.RunBinding), and resolve echoes
    them so the edge's own binding comparison is not inert."""

    GRANT_A = {"client_id": "client-1", "grant_id": "grant-a"}
    GRANT_B = {"client_id": "client-1", "grant_id": "grant-b"}

    def test_register_persists_client_and_grant_and_resolve_echoes_them(self, store):
        app = _app(store)
        context = _context(**self.GRANT_A)
        run = _register(app, idempotency_key="grant-register-1", context=context)["run"]
        assert (run["client_id"], run["grant_id"]) == ("client-1", "grant-a")
        resolved = app.invoke("work.run.resolve-v1", {"run_id": run["run_id"]}, context)
        assert resolved == {
            "repo_id": store.repo_id, "run_id": run["run_id"],
            "principal_id": "github:1:0", "workspace_id": "ws-1",
            "client_id": "client-1", "grant_id": "grant-a",
        }

    def test_a_run_without_a_grant_echoes_null_client_and_grant(self, store):
        app = _app(store)
        run_id = _new_run(app, "grant-none-1")
        resolved = app.invoke("work.run.resolve-v1", {"run_id": run_id}, _context())
        assert (resolved["client_id"], resolved["grant_id"]) == (None, None)

    @pytest.mark.parametrize(
        "other",
        [
            {"client_id": "client-1", "grant_id": "grant-b"},
            {"client_id": "client-2", "grant_id": "grant-a"},
            {"client_id": None, "grant_id": None},
        ],
        ids=["other-grant", "other-client", "no-grant"],
    )
    def test_a_different_grant_of_the_same_principal_is_run_not_found(self, store, other):
        app = _app(store)
        run_id = _register(
            app, idempotency_key="grant-resolve-" + (other["grant_id"] or "none"),
            context=_context(**self.GRANT_A),
        )["run"]["run_id"]
        for operation, arguments in (
            ("work.run.resolve-v1", {"run_id": run_id}),
            ("work.evidence.tail-v1", {"run_id": run_id}),
            ("work.session-note.write-v1",
             {"run_id": run_id, "note": "not yours", "idempotency_key": "grant-note-key-1"}),
        ):
            with pytest.raises(ApplicationRejection) as excinfo:
                app.invoke(operation, arguments, _context(**other))
            assert excinfo.value.code == "run-not-found", operation
        assert _notes(store, run_id) == 0

    def test_a_run_minted_without_a_grant_is_not_an_oauth_caller_s(self, store):
        app = _app(store)
        run_id = _new_run(app, "grant-none-2")
        with pytest.raises(ApplicationRejection) as excinfo:
            app.invoke("work.run.resolve-v1", {"run_id": run_id}, _context(**self.GRANT_A))
        assert excinfo.value.code == "run-not-found"

    def test_the_same_key_under_a_different_grant_is_a_conflict_not_a_replay(self, store):
        """Replaying grant A's run to grant B would hand B a run it cannot
        resolve; the binding is part of the request, so it conflicts."""
        app = _app(store)
        key = "grant-register-shared-1"
        _register(app, idempotency_key=key, context=_context(**self.GRANT_A))
        with pytest.raises(ApplicationRejection) as excinfo:
            _register(app, idempotency_key=key, context=_context(**self.GRANT_B))
        assert excinfo.value.code == "idempotency-conflict"
        assert _register(app, idempotency_key=key, context=_context(**self.GRANT_A))["run"][
            "grant_id"
        ] == "grant-a"


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
                "chain_prev_digest": pg.evidence_entry_digest(first["item"]),
                "idempotency_key": "evidence-append-key-2",
            },
            _context(),
        )
        assert second["item"]["chain_seq"] == 1
        assert second["item"]["chain_prev_digest"] == pg.evidence_entry_digest(first["item"])

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
        first = app.invoke(
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
                "claims": [], "provenance": {}, "chain_seq": 1,
                "chain_prev_digest": pg.evidence_entry_digest(first["item"]),
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
        other = app.invoke(
            "work.evidence.append-v1",
            {
                "run_id": run_id, "item_id": "evidence_other", "kind": "test",
                "ref": "ref-other", "digest": "sha256:" + "7" * 64, "collector": "tester",
                "validity": _validity(), "claims": [], "provenance": {},
                "chain_seq": 1, "chain_prev_digest": pg.evidence_entry_digest(first["item"]),
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
                "chain_seq": 2, "chain_prev_digest": pg.evidence_entry_digest(other["item"]),
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


class TestStorageLevelIdempotency:
    """The backend writes converge on one row per idempotency key even when
    called *outside* ``work_application``'s ledger (a row left without its
    ledger entry by any path), and replay that row only for the same request
    digest: a same-key retry with different arguments is an
    ``IdempotencyConflict`` at storage, never the other request's row."""

    @staticmethod
    def _append(store, run_id, *, key, request_digest, item_id, digest, seq, prev):
        return pg.append_evidence(
            store, run_id, idempotency_key=key, request_digest=request_digest,
            item_id=item_id, kind="test", ref=f"ref-{item_id}", digest=digest,
            collector="tester", validity=_validity(), claims=[], provenance={},
            chain_seq=seq, chain_prev_digest=prev,
        )

    def test_append_evidence_replays_the_same_request_even_after_the_chain_moved(
        self, store
    ):
        run_id = _new_run(_app(store), "storage-idempotency-evidence-1")
        first = self._append(
            store, run_id, key="storage-evidence-key-1", request_digest="1" * 64,
            item_id="evi_storage_1", digest="sha256:" + "a" * 64, seq=0, prev=None,
        )
        # A second, unrelated item advances the tail in between, so the retry
        # recomputes a *different* chain_seq/chain_prev_digest.
        other = self._append(
            store, run_id, key="storage-evidence-key-2", request_digest="2" * 64,
            item_id="evi_storage_other", digest="sha256:" + "b" * 64, seq=1,
            prev=pg.evidence_entry_digest(first),
        )
        replay = self._append(
            store, run_id, key="storage-evidence-key-1", request_digest="1" * 64,
            item_id="evi_storage_1", digest="sha256:" + "a" * 64, seq=2,
            prev=pg.evidence_entry_digest(other),
        )
        assert replay == first
        assert replay["chain_seq"] == 0

    def test_append_evidence_same_key_different_request_is_a_conflict(self, store):
        """The reviewer's repro: an item with digest aaa is committed; a
        same-key retry carrying digest eee must not come back ok with aaa."""
        run_id = _new_run(_app(store), "storage-idempotency-evidence-2")
        first = self._append(
            store, run_id, key="storage-evidence-key-3", request_digest="3" * 64,
            item_id="evi_storage_3", digest="sha256:" + "a" * 64, seq=0, prev=None,
        )
        with pytest.raises(pg.IdempotencyConflict):
            self._append(
                store, run_id, key="storage-evidence-key-3", request_digest="4" * 64,
                item_id="evi_storage_3", digest="sha256:" + "e" * 64, seq=1,
                prev=pg.evidence_entry_digest(first),
            )
        assert _items(store, run_id) == 1

    def test_write_session_note_with_the_same_key_replays_without_a_second_row(
        self, store
    ):
        run_id = _new_run(_app(store), "storage-idempotency-note-1")
        first = pg.write_session_note(
            store, run_id, note="same note", idempotency_key="storage-note-key-1",
            request_digest="5" * 64,
        )
        second = pg.write_session_note(
            store, run_id, note="same note", idempotency_key="storage-note-key-1",
            request_digest="5" * 64,
        )
        assert second == first
        assert _notes(store, run_id) == 1

    def test_write_session_note_same_key_different_request_is_a_conflict(self, store):
        run_id = _new_run(_app(store), "storage-idempotency-note-2")
        pg.write_session_note(
            store, run_id, note="first note", idempotency_key="storage-note-key-2",
            request_digest="6" * 64,
        )
        with pytest.raises(pg.IdempotencyConflict):
            pg.write_session_note(
                store, run_id, note="other note", idempotency_key="storage-note-key-2",
                request_digest="7" * 64,
            )
        assert _notes(store, run_id) == 1

    def test_a_storage_row_without_its_ledger_entry_still_refuses_different_arguments(
        self, store
    ):
        """Crash-then-different-args: a note committed under a key with no
        ledger row (as a crash in any earlier design could leave) is not
        replayed for a different request through the served operation."""
        app = _app(store)
        run_id = _new_run(app, "storage-idempotency-note-3")
        key = "storage-note-key-3"
        residue_digest = _served_digest("write_session_note", {
            "run_id": run_id, "note": "crashed note", "idempotency_key": key,
        })
        pg.write_session_note(
            store, run_id, note="crashed note", idempotency_key=key,
            request_digest=residue_digest,
        )
        with pytest.raises(ApplicationRejection) as excinfo:
            _note_call(run_id, key, "a different note")(app)
        assert excinfo.value.code == "idempotency-conflict"
        assert _ledger_rows(store, "write_session_note", key) == 0
        # The same request as the residue replays it and records the ledger.
        replay = _note_call(run_id, key, "crashed note")(app)
        assert replay["note"] == "crashed note"
        assert _notes(store, run_id) == 1
        assert _ledger_rows(store, "write_session_note", key) == 1


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


# ---------------------------------------------------------------------------
# Races and crashes (E2 review blocker on PR #97): the ledger claim, the
# effect and the stored result commit in ONE transaction.  Each race pauses
# the first caller after its effect ran but before it committed, starts the
# second caller on its own connection, waits until PostgreSQL reports that
# second backend blocked on a lock, then lets the first commit.
# ---------------------------------------------------------------------------


def _sibling_store(store) -> "pg.PgStore":
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return pg.PgStore(
        conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid
    )


def _wait_until_lock_blocked(observer, pid: int, done: threading.Event) -> bool:
    """True once backend ``pid`` waits on a lock; False if it finished first."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not done.is_set():
        with observer.cursor() as cur:
            cur.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            )
            row = cur.fetchone()
        observer.rollback()
        if row is not None and row["wait_event_type"] == "Lock":
            return True
        time.sleep(0.02)
    return False


def _race(monkeypatch, store, backend_function: str, first, second) -> dict:
    """Run ``first`` and ``second`` (each ``app -> result``) on two connections.

    ``first`` is held inside ``pg.<backend_function>`` -- after its write,
    before its transaction commits -- until ``second`` is blocked on a lock
    (or has already finished, which is what a broken guard looks like).
    Returns each caller's result or raised exception.
    """
    real = getattr(pg, backend_function)
    first_wrote = threading.Event()
    release_first = threading.Event()

    def paused(*args, **kwargs):
        result = real(*args, **kwargs)
        if threading.current_thread().name == "race-first":
            first_wrote.set()
            assert release_first.wait(timeout=30)
        return result

    monkeypatch.setattr(pg, backend_function, paused)
    stores = {"first": _sibling_store(store), "second": _sibling_store(store)}
    outcomes: dict = {}
    done = {name: threading.Event() for name in stores}

    def run(name, call):
        try:
            outcomes[name] = call(_app(stores[name]))
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcomes[name] = exc
        finally:
            done[name].set()

    threads = {
        name: threading.Thread(target=run, args=(name, call), name=f"race-{name}")
        for name, call in (("first", first), ("second", second))
    }
    observer = psycopg.connect(_PG_URL, row_factory=dict_row)
    try:
        threads["first"].start()
        assert first_wrote.wait(timeout=10)
        threads["second"].start()
        outcomes["second_blocked"] = _wait_until_lock_blocked(
            observer, stores["second"].conn.info.backend_pid, done["second"]
        )
    finally:
        release_first.set()
        for thread in threads.values():
            thread.join(timeout=30)
        observer.close()
        for s in stores.values():
            s.conn.close()
    assert not any(thread.is_alive() for thread in threads.values())
    return outcomes


def _served_digest(tool: str, arguments: dict) -> str:
    from sprintctl.work_application import _request_digest

    return _request_digest(tool, arguments)


def _count(store, sql: str, params: tuple) -> int:
    with store.conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()["n"]
    store.conn.rollback()
    return int(value)


def _ledger_rows(store, tool: str, key: str) -> int:
    return _count(
        store,
        "SELECT count(*) AS n FROM work_idempotency_ledger WHERE repo_id = %s "
        "AND tool = %s AND idempotency_key = %s",
        (store.repo_id, tool, key),
    )


def _notes(store, run_id: str) -> int:
    return _count(
        store,
        "SELECT count(*) AS n FROM session_note WHERE repo_id = %s AND run_id = %s",
        (store.repo_id, run_id),
    )


def _items(store, run_id: str) -> int:
    return _count(
        store,
        "SELECT count(*) AS n FROM evidence_item WHERE repo_id = %s AND run_id = %s",
        (store.repo_id, run_id),
    )


def _orphan_items(store, run_id: str) -> int:
    """Evidence items no committed ledger result accounts for."""
    return _count(
        store,
        "SELECT count(*) AS n FROM evidence_item e WHERE e.repo_id = %s AND e.run_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM work_idempotency_ledger l WHERE l.repo_id = e.repo_id "
        "AND l.tool = 'append_evidence' AND l.result->'item'->>'item_id' = e.item_id)",
        (store.repo_id, run_id),
    )


def _note_call(run_id: str, key: str, note: str = "raced note"):
    return lambda app: app.invoke(
        "work.session-note.write-v1",
        {"run_id": run_id, "note": note, "idempotency_key": key},
        _context(),
    )


def _append_args(run_id: str, key: str, *, item_id: str = "evidence_raced", seq: int = 0,
                 prev: str | None = None, digest: str = "sha256:" + "e" * 64) -> dict:
    return {
        "run_id": run_id, "item_id": item_id, "kind": "test", "ref": f"ref-{item_id}",
        "digest": digest, "collector": "tester", "validity": _validity(),
        "claims": [], "provenance": {}, "chain_seq": seq, "chain_prev_digest": prev,
        "idempotency_key": key,
    }


def _append_call(run_id: str, key: str, **kwargs):
    return lambda app: app.invoke(
        "work.evidence.append-v1", _append_args(run_id, key, **kwargs), _context()
    )


class TestIdempotencyRaces:
    def test_concurrent_same_key_session_notes_write_exactly_one(self, store, monkeypatch):
        app = _app(store)
        run_id = _new_run(app, "race-note-1")
        key = "race-note-key-1"
        out = _race(
            monkeypatch, store, "write_session_note",
            _note_call(run_id, key), _note_call(run_id, key),
        )
        assert out["second_blocked"], "the second caller never waited on the first's claim"
        assert isinstance(out["first"], dict), out["first"]
        assert out["second"] == out["first"]
        assert _notes(store, run_id) == 1
        assert _ledger_rows(store, "write_session_note", key) == 1

    def test_concurrent_same_key_evidence_appends_write_one_item_and_no_orphan(
        self, store, monkeypatch
    ):
        app = _app(store)
        run_id = _new_run(app, "race-append-1")
        key = "race-append-key-1"
        out = _race(
            monkeypatch, store, "append_evidence",
            _append_call(run_id, key), _append_call(run_id, key),
        )
        assert out["second_blocked"], "the second caller never waited on the first's claim"
        assert isinstance(out["first"], dict), out["first"]
        assert out["second"] == out["first"]
        assert _items(store, run_id) == 1
        assert _orphan_items(store, run_id) == 0
        assert _ledger_rows(store, "append_evidence", key) == 1

    def test_concurrent_same_key_different_arguments_is_an_idempotency_conflict(
        self, store, monkeypatch
    ):
        app = _app(store)
        run_id = _new_run(app, "race-conflict-1")
        key = "race-conflict-key-1"
        out = _race(
            monkeypatch, store, "write_session_note",
            _note_call(run_id, key, "first note"), _note_call(run_id, key, "second note"),
        )
        assert out["second_blocked"], "the second caller never waited on the first's claim"
        assert out["first"]["note"] == "first note"
        assert isinstance(out["second"], ApplicationRejection), out["second"]
        assert out["second"].code == "idempotency-conflict"
        assert _notes(store, run_id) == 1

    def test_concurrent_same_key_different_evidence_never_reaches_the_chain(
        self, store, monkeypatch
    ):
        """The loser is refused as idempotency-conflict -- by the ledger claim,
        or by the request digest stored on the item beneath it -- never by
        running its own append and losing the chain."""
        app = _app(store)
        run_id = _new_run(app, "race-conflict-2")
        key = "race-conflict-key-2"
        out = _race(
            monkeypatch, store, "append_evidence",
            _append_call(run_id, key, item_id="evidence_x"),
            _append_call(run_id, key, item_id="evidence_y"),
        )
        assert out["second_blocked"], "the second caller never waited on the first's claim"
        assert out["first"]["item"]["item_id"] == "evidence_x"
        assert isinstance(out["second"], ApplicationRejection), repr(out["second"])
        assert out["second"].code == "idempotency-conflict"
        assert _items(store, run_id) == 1

    def test_concurrent_chain_tail_race_is_a_clean_chain_conflict(self, store, monkeypatch):
        """Two different keys both extend seq 0: the loser is refused with
        evidence-chain-conflict, never a raw IntegrityError."""
        app = _app(store)
        run_id = _new_run(app, "race-tail-1")
        out = _race(
            monkeypatch, store, "append_evidence",
            _append_call(run_id, "race-tail-key-a", item_id="evidence_a"),
            _append_call(run_id, "race-tail-key-b", item_id="evidence_b"),
        )
        assert out["second_blocked"], "the second append never waited on the first"
        assert out["first"]["item"]["item_id"] == "evidence_a"
        assert isinstance(out["second"], ApplicationRejection), repr(out["second"])
        assert out["second"].code == "evidence-chain-conflict"
        # Serialized by the per-run lock: the loser judged the winner's
        # committed tail, not a unique-index collision after the fact.
        assert "(expected 1)" in out["second"].message
        assert _items(store, run_id) == 1
        assert _ledger_rows(store, "append_evidence", "race-tail-key-b") == 0


N_RACERS = 8


def _stampede(store, calls) -> list:
    """Run each ``app -> result`` call on its own thread and connection,
    released together by a barrier; returns each result or exception."""
    stores = [_sibling_store(store) for _ in calls]
    barrier = threading.Barrier(len(calls))
    outcomes: list = [None] * len(calls)

    def run(index, call):
        app = _app(stores[index])
        try:
            barrier.wait(timeout=10)
            outcomes[index] = call(app)
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcomes[index] = exc

    threads = [
        threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        for s in stores:
            s.conn.close()
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


def _split(outcomes):
    ok = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, ApplicationRejection)]
    other = [o for o in outcomes if not isinstance(o, (dict, ApplicationRejection))]
    assert not other, [repr(o) for o in other]
    return ok, sorted(r.code for r in refused)


class TestStampedes:
    """N threads, N connections, one barrier."""

    def test_same_args_note_gives_one_row_and_every_caller_the_same_ok(self, store):
        app = _app(store)
        run_id = _new_run(app, "stampede-note-1")
        key = "stampede-note-key-1"
        ok, refused = _split(_stampede(store, [_note_call(run_id, key)] * N_RACERS))
        assert refused == []
        assert len(ok) == N_RACERS and all(o == ok[0] for o in ok)
        assert _notes(store, run_id) == 1
        assert _ledger_rows(store, "write_session_note", key) == 1

    def test_same_args_evidence_gives_one_item_and_every_caller_the_same_ok(self, store):
        app = _app(store)
        run_id = _new_run(app, "stampede-append-1")
        key = "stampede-append-key-1"
        ok, refused = _split(_stampede(store, [_append_call(run_id, key)] * N_RACERS))
        assert refused == []
        assert len(ok) == N_RACERS and all(o == ok[0] for o in ok)
        assert _items(store, run_id) == 1
        assert _orphan_items(store, run_id) == 0

    def test_same_args_register_gives_one_run(self, store):
        app = _app(store)
        key = "stampede-register-1"
        ok, refused = _split(_stampede(
            store, [lambda a: _register(a, idempotency_key=key)] * N_RACERS
        ))
        assert refused == []
        assert len({o["run"]["run_id"] for o in ok}) == 1
        assert _count(
            store, "SELECT count(*) AS n FROM run WHERE repo_id = %s AND idempotency_key = %s",
            (store.repo_id, key),
        ) == 1

    def test_different_args_note_gives_one_ok_and_the_rest_conflict(self, store):
        app = _app(store)
        run_id = _new_run(app, "stampede-note-2")
        key = "stampede-note-key-2"
        calls = [_note_call(run_id, key, f"note {i}") for i in range(N_RACERS)]
        ok, refused = _split(_stampede(store, calls))
        assert len(ok) == 1
        assert refused == ["idempotency-conflict"] * (N_RACERS - 1)
        assert _notes(store, run_id) == 1
        assert _ledger_rows(store, "write_session_note", key) == 1

    def test_different_args_evidence_gives_one_ok_and_the_rest_conflict(self, store):
        app = _app(store)
        run_id = _new_run(app, "stampede-append-2")
        key = "stampede-append-key-2"
        calls = [
            _append_call(run_id, key, digest="sha256:" + f"{i:x}" * 64) for i in range(N_RACERS)
        ]
        ok, refused = _split(_stampede(store, calls))
        assert len(ok) == 1
        assert refused == ["idempotency-conflict"] * (N_RACERS - 1)
        assert _items(store, run_id) == 1
        assert _orphan_items(store, run_id) == 0

    def test_crash_then_different_args_gives_conflicts_and_no_second_row(self, store):
        """A note row committed without its ledger entry (a crash residue):
        N racers each sending a different note under its key all conflict."""
        app = _app(store)
        run_id = _new_run(app, "stampede-crash-1")
        key = "stampede-crash-key-1"
        pg.write_session_note(
            store, run_id, note="residue", idempotency_key=key,
            request_digest=_served_digest("write_session_note", {
                "run_id": run_id, "note": "residue", "idempotency_key": key,
            }),
        )
        calls = [_note_call(run_id, key, f"retry {i}") for i in range(N_RACERS)]
        ok, refused = _split(_stampede(store, calls))
        assert ok == []
        assert refused == ["idempotency-conflict"] * N_RACERS
        assert _notes(store, run_id) == 1
        assert _ledger_rows(store, "write_session_note", key) == 0

    def test_chain_tail_race_gives_one_ok_and_the_rest_chain_conflict(self, store):
        app = _app(store)
        run_id = _new_run(app, "stampede-tail-1")
        calls = [
            _append_call(run_id, f"stampede-tail-key-{i}", item_id=f"evidence_{i}")
            for i in range(N_RACERS)
        ]
        ok, refused = _split(_stampede(store, calls))
        assert len(ok) == 1
        assert refused == ["evidence-chain-conflict"] * (N_RACERS - 1)
        assert _items(store, run_id) == 1


class TestCrashBetweenEffectAndLedger:
    @staticmethod
    def _crash(monkeypatch):
        def crash(*_args, **_kwargs):
            raise RuntimeError("simulated crash before the ledger result was recorded")

        monkeypatch.setattr(pg, "_record_idempotent_result", crash)

    def test_session_note_crash_commits_nothing_and_the_retry_succeeds(
        self, store, monkeypatch
    ):
        app = _app(store)
        run_id = _new_run(app, "crash-note-1")
        key = "crash-note-key-1"
        with monkeypatch.context() as patch:
            self._crash(patch)
            with pytest.raises(RuntimeError, match="simulated crash"):
                _note_call(run_id, key)(app)
        assert _notes(store, run_id) == 0
        assert _ledger_rows(store, "write_session_note", key) == 0
        retried = _note_call(run_id, key)(app)
        assert retried["note"] == "raced note"
        assert _notes(store, run_id) == 1
        assert _note_call(run_id, key)(app) == retried

    def test_evidence_crash_commits_nothing_and_the_retry_succeeds(self, store, monkeypatch):
        app = _app(store)
        run_id = _new_run(app, "crash-append-1")
        key = "crash-append-key-1"
        with monkeypatch.context() as patch:
            self._crash(patch)
            with pytest.raises(RuntimeError, match="simulated crash"):
                _append_call(run_id, key)(app)
        assert _items(store, run_id) == 0
        assert _ledger_rows(store, "append_evidence", key) == 0
        retried = _append_call(run_id, key)(app)
        assert retried["item"]["chain_seq"] == 0
        assert _items(store, run_id) == 1


class TestChainOwnerChecks:
    def test_entry_digest_matches_vuoro_evidence_chain(self):
        """Vectors computed by vuoro_evidence.core.chain.entry_digest/link
        (vuoro packages/vuoro-evidence); if this drifts, the owner check
        refuses every correctly linked append."""
        root = {"item_id": "evidence_1", "digest": "sha256:" + "b" * 64,
                "chain_seq": 0, "chain_prev_digest": None}
        root_digest = "sha256:b84068eb36518ca9aad5e1c208c5f45789e130f7449dc8e84c478b8e4b07d4bd"
        assert pg.evidence_entry_digest(root) == root_digest
        second = {"item_id": "evidence_2", "digest": "sha256:" + "c" * 64,
                  "chain_seq": 1, "chain_prev_digest": root_digest}
        assert pg.evidence_entry_digest(second) == (
            "sha256:9782d4233009a4e0faa730a82e712be1ffa4c6c1a79d9b45dc1a43f5a3feaf17"
        )

    def test_a_prev_digest_that_is_not_the_tail_s_entry_digest_is_refused(self, store):
        app = _app(store)
        run_id = _new_run(app, "prev-digest-1")
        _append_call(run_id, "prev-digest-key-1", item_id="evidence_p0")(app)
        with pytest.raises(ApplicationRejection) as excinfo:
            _append_call(
                run_id, "prev-digest-key-2", item_id="evidence_p1", seq=1,
                prev="sha256:" + "0" * 64,
            )(app)
        assert excinfo.value.code == "evidence-chain-conflict"
        assert "chain_prev_digest" in excinfo.value.message
        assert _items(store, run_id) == 1

    def test_a_chain_root_carrying_a_prev_digest_is_refused(self, store):
        app = _app(store)
        run_id = _new_run(app, "prev-digest-2")
        with pytest.raises(ApplicationRejection) as excinfo:
            _append_call(run_id, "prev-digest-key-3", prev="sha256:" + "0" * 64)(app)
        assert excinfo.value.code == "evidence-chain-conflict"
        assert _items(store, run_id) == 0

    def test_reusing_an_item_id_under_a_different_key_is_a_chain_conflict(self, store):
        """item_id is tied to the idempotency key that stored it: the same id
        under another key is refused as a domain conflict (whatever its
        content), never leaked as a raw UniqueViolation on the primary key."""
        app = _app(store)
        run_id = _new_run(app, "dup-item-1")
        first = _append_call(run_id, "dup-item-key-1", item_id="evidence_dup")(app)
        for digest in ("sha256:" + "e" * 64, "sha256:" + "f" * 64):
            with pytest.raises(ApplicationRejection) as excinfo:
                _append_call(
                    run_id, "dup-item-key-2", item_id="evidence_dup", seq=1,
                    prev=pg.evidence_entry_digest(first["item"]), digest=digest,
                )(app)
            assert excinfo.value.code == "evidence-chain-conflict"
            assert "evidence_dup" in excinfo.value.message
        assert _items(store, run_id) == 1


class TestSchema17Migration:
    @staticmethod
    def _migrate_with(store, label: str, foreign_ddl: str):
        """Build a schema at version 2 holding ``foreign_ddl``, migrate it,
        and return the raised error (the schema is dropped afterwards)."""
        schema = f"migration_{label}_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
            cur.execute(foreign_ddl)
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            with pytest.raises(pg_migrations.RemoteSchemaMigrationError) as excinfo:
                pg_migrations.migrate_schema(pg.PgStore(conn, f"migration-{label}"))
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_version")
                assert cur.fetchone()["version"] == 2
            conn.rollback()
            return excinfo.value
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    @pytest.mark.parametrize("table", sorted(pg._SCHEMA_17_TABLES))
    def test_a_foreign_table_with_matching_columns_is_refused(self, store, table):
        """Same column names, types and nullability as schema 17's table, but
        none of its keys or checks: IF NOT EXISTS would have kept it."""
        columns, _constraints = pg._SCHEMA_17_TABLES[table]
        ddl = ", ".join(
            f"{name} {type_} {'NOT NULL' if not_null else ''}"
            for name, type_, not_null in columns
        )
        error = self._migrate_with(store, "matching_" + table, f"CREATE TABLE {table} ({ddl})")
        assert table in str(error)

    def test_a_foreign_pre_existing_run_table_is_refused(self, store):
        schema = "migration_foreign_run_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
            cur.execute("CREATE TABLE run (id integer PRIMARY KEY, label text)")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match=r"\brun\b"):
                pg_migrations.migrate_schema(pg.PgStore(conn, "migration-foreign-run"))
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_version")
                assert cur.fetchone()["version"] == 2
                cur.execute(
                    "SELECT count(*) AS n FROM pg_attribute WHERE attrelid = 'run'::regclass "
                    "AND attnum > 0 AND NOT attisdropped"
                )
                assert cur.fetchone()["n"] == 2
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()
