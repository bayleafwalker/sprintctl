from __future__ import annotations

import json

import pytest

from sprintctl import db, volatile_context
from sprintctl.volatile_hook import FileCursor, MemoryCursor, VolatileContextHookAdapter


def _item(conn, active_sprint, *, title="Projected item") -> int:
    track = db.get_or_create_track(conn, active_sprint["id"], "volatile-context")
    return db.create_work_item(conn, active_sprint["id"], track, title)


def test_projection_is_bounded_allowlisted_and_uses_owner_status_revision(
    conn, active_sprint
):
    item_id = _item(conn, active_sprint, title="x" * 2_000)

    result = volatile_context.project_work_item(
        db, conn, repo_id="repo-a", item_id=item_id
    )

    assert result is not None
    assert result["revision"] == db.item_status_revision(db.get_work_item(conn, item_id))
    assert result["data_class"] == "untrusted-work-state"
    assert result["truncated"] is True
    assert set(result["item"]) == {"id", "title", "status", "priority", "assignee"}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert len(encoded) <= volatile_context.MAX_PROJECTION_BYTES


def test_precheck_is_read_only_and_authority_still_rejects_hook_bypass(
    conn, active_sprint
):
    item_id = _item(conn, active_sprint)
    before = db.get_work_item(conn, item_id)
    basis = db.item_status_revision(before)
    assert volatile_context.validate_status_mutation(
        db, conn, repo_id="repo-a", item_id=item_id, expected_revision=basis
    )["allowed"] is True

    db.set_work_item_status(conn, item_id, "active", expected_revision=basis)
    with pytest.raises(db.StatusConflict):
        db.set_work_item_status(conn, item_id, "done", expected_revision=basis)

    stale = volatile_context.validate_status_mutation(
        db, conn, repo_id="repo-a", item_id=item_id, expected_revision=basis
    )
    assert stale["allowed"] is False
    assert stale["current_revision"] != basis


def _adapter(project, validate, cursor=None):
    return VolatileContextHookAdapter(
        repo_id="repo-a",
        item_id=7,
        project=project,
        validate=validate,
        cursor=cursor or MemoryCursor(),
    )


def _projection(revision="item:00000000-0000-4000-8000-000000000007@status:pending"):
    return {
        "projection": {
            "contract_version": "work-item-context/v1",
            "provider_id": "sprintctl.work-item",
            "resource_id": "repo-a#7",
            "revision": revision,
            "data_class": "untrusted-work-state",
            "item": {"id": 7, "title": "Task", "status": "pending"},
            "truncated": False,
        }
    }


def test_hook_reads_fail_open_and_delta_cursor_is_per_session():
    cursor = MemoryCursor()
    adapter = _adapter(lambda _item_id: _projection(), lambda *_args: {}, cursor)
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-a",
        "agent_id": None,
    }
    assert "additionalContext" in adapter.handle(event)["hookSpecificOutput"]
    assert adapter.handle(event) == {}
    assert "additionalContext" in adapter.handle({**event, "session_id": "session-b"})[
        "hookSpecificOutput"
    ]

    unavailable = _adapter(
        lambda _item_id: (_ for _ in ()).throw(ConnectionError("offline")),
        lambda *_args: {},
    )
    assert unavailable.handle({"hook_event_name": "SessionStart"}) == {}


def test_hook_recognized_mutation_fails_closed_but_unknown_tool_fails_open():
    unavailable = _adapter(
        lambda _item_id: _projection(),
        lambda *_args: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    recognized = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__sprintctl__item_status",
        "tool_input": {"item_id": 7, "expected_revision": "revision"},
    }
    denied = unavailable.handle(recognized)["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "unavailable" in denied["permissionDecisionReason"]
    assert unavailable.handle({**recognized, "tool_name": "Bash"}) == {}


def test_hook_stale_precheck_returns_current_bounded_projection():
    response = _projection()
    adapter = _adapter(
        lambda _item_id: response,
        lambda _item_id, _revision: {
            "allowed": False,
            "reason": "item status revision changed",
            "current_revision": response["projection"]["revision"],
            "projection": response["projection"],
        },
    )
    denied = adapter.handle(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "sprintctl.item_status",
            "tool_input": {"item_id": 7, "expected_revision": "stale"},
        }
    )["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny"
    assert "volatile_context" in denied["additionalContext"]


def test_file_cursor_is_disposable_private_and_rejects_unsafe_root(tmp_path):
    root = tmp_path / "cursor"
    cursor = FileCursor(root)
    cursor.put("consumer", "revision-1")
    assert cursor.get("consumer") == "revision-1"
    assert root.stat().st_mode & 0o777 == 0o700
    assert next(root.iterdir()).stat().st_mode & 0o777 == 0o600

    root.chmod(0o755)
    assert cursor.get("consumer") is None
    cursor.put("consumer", "revision-2")
    assert next(root.iterdir()).read_text(encoding="utf-8").strip() == "revision-1"
