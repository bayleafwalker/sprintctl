"""PostgreSQL integration tests: CapabilityAdmissionUsesDatabaseTime.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    pg,
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
    AT,
    envelope,
    shifted_envelope,
    _stamp,
    PG_MARKS,
    datetime,
    timedelta,
    timezone,
    time,
    uuid,
)

pytestmark = PG_MARKS


class TestCapabilityAdmissionUsesDatabaseTime:
    """#2093 -- database time authorizes transitions; caller `at` is audit data.

    Claim admission filters live capabilities on the database clock. If a
    caller could authorize a transition with its own timestamp, a delayed,
    retried, or replayed request could drive a capability into a state that
    admission no longer honors, breaking the mutual exclusion Plan 1 relies on.
    """

    def _lifecycle(self, store, pg_test_scope, label):
        repo_id = pg_test_scope(label)
        return PostgresMaintenanceCapabilityStore(
            pg.PgStore(conn=store.conn, repo_id=repo_id)
        ), repo_id

    def _attested(self, lifecycle, value):
        capability_id = f"mcap:{uuid.uuid4()}"
        prepared = lifecycle.prepare(
            capability_id=capability_id, request_id=str(uuid.uuid4()),
            envelope=value, actor="operator", at=AT,
        )
        attested = lifecycle.transition(
            capability_id=capability_id, request_id=str(uuid.uuid4()),
            action="attest", expected_revision=prepared["revision"],
            actor="operator", at=AT, effect_ref="sha256:" + "0" * 64,
        )
        return capability_id, attested

    def _activate(self, lifecycle, capability_id, attested, at):
        return lifecycle.transition(
            capability_id=capability_id, request_id=str(uuid.uuid4()),
            action="activate", expected_revision=attested["revision"],
            actor="operator", at=at, step_id="attest-backup",
            command_id="verify-backup", command_ref="sha256:" + "1" * 64,
            effect_ref="sha256:" + "2" * 64,
        )

    def test_stale_pre_expiry_at_cannot_activate_after_database_expiry(
        self, store, pg_test_scope
    ):
        lifecycle, _ = self._lifecycle(store, pg_test_scope, "capability-stale-at")
        capability_id, attested = self._attested(
            lifecycle, shifted_envelope(timedelta(hours=-3))
        )
        result = self._activate(
            lifecycle, capability_id, attested,
            at=_stamp(timedelta(hours=-2, minutes=-30)),
        )
        assert result["state"] == result["outcome"] == "expired"

    def test_future_at_cannot_make_a_transition_admissible_early(
        self, store, pg_test_scope
    ):
        lifecycle, _ = self._lifecycle(store, pg_test_scope, "capability-future-at")
        capability_id, attested = self._attested(
            lifecycle, shifted_envelope(timedelta(hours=2))
        )
        with pytest.raises(MaintenanceCapabilityError, match="not_before"):
            self._activate(
                lifecycle, capability_id, attested,
                at=_stamp(timedelta(hours=2, minutes=30)),
            )
        assert lifecycle.get(capability_id)["state"] == "attested"

    def test_transaction_opened_before_expiry_is_rejected_when_the_statement_runs_after(
        self, store, pg_test_scope
    ):
        """The reason this uses statement_timestamp() rather than now().

        now() is fixed at transaction start, so a transaction opened while the
        window was still open would keep looking admissible after it closed.
        """
        lifecycle, _ = self._lifecycle(store, pg_test_scope, "capability-delayed-txn")
        window_closes_in = 3
        value = envelope()
        value["window"] = {
            "not_before": _stamp(timedelta(minutes=-30)),
            "expires_at": _stamp(timedelta(seconds=window_closes_in)),
        }
        # JIT deadlines must sit inside the (now very short) window.
        for definition in value["jit_fields"]:
            definition["bind_by"] = _stamp(timedelta(seconds=window_closes_in - 1))
        capability_id, attested = self._attested(lifecycle, value)

        # Open the transaction while the window is still open. This is what
        # pins now() to a pre-expiry instant.
        with store.conn.cursor() as cur:
            cur.execute("SELECT now() AS pinned")
            pinned = cur.fetchone()["pinned"]
        time.sleep(window_closes_in + 2)
        with store.conn.cursor() as cur:
            cur.execute("SELECT now() AS still_pinned, statement_timestamp() AS live")
            row = cur.fetchone()
        assert row["still_pinned"] == pinned, "now() must still be the stale instant"
        assert row["live"] > pinned, "statement_timestamp() must have advanced"

        result = self._activate(lifecycle, capability_id, attested, at=AT)
        assert result["state"] == result["outcome"] == "expired"

    def test_replay_after_expiry_is_rejected(self, store, pg_test_scope):
        lifecycle, _ = self._lifecycle(store, pg_test_scope, "capability-replay")
        capability_id, attested = self._attested(
            lifecycle, shifted_envelope(timedelta(hours=-3))
        )
        first = self._activate(lifecycle, capability_id, attested, at=AT)
        assert first["outcome"] == "expired"
        replay = self._activate(lifecycle, capability_id, first, at=AT)
        assert replay["outcome"] == "expired"
        assert lifecycle.get(capability_id)["state"] == "expired"

    def test_expiry_boundary_is_deterministic(self, store, pg_test_scope):
        lifecycle, _ = self._lifecycle(store, pg_test_scope, "capability-boundary")
        capability_id, attested = self._attested(
            lifecycle, shifted_envelope(timedelta(hours=-1, seconds=-2))
        )
        result = self._activate(lifecycle, capability_id, attested, at=AT)
        assert result["state"] == result["outcome"] == "expired"

    def test_rejected_activation_leaves_no_misleading_active_state(
        self, store, pg_test_scope
    ):
        lifecycle, repo_id = self._lifecycle(store, pg_test_scope, "capability-no-trace")
        capability_id, attested = self._attested(
            lifecycle, shifted_envelope(timedelta(hours=2))
        )
        with pytest.raises(MaintenanceCapabilityError):
            self._activate(lifecycle, capability_id, attested, at=AT)
        assert lifecycle.get(capability_id)["state"] == "attested"
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM maintenance_capability_receipt "
                "WHERE repo_id=%s AND capability_id=%s AND action='activate'",
                (repo_id, capability_id),
            )
            assert int(cur.fetchone()["count"]) == 0

    def test_caller_time_is_retained_as_audit_and_is_not_the_decision_time(
        self, store, pg_test_scope
    ):
        lifecycle, repo_id = self._lifecycle(store, pg_test_scope, "capability-audit")
        capability_id, attested = self._attested(lifecycle, envelope())
        active = self._activate(lifecycle, capability_id, attested, at=AT)
        assert active["outcome"] == "accepted"
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM maintenance_capability_receipt "
                "WHERE repo_id=%s AND capability_id=%s AND action='activate'",
                (repo_id, capability_id),
            )
            recorded = cur.fetchone()["created_at"]
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        # The caller's `at` is preserved verbatim as the recorded event time...
        assert recorded == datetime.fromisoformat(AT.replace("Z", "+00:00"))
        # ...and is nowhere near the database clock that actually admitted it.
        assert abs(
            (datetime.now(timezone.utc) - recorded).total_seconds()
        ) > 3600
