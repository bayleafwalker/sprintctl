"""Served Decision and Release operations (S3 PR3) on the SQLite backend.

``work.decision.record``, ``work.read.item-decisions`` and
``work.read.release`` are exercised through ``WorkApplication.invoke`` here;
``tests/pg/test_served_decisions.py`` runs the same contract against a
disposable PostgreSQL authority.  The CLI half (``item decide`` and the
decision lines of ``item show``) is covered at the end of this module.
"""

from __future__ import annotations

import json
import sys
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

_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="served mode requires Python 3.12+"
)

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

    def test_a_legacy_done_item_takes_one_remark(self, env):
        item_id = env.legacy_item("done")
        before = env.backend.get_work_item(env.store, item_id)
        key = _key()
        result = env.decide(item_id, "reject", key=key, rationale="never built")
        assert (result["status"], result["resolution"]) == ("done", "rejected")
        decision = result["decision"]
        assert result["terminal_decision_id"] == decision["id"]
        assert (decision["rationale"], decision["evidence_digests"]) == (
            "never built", [EVIDENCE]
        )
        assert decision["release_digest"] is None
        after = env.backend.get_work_item(env.store, item_id)
        # The item stays done and legacy; its close time is not rewritten.
        assert (after["status"], after["legacy"]) == ("done", True)
        assert after["updated_at"] == before["updated_at"]
        [event] = [
            event
            for event in env.backend.list_events(env.store, after["sprint_id"])
            if event.get("work_item_id") == item_id and event["event_type"] == "item-decided"
        ]
        payload = event["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["legacy_remark"] is True
        # A retry replays; any further decision is refused: the re-mark is one-shot.
        again = env.decide(item_id, "reject", key=key, rationale="never built")
        assert again["replayed"] is True
        assert again["decision"]["id"] == decision["id"]
        with pytest.raises(ApplicationRejection) as second:
            env.decide(item_id, "accept")
        assert (second.value.code, second.value.http_status) == ("item-terminal", 409)
        assert len(env.backend.list_decisions(env.store, item_id)) == 1

    def test_a_legacy_remark_needs_a_terminal_kind_rationale_and_evidence(self, env):
        item_id = env.legacy_item("done")
        for kind, extra in (
            ("reject", {"evidence_digests": []}),
            ("reject", {"rationale": "   "}),
            ("revise", {}),
        ):
            with pytest.raises(ApplicationRejection) as rejected:
                env.decide(item_id, kind, **extra)
            assert (rejected.value.code, rejected.value.http_status) == (
                "legacy-done-item", 409
            ), (kind, extra)
        item = env.backend.get_work_item(env.store, item_id)
        assert (item["resolution"], item["terminal_decision_id"]) == (None, None)
        assert env.backend.list_decisions(env.store, item_id) == []

    def test_unbound_lists_three_categories_and_resolutions(self, env):
        legacy = env.legacy_item("done")
        sprint_id = env.backend.get_work_item(env.store, legacy)["sprint_id"]
        track_id = env.backend.get_work_item(env.store, legacy)["track_id"]

        def item_in_sprint(title):
            item_id = env.backend.create_work_item(env.store, sprint_id, track_id, title)
            env.backend.set_work_item_status(env.store, item_id, "active")
            return item_id

        unreleased = item_in_sprint("closed without a release")
        env.decide(unreleased, "accept")
        released = item_in_sprint("picked up")
        digest = env.reserve(released)
        bound = item_in_sprint("closed against its release")
        env.reserve(bound)
        env.decide(bound, "withdraw")
        open_item = item_in_sprint("not picked up")

        result = env.invoke("work.read.unbound", {"sprint_id": sprint_id})
        categories = result["categories"]
        assert [i["id"] for i in categories["legacy_done"]["items"]] == [legacy]
        assert [i["id"] for i in categories["decided_unreleased"]["items"]] == [unreleased]
        [picked] = categories["released_undecided"]["items"]
        assert (picked["id"], picked["release_digest"]) == (released, digest)
        assert {name: c["count"] for name, c in categories.items()} == {
            "legacy_done": 1, "decided_unreleased": 1, "released_undecided": 1,
            "accepted_without_evidence": 0,
        }
        assert open_item not in {
            i["id"] for c in categories.values() for i in c["items"]
        }
        assert result["resolutions"] == {
            "accepted": 1, "rejected": 0, "withdrawn": 1, "superseded": 0,
            "decided_done": 2, "legacy_done": 1, "legacy_remarked": 0, "done": 3,
        }

        # A re-mark takes the legacy item out of every unbound category: it
        # has no release to bind, and the re-mark is its repair.  It is
        # counted as legacy_remarked instead.
        env.decide(legacy, "reject")
        after = env.invoke("work.read.unbound", {"sprint_id": sprint_id})
        assert after["categories"]["legacy_done"] == {"count": 0, "items": []}
        assert [i["id"] for i in after["categories"]["decided_unreleased"]["items"]] == [
            unreleased
        ]
        assert after["resolutions"] == {
            "accepted": 1, "rejected": 1, "withdrawn": 1, "superseded": 0,
            "decided_done": 3, "legacy_done": 0, "legacy_remarked": 1, "done": 3,
        }
        only = env.invoke(
            "work.read.unbound", {"sprint_id": sprint_id, "category": "legacy_done"}
        )
        assert list(only["categories"]) == ["legacy_done"]

        second = item_in_sprint("also closed without a release")
        env.decide(second, "accept")
        limited = env.invoke("work.read.unbound", {"sprint_id": sprint_id, "limit": 1})
        assert limited["categories"]["decided_unreleased"]["count"] == 2
        assert len(limited["categories"]["decided_unreleased"]["items"]) == 1
        for arguments, status in (
            ({"category": "nope"}, 422),
            ({"limit": 0}, 422),
            ({"sprint_id": 99_999_999}, 404),
        ):
            with pytest.raises(ApplicationRejection) as rejected:
                env.invoke("work.read.unbound", arguments)
            assert rejected.value.http_status == status, arguments

    def _item_in_sprint(self, env, sprint_id, track_id, title):
        item_id = env.backend.create_work_item(env.store, sprint_id, track_id, title)
        env.backend.set_work_item_status(env.store, item_id, "active")
        return item_id

    def test_accepted_without_evidence_reports_never_blocks(self, env):
        anchor = env.new_item()
        item = env.backend.get_work_item(env.store, anchor)
        sprint_id, track_id = item["sprint_id"], item["track_id"]

        # ``item status --status done`` is the accept alias (TS-5): it takes
        # the item's default review-required release with no evidence.
        alias_item = self._item_in_sprint(env, sprint_id, track_id, "alias accept")
        alias_digest = env.reserve(alias_item)
        env.backend.set_work_item_status(env.store, alias_item, "done", actor="agent")

        # An explicit accept that also names no evidence takes the same
        # obligation through the other door.
        explicit_item = self._item_in_sprint(env, sprint_id, track_id, "explicit accept")
        env.reserve(explicit_item)
        explicit_decision = env.decide(explicit_item, "accept", evidence_digests=[])[
            "decision"
        ]

        # An explicit accept carrying evidence satisfies the obligation and
        # must not appear.
        evidenced_item = self._item_in_sprint(env, sprint_id, track_id, "evidenced accept")
        env.reserve(evidenced_item)
        env.decide(evidenced_item, "accept")

        result = env.invoke(
            "work.read.unbound",
            {"sprint_id": sprint_id, "category": "accepted_without_evidence"},
        )
        section = result["categories"]["accepted_without_evidence"]
        rows = {row["id"]: row for row in section["items"]}
        assert alias_item in rows and explicit_item in rows
        assert evidenced_item not in rows
        assert (rows[alias_item]["release_digest"], rows[alias_item]["accept_path"]) == (
            alias_digest, "alias",
        )
        assert (
            rows[explicit_item]["decision_id"], rows[explicit_item]["accept_path"]
        ) == (explicit_decision["id"], "explicit")
        # The write itself is never refused: both accepts above closed their
        # items normally.
        for item_id in (alias_item, explicit_item, evidenced_item):
            assert env.backend.get_work_item(env.store, item_id)["status"] == "done"

    def test_one_alias_accept_and_one_evidenced_explicit_accept_is_one_row(self, env):
        anchor = env.new_item()
        item = env.backend.get_work_item(env.store, anchor)
        sprint_id, track_id = item["sprint_id"], item["track_id"]

        alias_item = self._item_in_sprint(env, sprint_id, track_id, "alias accept")
        env.reserve(alias_item)
        env.backend.set_work_item_status(env.store, alias_item, "done", actor="agent")

        evidenced_item = self._item_in_sprint(env, sprint_id, track_id, "evidenced accept")
        env.reserve(evidenced_item)
        env.decide(evidenced_item, "accept")

        result = env.invoke(
            "work.read.unbound",
            {"sprint_id": sprint_id, "category": "accepted_without_evidence"},
        )
        section = result["categories"]["accepted_without_evidence"]
        assert section["count"] == 1
        [row] = section["items"]
        assert (row["id"], row["accept_path"]) == (alias_item, "alias")

    def test_notes_cannot_pose_as_decisions(self, env):
        item_id = env.new_item()
        sprint_id = env.backend.get_work_item(env.store, item_id)["sprint_id"]
        for event_type in ("item.done", "accept", "rejected", "work-decision.recorded", "Item-Done"):
            with pytest.raises(ApplicationRejection) as event:
                env.app.invoke(
                    "work.event.add",
                    {"sprint_id": sprint_id, "work_item_id": item_id, "event_type": event_type},
                    _context(),
                )
            assert (event.value.code, event.value.http_status) == (
                "decision-like-event-type", 422
            ), event_type
            with pytest.raises(ApplicationRejection) as note:
                env.app.invoke(
                    "work.item.note",
                    {"item_id": item_id, "note_type": event_type, "summary": "closed"},
                    _context(),
                )
            assert note.value.code == "decision-like-event-type", event_type
        # A design-decision knowledge note is still a note.
        note = env.app.invoke(
            "work.item.note",
            {"item_id": item_id, "note_type": "decision", "summary": "use pg"},
            _context(),
        )
        assert note["note_type"] == "decision"
        assert env.backend.get_work_item(env.store, item_id)["status"] == "active"

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

    def test_nul_characters_are_refused_before_storage(self, env):
        item_id = env.new_item()
        with pytest.raises(ApplicationRejection) as rationale:
            env.decide(item_id, "revise", rationale="bad\x00rationale")
        assert (rationale.value.code, rationale.value.http_status) == ("decision-rejected", 422)
        with pytest.raises(ApplicationRejection) as key:
            env.decide(item_id, "revise", key="bad\x00key")
        assert (key.value.code, key.value.http_status) == ("decision-rejected", 422)
        assert env.backend.list_decisions(env.store, item_id) == []

    def test_same_key_on_another_item_is_a_conflict(self, env):
        first, second = env.new_item(), env.new_item()
        key = _key()
        env.decide(first, "revise", key=key)
        with pytest.raises(ApplicationRejection) as rejected:
            env.decide(second, "revise", key=key)
        assert (rejected.value.code, rejected.value.http_status) == ("idempotency-conflict", 409)
        assert env.backend.list_decisions(env.store, second) == []

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


@_requires_312
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


@_requires_312
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


# --- Sprint export/import carries decisions as archive-only history ---------


def _export(runner, sprint_id, path):
    result = runner.invoke(
        cli, ["export", "--sprint-id", str(sprint_id), "--output", str(path)]
    )
    assert result.exit_code == 0, result.output
    return json.loads(path.read_text())


def test_sprint_with_decisions_and_edits_round_trips_through_export(
    runner, conn, tmp_path, monkeypatch
):
    item_id = _sqlite_item(conn)
    _row, revision = db.get_work_item_with_edit_revision(conn, item_id)
    db.update_work_item_description(
        conn, item_id, "edited", expected_revision=revision, actor="editor"
    )
    db.record_decision(
        conn, item_id, "withdraw", actor="alice", rationale="dropped",
        idempotency_key="source-key",
    )
    sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
    exported = _export(runner, sprint_id, tmp_path / "sprint.json")
    assert {"item-decided", "item-edited"} <= {e["event_type"] for e in exported["events"]}

    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("SPRINTCTL_DB", str(fresh))
    result = runner.invoke(cli, ["import", "--file", str(tmp_path / "sprint.json")])
    assert result.exit_code == 0, result.output

    target = db.get_connection(fresh)
    try:
        [imported_item] = target.execute("SELECT * FROM work_item").fetchall()
        assert imported_item["status"] == "done"
        events = target.execute(
            "SELECT event_type, payload FROM event WHERE work_item_id = ? ORDER BY id",
            (imported_item["id"],),
        ).fetchall()
        types = [row["event_type"] for row in events]
        assert "item-decided" not in types and "item-edited" not in types
        assert {"item-decided-imported", "item-edited-imported"} <= set(types)
        decided = next(
            json.loads(row["payload"]) for row in events
            if row["event_type"] == "item-decided-imported"
        )
        assert decided["source_event_type"] == "item-decided"
        assert decided["source_payload"]["kind"] == "withdraw"
        assert "idempotency_key" not in decided["source_payload"]
        assert "decision_id" not in decided["source_payload"]
        # The imported item starts a fresh edit history.
        _item, new_revision = db.get_work_item_with_edit_revision(target, imported_item["id"])
        assert "@description:v0@" in new_revision
    finally:
        target.close()


def test_import_refuses_reserved_events_before_writing_anything(
    runner, conn, tmp_path, monkeypatch
):
    item_id = _sqlite_item(conn)
    sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
    exported = _export(runner, sprint_id, tmp_path / "sprint.json")
    exported["events"].append(
        {
            "id": 999_999, "sprint_id": sprint_id, "work_item_id": item_id,
            "source_type": "actor", "actor": "x", "event_type": "session-capsule.recorded",
            "payload": "{}", "created_at": "2026-09-18T00:00:00Z",
        }
    )
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(exported))
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("SPRINTCTL_DB", str(fresh))
    result = runner.invoke(cli, ["import", "--file", str(path)])
    assert result.exit_code == 1
    assert "Import aborted" in result.output
    target = db.get_connection(fresh)
    try:
        db.init_db(target)
        assert target.execute("SELECT count(*) FROM sprint").fetchone()[0] == 0
        assert target.execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    finally:
        target.close()


def test_decision_history_types_cannot_be_written_generically(conn):
    item_id = _sqlite_item(conn)
    sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
    for event_type in ("item-decided", "item-decided-imported", "item-edited-imported"):
        with pytest.raises(ValueError, match="reserved"):
            db.create_event(conn, sprint_id, "x", event_type, work_item_id=item_id, payload={})
