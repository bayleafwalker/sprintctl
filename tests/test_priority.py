"""
Tests for the native work item priority field (schema v10): validation,
storage, next-work ordering, legacy [pN] title-prefix fallback, and the CLI
surfaces (item add --priority, item priority, item list, next-work).
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", priority=None):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title, priority=priority)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidatePriority:
    def test_none_passes(self):
        assert db.validate_priority(None) is None

    @pytest.mark.parametrize("value", [1, 5, 9])
    def test_in_range_passes(self, value):
        assert db.validate_priority(value) == value

    @pytest.mark.parametrize("value", [0, 10, -1])
    def test_out_of_range_raises(self, value):
        with pytest.raises(ValueError, match="between 1 and 9"):
            db.validate_priority(value)

    @pytest.mark.parametrize("value", ["1", 1.5, True])
    def test_non_int_raises(self, value):
        with pytest.raises(ValueError):
            db.validate_priority(value)


class TestEffectivePriority:
    def test_native_priority_wins(self):
        assert db.effective_priority({"priority": 2, "title": "[p1] x"}) == 2

    def test_title_prefix_fallback(self):
        assert db.effective_priority({"priority": None, "title": "[p3] fix it"}) == 3

    def test_no_priority(self):
        assert db.effective_priority({"priority": None, "title": "plain title"}) is None

    def test_prefix_requires_leading_position(self):
        assert db.effective_priority({"priority": None, "title": "fix [p1] later"}) is None


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

class TestPriorityDB:
    def test_create_with_priority(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"], priority=2)
        assert db.get_work_item(conn, iid)["priority"] == 2

    def test_create_default_unprioritized(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        assert db.get_work_item(conn, iid)["priority"] is None

    def test_set_and_clear_priority(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_priority(conn, iid, 1)
        assert db.get_work_item(conn, iid)["priority"] == 1
        db.set_work_item_priority(conn, iid, None)
        assert db.get_work_item(conn, iid)["priority"] is None

    def test_set_priority_missing_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.set_work_item_priority(conn, 99999, 1)

    def test_create_invalid_priority_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="between 1 and 9"):
            _item(conn, active_sprint["id"], priority=0)


class TestNextWorkOrdering:
    def test_priority_orders_ready_items(self, conn, active_sprint):
        sid = active_sprint["id"]
        iid_plain = _item(conn, sid, "plain")
        iid_p3 = _item(conn, sid, "three", priority=3)
        iid_p1 = _item(conn, sid, "one", priority=1)
        ready = db.get_ready_items(conn, sid)
        assert [it["id"] for it in ready] == [iid_p1, iid_p3, iid_plain]

    def test_title_prefix_fallback_orders(self, conn, active_sprint):
        sid = active_sprint["id"]
        iid_plain = _item(conn, sid, "plain")
        iid_prefix = _item(conn, sid, "[p2] legacy convention")
        ready = db.get_ready_items(conn, sid)
        assert [it["id"] for it in ready] == [iid_prefix, iid_plain]

    def test_native_priority_beats_title_prefix(self, conn, active_sprint):
        sid = active_sprint["id"]
        iid_prefix = _item(conn, sid, "[p1] stale label", )
        db.set_work_item_priority(conn, iid_prefix, 5)
        iid_native = _item(conn, sid, "native", priority=2)
        ready = db.get_ready_items(conn, sid)
        assert [it["id"] for it in ready] == [iid_native, iid_prefix]

    def test_ready_items_expose_effective_priority(self, conn, active_sprint):
        sid = active_sprint["id"]
        _item(conn, sid, "[p4] legacy")
        ready = db.get_ready_items(conn, sid)
        assert ready[0]["effective_priority"] == 4

    def test_equal_priority_keeps_creation_order(self, conn, active_sprint):
        sid = active_sprint["id"]
        iid_a = _item(conn, sid, "A", priority=2)
        iid_b = _item(conn, sid, "B", priority=2)
        ready = db.get_ready_items(conn, sid)
        assert [it["id"] for it in ready] == [iid_a, iid_b]


# ---------------------------------------------------------------------------
# CLI surfaces
# ---------------------------------------------------------------------------

class TestPriorityCLI:
    def test_item_add_with_priority_json(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, [
            "item", "add", "--sprint-id", str(active_sprint["id"]),
            "--track", "eng", "--title", "urgent", "--priority", "1", "--json",
        ])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["priority"] == 1

    def test_item_add_invalid_priority_rejected(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, [
            "item", "add", "--sprint-id", str(active_sprint["id"]),
            "--track", "eng", "--title", "bad", "--priority", "12",
        ])
        assert result.exit_code != 0
        assert "between 1 and 9" in result.output

    def test_item_priority_set(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "priority", "--id", str(iid), "--set", "2"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["priority"] == 2

    def test_item_priority_clear(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"], priority=3)
        result = runner.invoke(cli, ["item", "priority", "--id", str(iid), "--clear"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["priority"] is None

    def test_item_priority_requires_exactly_one_mode(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "priority", "--id", str(iid)])
        assert result.exit_code != 0
        result = runner.invoke(cli, [
            "item", "priority", "--id", str(iid), "--set", "1", "--clear",
        ])
        assert result.exit_code != 0

    def test_item_priority_missing_item_fails(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, ["item", "priority", "--id", "99999", "--set", "1"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_item_list_shows_priority_column(self, runner, conn, active_sprint, db_path):
        _item(conn, active_sprint["id"], "urgent", priority=1)
        result = runner.invoke(cli, ["item", "list", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "PRI" in result.output
        assert "p1" in result.output

    def test_item_list_json_includes_priority(self, runner, conn, active_sprint, db_path):
        _item(conn, active_sprint["id"], "urgent", priority=2)
        result = runner.invoke(cli, [
            "item", "list", "--sprint-id", str(active_sprint["id"]), "--json",
        ])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)[0]["priority"] == 2

    def test_next_work_orders_by_priority(self, runner, conn, active_sprint, db_path):
        sid = active_sprint["id"]
        iid_plain = _item(conn, sid, "plain")
        iid_p1 = _item(conn, sid, "urgent", priority=1)
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(sid), "--json"])
        assert result.exit_code == 0, result.output
        ready = json.loads(result.output)
        assert [it["id"] for it in ready] == [iid_p1, iid_plain]
