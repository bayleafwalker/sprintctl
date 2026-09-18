"""Served Decision and Release operations (S3 PR3) on the SQLite backend.

``work.decision.record``, ``work.read.item-decisions`` and
``work.read.release`` are exercised through ``WorkApplication.invoke`` here;
``tests/pg/test_served_decisions.py`` runs the same contract against a
disposable PostgreSQL authority.  The CLI half (``item decide`` and the
decision lines of ``item show``) is covered at the end of this module.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from sprintctl import db
from sprintctl.application import ApplicationRejection
from sprintctl.cli import cli
import sprintctl.cli as cli_module
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.test_decisions import _legacy_item as _sqlite_legacy_item
from tests.test_served_result_schema_conformance import _validate
from tests.test_served_routes import _configure_served_repo
from tests.test_work_application import _application

EVIDENCE = "ab" * 32


def _context(actor: str = "alice", idempotency_key: str | None = None):
    identity = SimpleNamespace(
        actor=actor,
        environment="vuoro-dev",
        authorities=frozenset(),
        authorizes_repo=lambda repo_id: True,
    )
    return SimpleNamespace(
        identity=identity,
        request_id="request-1",
        basis_revision=None,
        catalog_revision="catalog-1",
        idempotency_requirement="required" if idempotency_key else "not-allowed",
        idempotency_key=idempotency_key,
    )


def _key() -> str:
    return uuid.uuid4().hex


def _assert_conforms(operation: str, result: dict) -> None:
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == operation)
    failures: list[str] = []
    _validate(result, contract.result_schema, operation, failures)
    assert not failures, "\n".join(failures)


@dataclass
class Env:
    app: Any
    backend: Any
    store: Any
    new_item: Callable[..., int]
    legacy_item: Callable[[str], int]

    def invoke(self, operation, arguments, *, actor="alice", key=None):
        result = self.app.invoke(operation, arguments, _context(actor, key))
        _assert_conforms(operation, result)
        return result

    def decide(self, item_id, kind="accept", *, key=None, actor="alice", **extra):
        arguments = {
            "item_id": item_id,
            "kind": kind,
            "rationale": extra.pop("rationale", f"{kind} it"),
            "evidence_digests": extra.pop("evidence_digests", [EVIDENCE]),
            **extra,
        }
        return self.invoke(
            "work.decision.record", arguments, actor=actor, key=key or _key()
        )

    def reserve(self, item_id) -> str:
        row = self.backend.reserve(
            self.store, item_id, actor="agent", session_id=f"s-{_key()[:6]}", role="execution"
        )
        return row["release_digest"]


class DecisionOperationContract:
    """The served decision/release contract, run once per backend."""

    def test_accept_closes_the_item_against_its_current_release(self, env):
        item_id = env.new_item()
        digest = env.reserve(item_id)
        result = env.decide(item_id, "accept")
        assert result["status"] == "done"
        assert result["resolution"] == "accepted"
        assert result["replayed"] is False
        decision = result["decision"]
        assert result["terminal_decision_id"] == decision["id"]
        assert decision["kind"] == "accept"
        assert decision["release_digest"] == digest
        assert decision["evidence_digests"] == [EVIDENCE]

    def test_replay_under_the_same_key_returns_the_first_decision(self, env):
        item_id = env.new_item()
        key = _key()
        first = env.decide(item_id, "reject", key=key)
        again = env.decide(item_id, "reject", key=key)
        assert again["replayed"] is True
        assert again["decision"]["id"] == first["decision"]["id"]
        assert (again["status"], again["resolution"]) == ("done", "rejected")
        assert len(env.backend.list_decisions(env.store, item_id)) == 1
        events = [
            event
            for event in env.backend.list_events(env.store, env.backend.get_work_item(env.store, item_id)["sprint_id"])
            if event.get("work_item_id") == item_id and event["event_type"] == "item-decided"
        ]
        assert len(events) == 1

    def test_revise_replay_records_one_row(self, env):
        item_id = env.new_item()
        key = _key()
        env.decide(item_id, "revise", key=key)
        env.decide(item_id, "revise", key=key)
        assert [d["kind"] for d in env.backend.list_decisions(env.store, item_id)] == ["revise"]

    def test_same_key_with_a_different_request_is_a_conflict(self, env):
        item_id = env.new_item()
        key = _key()
        env.decide(item_id, "revise", key=key)
        with pytest.raises(ApplicationRejection) as rejected:
            env.decide(item_id, "revise", key=key, rationale="something else")
        assert (rejected.value.code, rejected.value.http_status) == ("idempotency-conflict", 409)
        with pytest.raises(ApplicationRejection) as other_actor:
            env.decide(item_id, "revise", key=key, actor="mallory")
        assert other_actor.value.code == "idempotency-conflict"

    def test_the_actor_is_the_authenticated_identity(self, env):
        item_id = env.new_item()
        result = env.app.invoke(
            "work.decision.record",
            {
                "item_id": item_id,
                "kind": "withdraw",
                "rationale": "not needed",
                "evidence_digests": [],
                # Not in the contract; a handler must never read it.
                "actor": "mallory",
            },
            _context("alice", _key()),
        )
        assert result["decision"]["actor"] == "alice"
        [decision] = env.backend.list_decisions(env.store, item_id)
        assert decision["actor"] == "alice"

    def test_a_foreign_release_digest_is_refused(self, env):
        item_id = env.new_item()
        env.reserve(item_id)
        other = env.new_item()
        foreign = env.reserve(other)
        with pytest.raises(ApplicationRejection) as rejected:
            env.decide(item_id, "accept", release_digest=foreign)
        assert (rejected.value.code, rejected.value.http_status) == ("release-mismatch", 409)
        item = env.backend.get_work_item(env.store, item_id)
        assert item["status"] == "active"
        assert env.backend.list_decisions(env.store, item_id) == []

    def test_revise_keeps_the_item_open_and_retires_the_current_release(self, env):
        item_id = env.new_item()
        digest = env.reserve(item_id)
        result = env.decide(item_id, "revise")
        assert (result["status"], result["resolution"]) == ("active", None)
        assert result["terminal_decision_id"] is None
        assert result["decision"]["release_digest"] == digest
        with pytest.raises(ApplicationRejection) as missing:
            env.invoke("work.read.release", {"item_id": item_id})
        assert (missing.value.code, missing.value.http_status) == ("release-not-found", 404)
        # The retired release is still readable by digest.
        assert env.invoke("work.read.release", {"release_digest": digest})["release"][
            "release_digest"
        ] == digest

    def test_supersede_names_the_superseding_item(self, env):
        item_id = env.new_item()
        replacement = env.new_item(status="pending")
        result = env.decide(item_id, "supersede", superseded_by_item_id=replacement)
        assert result["resolution"] == "superseded"
        assert result["decision"]["superseded_by_item_id"] == replacement

    def test_invalid_transitions_are_conflicts(self, env):
        pending = env.new_item(status="pending")
        with pytest.raises(ApplicationRejection) as rejected:
            env.decide(pending, "accept")
        assert (rejected.value.code, rejected.value.http_status) == ("invalid-transition", 409)
        closed = env.new_item()
        env.decide(closed, "accept")
        with pytest.raises(ApplicationRejection) as terminal:
            env.decide(closed, "reject")
        assert (terminal.value.code, terminal.value.http_status) == ("item-terminal", 409)

    def test_a_legacy_done_item_takes_no_decision(self, env):
        item_id = env.legacy_item("done")
        with pytest.raises(ApplicationRejection) as rejected:
            env.decide(item_id, "accept")
        assert (rejected.value.code, rejected.value.http_status) == ("legacy-done-item", 409)

    def test_arguments_are_validated(self, env):
        item_id = env.new_item()
        for arguments, code, status in (
            ({"kind": "approve"}, "decision-rejected", 422),
            ({"evidence_digests": ["not-a-digest"]}, "decision-rejected", 422),
            ({"kind": "supersede"}, "decision-rejected", 422),
            ({"item_id": 99_999_999}, "item-not-found", 404),
        ):
            payload = {
                "item_id": item_id,
                "kind": "accept",
                "rationale": "x",
                "evidence_digests": [],
                **arguments,
            }
            with pytest.raises(ApplicationRejection) as rejected:
                env.app.invoke("work.decision.record", payload, _context(idempotency_key=_key()))
            assert (rejected.value.code, rejected.value.http_status) == (code, status), arguments

    def test_a_decision_requires_an_idempotency_key(self, env):
        item_id = env.new_item()
        with pytest.raises(ApplicationRejection) as rejected:
            env.app.invoke(
                "work.decision.record",
                {"item_id": item_id, "kind": "accept", "rationale": "", "evidence_digests": []},
                _context(),
            )
        assert rejected.value.code == "idempotency-key-required"

    def test_item_decisions_are_listed_oldest_first(self, env):
        item_id = env.new_item()
        env.decide(item_id, "revise")
        env.decide(item_id, "accept")
        result = env.invoke("work.read.item-decisions", {"item_id": item_id})
        assert [d["kind"] for d in result["decisions"]] == ["revise", "accept"]
        assert (result["status"], result["resolution"]) == ("done", "accepted")
        assert result["terminal_decision_id"] == result["decisions"][-1]["id"]
        with pytest.raises(ApplicationRejection) as missing:
            env.invoke("work.read.item-decisions", {"item_id": 99_999_999})
        assert missing.value.http_status == 404

    def test_the_item_done_alias_still_records_an_accept(self, env):
        item_id = env.new_item()
        env.backend.set_work_item_status(env.store, item_id, "done", actor="old-client")
        result = env.invoke("work.read.item-decisions", {"item_id": item_id})
        [decision] = result["decisions"]
        assert (decision["kind"], decision["actor"]) == ("accept", "old-client")
        assert (result["status"], result["resolution"]) == ("done", "accepted")

    def test_release_reads_by_digest_or_current_item(self, env):
        item_id = env.new_item()
        digest = env.reserve(item_id)
        by_item = env.invoke("work.read.release", {"item_id": item_id})
        by_digest = env.invoke("work.read.release", {"release_digest": digest})
        assert by_item["release"]["release_digest"] == digest
        assert by_digest["release"] == by_item["release"]
        assert by_item["release"]["work_item_id"] == item_id
        assert by_item["commits"] == []
        for arguments, status in (
            ({}, 422),
            ({"item_id": item_id, "release_digest": digest}, 422),
            ({"release_digest": "f" * 64}, 404),
            ({"release_digest": "nope"}, 422),
        ):
            with pytest.raises(ApplicationRejection) as rejected:
                env.invoke("work.read.release", arguments)
            assert rejected.value.http_status == status, arguments

    def test_item_decided_events_cannot_be_forged(self, env):
        item_id = env.new_item()
        sprint_id = env.backend.get_work_item(env.store, item_id)["sprint_id"]
        with pytest.raises(ApplicationRejection):
            env.app.invoke(
                "work.event.add",
                {
                    "sprint_id": sprint_id,
                    "work_item_id": item_id,
                    "event_type": "item-decided",
                    "payload": {"decision_id": 1, "idempotency_key": "forged"},
                },
                _context(),
            )


def _sqlite_item(conn, status="active"):
    sprint_id = db.create_sprint(conn, f"Decide {_key()[:6]}", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "decisions")
    item_id = db.create_work_item(conn, sprint_id, track_id, "decide me")
    if status != "pending":
        db.set_work_item_status(conn, item_id, status)
    return item_id


@pytest.fixture
def env(conn):
    return Env(
        app=_application(store=conn, backend=db),
        backend=db,
        store=conn,
        new_item=lambda status="active": _sqlite_item(conn, status),
        legacy_item=lambda status: _sqlite_legacy_item(conn, status)[2],
    )


class TestSqliteDecisionOperations(DecisionOperationContract):
    pass


# --- CLI: item decide / item show -------------------------------------------


def test_item_decide_accept_closes_the_item_locally(runner, conn):
    item_id = _sqlite_item(conn)
    result = runner.invoke(
        cli,
        ["item", "decide", "--id", str(item_id), "--kind", "accept",
         "--rationale", "shipped", "--evidence", EVIDENCE, "--actor", "reviewer"],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded accept decision" in result.output
    assert "status done (accepted)" in result.output
    [decision] = db.list_decisions(conn, item_id)
    assert (decision["actor"], decision["rationale"], decision["evidence_digests"]) == (
        "reviewer", "shipped", [EVIDENCE]
    )

    shown = runner.invoke(cli, ["item", "show", "--id", str(item_id)])
    assert shown.exit_code == 0, shown.output
    assert "Resolution: accepted" in shown.output
    assert f"Decision: #{decision['id']} accept by reviewer" in shown.output
    assert "Rationale: shipped" in shown.output

    as_json = runner.invoke(cli, ["item", "show", "--id", str(item_id), "--json"])
    payload = json.loads(as_json.output)
    assert payload["terminal_decision"]["id"] == decision["id"]
    assert payload["item"]["resolution"] == "accepted"


def test_item_decide_revise_keeps_the_item_open_locally(runner, conn):
    item_id = _sqlite_item(conn)
    result = runner.invoke(
        cli,
        ["item", "decide", "--id", str(item_id), "--kind", "revise",
         "--rationale", "needs another pass", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["status"], payload["resolution"]) == ("active", None)
    shown = runner.invoke(cli, ["item", "show", "--id", str(item_id)])
    assert "Resolution:" not in shown.output
    assert "Decision:" not in shown.output


def test_item_decide_refusals_exit_nonzero_locally(runner, conn):
    item_id = _sqlite_item(conn, "pending")
    result = runner.invoke(
        cli, ["item", "decide", "--id", str(item_id), "--kind", "accept", "--rationale", "x"]
    )
    assert result.exit_code == 1
    assert "requires an active item" in result.output
    other = _sqlite_item(conn)
    foreign = db.reserve(conn, other, actor="a", session_id="s", role="execution")
    active = _sqlite_item(conn)
    result = runner.invoke(
        cli,
        ["item", "decide", "--id", str(active), "--kind", "accept", "--rationale", "x",
         "--release", foreign["release_digest"]],
    )
    assert result.exit_code == 1
    assert "is not a release of item" in result.output


def test_item_status_done_alias_still_works_locally(runner, conn):
    item_id = _sqlite_item(conn)
    revision = db.item_status_revision(db.get_work_item(conn, item_id))
    result = runner.invoke(
        cli,
        ["item", "status", "--id", str(item_id), "--status", "done",
         "--expected-revision", revision],
    )
    assert result.exit_code == 0, result.output
    [decision] = db.list_decisions(conn, item_id)
    assert decision["kind"] == "accept"
    shown = runner.invoke(cli, ["item", "show", "--id", str(item_id)])
    assert "Resolution: accepted" in shown.output


def _served_config(tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened a store")
    )


def test_served_item_decide_sends_no_actor(runner, tmp_path, monkeypatch):
    _served_config(tmp_path, monkeypatch)
    calls = []

    def decision_record(profile, **kwargs):
        calls.append((profile, kwargs))
        return {
            "repo_id": "served-repo",
            "item_id": 7,
            "decision": {"id": 3, "kind": "supersede", "release_digest": None},
            "status": "done",
            "resolution": "superseded",
            "terminal_decision_id": 3,
            "replayed": False,
        }

    monkeypatch.setattr(cli_module._served, "decision_record", decision_record)
    result = runner.invoke(
        cli,
        ["item", "decide", "--id", "7", "--kind", "supersede", "--rationale", "dup",
         "--superseded-by", "9", "--evidence", EVIDENCE, "--actor", "mallory"],
    )
    assert result.exit_code == 0, result.output
    assert "status done (superseded)" in result.output
    assert "--actor 'mallory' was not sent" in result.output
    [(_profile, kwargs)] = calls
    assert kwargs["repo_id"] == "served-guard-test"
    assert "actor" not in kwargs
    assert kwargs["item_id"] == 7 and kwargs["superseded_by_item_id"] == 9
    assert kwargs["evidence_digests"] == [EVIDENCE]
    assert kwargs["release_digest"] is None


def test_served_item_show_reads_the_terminal_decision(runner, tmp_path, monkeypatch):
    _served_config(tmp_path, monkeypatch)
    item = {
        "id": 7, "sprint_id": 1, "status": "done", "title": "t", "updated_at": "now",
        "resolution": "accepted", "terminal_decision_id": 3,
    }
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda profile, **kwargs: {
            "item": item, "events": [], "active_reservations": [], "refs": [],
            "deps": {"blocked_by": [], "blocks": []},
        },
    )
    monkeypatch.setattr(
        cli_module._served,
        "read_item_decisions",
        lambda profile, **kwargs: {
            "decisions": [
                {"id": 2, "kind": "revise", "actor": "bob", "created_at": "t1"},
                {"id": 3, "kind": "accept", "actor": "alice", "created_at": "t2",
                 "rationale": "good"},
            ]
        },
    )
    result = runner.invoke(cli, ["item", "show", "--id", "7"])
    assert result.exit_code == 0, result.output
    assert "Resolution: accepted" in result.output
    assert "Decision: #3 accept by alice at t2" in result.output


def test_served_decision_facade_is_keyed(monkeypatch):
    from sprintctl import served

    seen = {}

    async def fake_invoke(profile, operation, arguments, **kwargs):
        seen.update(operation=operation, arguments=arguments, kwargs=kwargs)
        return {}

    monkeypatch.setattr(served, "_invoke_operation", fake_invoke)
    served.decision_record(
        object(), repo_id="r", item_id=1, kind="accept", rationale="", evidence_digests=[]
    )
    assert seen["operation"] == "work.decision.record"
    assert seen["kwargs"]["idempotency_key"]
    assert "actor" not in seen["arguments"]
    served.decision_record(
        object(), repo_id="r", item_id=1, kind="accept", rationale="",
        evidence_digests=[], idempotency_key="fixed",
    )
    assert seen["kwargs"]["idempotency_key"] == "fixed"
    served.read_release(object(), repo_id="r", item_id=4)
    assert (seen["operation"], seen["arguments"]) == ("work.read.release", {"item_id": 4})
