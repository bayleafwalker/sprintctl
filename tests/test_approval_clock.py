"""Golden instant comparisons for PostgreSQL timestamps in a repeated hour."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from sprintctl import approval


@pytest.mark.parametrize("zone", ["UTC", "Europe/Helsinki"])
@pytest.mark.parametrize("expired", [False, True])
def test_approval_expiry_orders_instants_in_repeated_hour(zone, expired):
    # Helsinki repeats 03:00 on 2026-10-25. The earlier UTC instant has the
    # later wall clock, so comparing equal ZoneInfo objects reverses order.
    earlier = datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc)
    later = datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc)
    now, expiry = (later, earlier) if expired else (earlier, later)
    row = dict(approval_id="apr_" + "0" * 26, repo_id="repo", workspace_id="ws",
               run_id="run", transition="resume", rationale=None,
               approver_principal_id="operator", approver_client_id=None,
               approver_grant_id=None, recorded_at=earlier,
               evaluated_at=now.astimezone(ZoneInfo(zone)),
               expires_at=expiry.astimezone(ZoneInfo(zone)))

    class Cursor:
        def execute(self, sql, params):
            pass

        def fetchone(self):
            return row

    class Store:
        repo_id = "repo"

    result = approval.get(Cursor(), Store(), workspace_id="ws", approval_id=row["approval_id"])
    assert result["expired"] is expired
