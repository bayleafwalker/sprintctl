from datetime import datetime, timedelta

import pytest

from sprintctl import calc


NOW = datetime(2026, 3, 26, 12, 0, 0)
THRESHOLD = timedelta(hours=4)


# ---------------------------------------------------------------------------
# item_staleness
# ---------------------------------------------------------------------------

def _item(status, updated_at):
    return {"id": 1, "status": status, "updated_at": updated_at}


def test_item_staleness_stale_active():
    updated = (NOW - timedelta(hours=6, minutes=12)).isoformat()
    result = calc.item_staleness(_item("active", updated), NOW, THRESHOLD)
    assert result["is_stale"] is True
    assert result["idle_seconds"] == pytest.approx(6 * 3600 + 12 * 60, abs=1)


def test_item_staleness_pending_never_stale_by_default():
    # pending items are backlog — no pending_threshold means never stale
    updated = (NOW - timedelta(days=30)).isoformat()
    result = calc.item_staleness(_item("pending", updated), NOW, THRESHOLD)
    assert result["is_stale"] is False


def test_item_staleness_pending_stale_when_threshold_set():
    updated = (NOW - timedelta(hours=11, minutes=3)).isoformat()
    result = calc.item_staleness(
        _item("pending", updated), NOW, THRESHOLD, pending_threshold=timedelta(hours=8)
    )
    assert result["is_stale"] is True


def test_item_staleness_pending_not_stale_below_pending_threshold():
    updated = (NOW - timedelta(hours=5)).isoformat()
    result = calc.item_staleness(
        _item("pending", updated), NOW, THRESHOLD, pending_threshold=timedelta(hours=8)
    )
    assert result["is_stale"] is False


def test_item_staleness_not_stale():
    updated = (NOW - timedelta(hours=1)).isoformat()
    result = calc.item_staleness(_item("active", updated), NOW, THRESHOLD)
    assert result["is_stale"] is False


def test_item_staleness_done_never_stale():
    updated = (NOW - timedelta(days=7)).isoformat()
    result = calc.item_staleness(_item("done", updated), NOW, THRESHOLD)
    assert result["is_stale"] is False


def test_item_staleness_blocked_never_stale():
    updated = (NOW - timedelta(days=2)).isoformat()
    result = calc.item_staleness(_item("blocked", updated), NOW, THRESHOLD)
    assert result["is_stale"] is False


def test_item_staleness_exactly_at_threshold_not_stale():
    updated = (NOW - THRESHOLD).isoformat()
    result = calc.item_staleness(_item("active", updated), NOW, THRESHOLD)
    assert result["is_stale"] is False


# ---------------------------------------------------------------------------
# track_health
# ---------------------------------------------------------------------------

def _items(*statuses):
    return [{"id": i, "status": s} for i, s in enumerate(statuses, 1)]


def test_track_health_empty():
    result = calc.track_health([])
    assert result["total"] == 0
    assert result["blocked_ratio"] == 0.0
    assert result["done_ratio"] == 0.0


def test_track_health_all_done():
    result = calc.track_health(_items("done", "done", "done"))
    assert result["done_ratio"] == 1.0
    assert result["blocked_ratio"] == 0.0
    assert result["counts"]["done"] == 3


def test_track_health_mixed():
    result = calc.track_health(_items("done", "active", "pending", "blocked"))
    assert result["total"] == 4
    assert result["done_ratio"] == 0.25
    assert result["blocked_ratio"] == 0.25


def test_track_health_high_blocked():
    result = calc.track_health(_items("blocked", "blocked", "active"))
    assert result["blocked_ratio"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# sprint_overrun_risk
# ---------------------------------------------------------------------------

def _sprint(end_date, status="active"):
    return {"id": 1, "end_date": end_date, "status": status}


def test_sprint_overrun_risk_at_risk():
    end = (NOW + timedelta(days=1)).date().isoformat()
    result = calc.sprint_overrun_risk(_sprint(end), active_items=3, now=NOW)
    assert result["at_risk"] is True
    assert result["overdue"] is False


def test_sprint_overrun_risk_overdue():
    end = (NOW - timedelta(days=1)).date().isoformat()
    result = calc.sprint_overrun_risk(_sprint(end, "active"), active_items=0, now=NOW)
    assert result["overdue"] is True
    assert result["at_risk"] is False


def test_sprint_overrun_risk_healthy():
    end = (NOW + timedelta(days=10)).date().isoformat()
    result = calc.sprint_overrun_risk(_sprint(end), active_items=5, now=NOW)
    assert result["at_risk"] is False
    assert result["overdue"] is False


def test_sprint_overrun_risk_at_risk_boundary_no_active():
    # 1 day remaining but no active items — not at risk
    end = (NOW + timedelta(days=1)).date().isoformat()
    result = calc.sprint_overrun_risk(_sprint(end), active_items=0, now=NOW)
    assert result["at_risk"] is False


def test_sprint_overrun_risk_overdue_closed_not_flagged():
    # overdue but already closed — should not flag overdue
    end = (NOW - timedelta(days=3)).date().isoformat()
    result = calc.sprint_overrun_risk(_sprint(end, "closed"), active_items=0, now=NOW)
    assert result["overdue"] is False
