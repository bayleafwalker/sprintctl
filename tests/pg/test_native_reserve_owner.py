"""Real native owner transaction/replay histories in disposable PG scopes."""

from dataclasses import replace
import threading
import time

import pytest

from sprintctl import pg, reservation
from sprintctl.application import ApplicationRejection, WorkApplication
from tests.test_native_reserve_owner import context
from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    dict_row,
    psycopg,
    assert_disposable_connection,
)
from tests.pg.test_work_lease import _items, TestMaintenance as _MaintenanceHistories

pytestmark = PG_MARKS
OPERATION = "work.reservation.reserve-v1"
TOOL = "reservation.reserve-v1"


@pytest.fixture
def native(store, pg_test_scope):
    scoped = replace(store, repo_id=pg_test_scope("native-reserve"))
    (item,) = _items(scoped)
    return scoped, item


def invoke(store, item, *, key="native-key-1", ctx=None, **overrides):
    return WorkApplication.postgres(store).invoke(
        OPERATION,
        {
            "item_id": item,
            "actor": "native-test",
            "session_id": "native-session",
            **overrides,
        },
        ctx or context(key=key),
    )["reservation"]


def counts(store):
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM reservation WHERE repo_id=%s) AS rows, "
            "(SELECT count(*) FROM event WHERE repo_id=%s AND event_type LIKE 'reservation.%%') AS events, "
            "(SELECT count(*) FROM work_idempotency_ledger WHERE repo_id=%s AND tool=%s) AS keys",
            (store.repo_id, store.repo_id, store.repo_id, TOOL),
        )
        return dict(cur.fetchone())


def sibling(store):
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return replace(store, conn=conn)


def test_native_reserve_normalized_replay_conflict_and_advisory_overlap(native):
    store, item = native
    first = invoke(store, item)
    assert not first["replayed"] and not first["conflict"]
    snapshot = first["admission_snapshot"]
    replay = invoke(
        store,
        item,
        role="execution",
        interrupt_existing=False,
        correlation_ref=None,
        expected_revision=None,
    )
    assert replay["replayed"] and replay["admission_snapshot"] == snapshot
    assert counts(store) == {"rows": 1, "events": 1, "keys": 1}
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item, correlation_ref="different")
    assert exc.value.code == "idempotency-conflict"
    second = invoke(store, item, key="native-key-2", session_id="another-session")
    assert second["conflict"] and second["conflict_severity"] == "warning"
    assert second["conflicting_reservations"][0]["id"] == first["id"]
    assert counts(store) == {"rows": 2, "events": 2, "keys": 2}


def test_native_reserve_binds_explicit_protected_contract_and_refuses_changed_replay(native):
    store, item = native
    basis = pg.item_release_revision(store, item)
    contract = {"review_required": True, "effect_verification_required": True}
    first = invoke(store, item, expected_revision=basis, acceptance_contract=contract)
    release = pg.get_release(store, first["release_digest"])
    assert release["acceptance_contract"]["effect_verification_required"] is True
    replay = invoke(store, item, expected_revision=basis, acceptance_contract=dict(reversed(list(contract.items()))))
    assert replay["replayed"] and replay["admission_snapshot"] == first["admission_snapshot"]
    before = counts(store)
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item, expected_revision=basis, acceptance_contract={"review_required": True})
    assert exc.value.code == "idempotency-conflict"
    assert counts(store) == before
    assert pg.get_release(store, first["release_digest"]) == release


@pytest.mark.parametrize("overrides", [
    {"acceptance_contract": {}},
    {"acceptance_contract": None},
    {"acceptance_contract": {}, "role": "observation"},
    {"acceptance_contract": {"effect_verification_required": "true"}},
])
def test_native_reserve_invalid_contract_never_claims_or_freezes(native, overrides):
    store, item = native
    if overrides.get("acceptance_contract") and overrides.get("role") != "observation":
        overrides = {**overrides, "expected_revision": pg.item_release_revision(store, item)}
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item, **overrides)
    assert exc.value.code == "invalid-arguments"
    assert counts(store) == {"rows": 0, "events": 0, "keys": 0}


def test_native_reserve_replay_tracks_current_lifecycle_without_changing_snapshot(
    native,
):
    store, item = native
    first = invoke(store, item)
    original = first["admission_snapshot"]
    pg.reassign_reservation(store, first["id"], actor="next", session_id="next-session")
    pg.release_reservation(store, first["id"], actor="next")
    before = pg.get_reservation(store, first["id"])
    replay = invoke(store, item)
    assert replay["replayed"] and replay["state"] == "released"
    assert replay["session_id"] == "next-session"
    assert replay["admission_snapshot"] == original and original["state"] == "active"
    assert reservation.parse_time(replay["last_activity_at"]) == reservation.parse_time(
        before["last_activity_at"]
    )
    assert not replay["conflict"] and not replay["conflicting_reservations"]


@pytest.mark.parametrize("stage", ["interruption", "reserved", "result"])
def test_native_reserve_failure_rolls_back_every_effect_and_allows_corrected_retry(
    native, monkeypatch, stage
):
    store, item = native
    previous = pg.reserve(store, item, actor="previous", session_id="previous-session")
    reviewer = pg.reserve(
        store,
        item,
        actor="reviewer",
        session_id="reviewer-session",
        role="verification",
    )
    before = counts(store)
    releases = pg.list_releases(store, item)
    event = pg._reservation_event_in_transaction
    with monkeypatch.context() as patch:

        def fail_event(*args, **kwargs):
            event_type = args[3]
            if event_type == (
                "reservation.interrupted"
                if stage == "interruption"
                else "reservation.reserved"
            ):
                raise RuntimeError("injected event failure")
            return event(*args, **kwargs)

        if stage == "result":
            patch.setattr(
                pg,
                "_record_idempotent_result",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected result failure")
                ),
            )
        else:
            patch.setattr(pg, "_reservation_event_in_transaction", fail_event)
        with pytest.raises(RuntimeError, match="injected"):
            invoke(store, item, interrupt_existing=True)
    assert counts(store) == before
    assert pg.get_reservation(store, previous["id"])["state"] == "active"
    assert pg.list_releases(store, item) == releases
    # Native failed effect leaves no permanent deny; corrected content can use key.
    fixed = invoke(store, item, interrupt_existing=False, correlation_ref="corrected")
    assert not fixed["replayed"] and fixed["conflict"]
    assert pg.get_reservation(store, reviewer["id"])["state"] == "active"


def test_native_reserve_stale_basis_rolls_back_claim_and_freeze(native):
    store, item = native
    requested = pg.item_release_revision(store, item)
    _, edit = pg.get_work_item_with_edit_revision(store, item)
    pg.update_work_item_description(
        store, item, "changed basis", expected_revision=edit, actor="editor"
    )
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item, expected_revision=requested)
    assert exc.value.code == "stale-basis"
    assert counts(store) == {"rows": 0, "events": 0, "keys": 0}
    assert not pg.list_releases(store, item)
    assert not invoke(store, item)["replayed"]


@pytest.mark.parametrize("after_commit", [False, True])
def test_native_reserve_commit_boundary_loss_and_exact_retry(
    native, after_commit, monkeypatch
):
    store, item = native
    real = store.conn

    original_commit = type(real).commit

    def fault_commit(conn):
        if conn is not real:
            return original_commit(conn)
        if after_commit:
            original_commit(conn)
        raise OSError("injected commit boundary loss")

    with monkeypatch.context() as patch:
        patch.setattr(type(real), "commit", fault_commit)
        with pytest.raises(OSError, match="commit boundary"):
            invoke(store, item)
    assert counts(store) == (
        {"rows": 1, "events": 1, "keys": 1}
        if after_commit
        else {"rows": 0, "events": 0, "keys": 0}
    )
    assert invoke(store, item)["replayed"] is after_commit
    assert counts(store) == {"rows": 1, "events": 1, "keys": 1}


def test_native_reserve_concurrent_same_key_waits_for_one_real_effect(
    native, monkeypatch
):
    store, item = native
    first_entered, proceed, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = pg.reserve_in_transaction
    results, failures = [], []
    connections = []

    def paused(*args, **kwargs):
        first_entered.set()
        assert proceed.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(pg, "reserve_in_transaction", paused)

    def worker(index):
        peer = sibling(store)
        connections.append(peer.conn)
        try:
            if index:
                second_entered.set()
            results.append(invoke(peer, item))
        except BaseException as exc:
            failures.append(exc)
        finally:
            peer.conn.close()

    first = threading.Thread(target=worker, args=(0,))
    first.start()
    assert first_entered.wait(10)
    second = threading.Thread(target=worker, args=(1,))
    second.start()
    assert second_entered.wait(10)
    # Observe actual transaction-key blocking before releasing the winner.
    deadline = time.monotonic() + 10
    waiting = False
    while time.monotonic() < deadline:
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE pid=%s AND NOT granted AND locktype='transactionid'",
                (connections[1].info.backend_pid,),
            )
            waiting = cur.fetchone()["n"] > 0
        if waiting:
            break
        time.sleep(0.01)
    proceed.set()
    first.join(15)
    second.join(15)
    assert waiting and not first.is_alive() and not second.is_alive() and not failures
    assert sorted(row["replayed"] for row in results) == [False, True]
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["admission_snapshot"] == results[1]["admission_snapshot"]
    assert counts(store) == {"rows": 1, "events": 1, "keys": 1}


def test_native_reserve_auth_scope_revocation_and_legacy_paths(native):
    store, item = native
    first = invoke(store, item)
    foreign = context()
    foreign.identity.principal_id = "github:2:0"
    assert invoke(store, item, ctx=foreign)["id"] != first["id"]
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item, ctx=context(authority=False))
    assert exc.value.code == "authority-required"
    # Existing unbound actor-only hosted operation still admits a fresh advisory row.
    legacy = WorkApplication.postgres(store).invoke(
        "work.reservation.reserve",
        {
            "item_id": item,
            "actor": "native-test",
            "session_id": "legacy-session",
        },
        context(bound=False),
    )["reservation"]
    assert legacy["conflict"] and "replayed" not in legacy


def test_native_reserve_replay_after_real_maintenance_activation_skips_new_admission(
    native,
):
    store, item = native
    first = invoke(store, item)
    pg.release_reservation(store, first["id"])
    attested = _MaintenanceHistories._attested(store)
    _MaintenanceHistories._activate(*attested)
    replay = invoke(store, item)
    assert replay["replayed"] and replay["state"] == "released"
    with pytest.raises(ApplicationRejection, match="disabled"):
        invoke(store, item, key="native-key-new")
    assert counts(store)["keys"] == 1


@pytest.mark.parametrize("path", ["direct", "native"])
def test_native_reserve_event_failure_never_leaves_new_release(
    native, monkeypatch, path
):
    store, item = native
    monkeypatch.setattr(
        pg,
        "_reservation_event_in_transaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("legacy event failure")
        ),
    )
    with pytest.raises(RuntimeError, match="legacy event"):
        if path == "direct":
            pg.reserve(store, item, actor="legacy", session_id="legacy")
        else:
            invoke(store, item)
    assert counts(store) == {"rows": 0, "events": 0, "keys": 0}
    assert not pg.list_releases(store, item)


@pytest.mark.parametrize(
    "phase", ["before_repo", "after_repo", "after_item", "after_rows", "after_result"]
)
def test_native_reserve_maintenance_interleaves_at_each_lock_boundary(
    native, monkeypatch, phase
):
    store, item = native
    attested = _MaintenanceHistories._attested(store)
    capability_id, revision = attested[1:]
    entered, proceed, activation_started = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    participant, record = pg.reserve_in_transaction, pg._record_idempotent_result
    outcomes, errors, activation_conn = {}, {}, []

    def pause():
        entered.set()
        assert proceed.wait(10)

    class CursorBoundary:
        def __init__(self, cur):
            self.cur = cur

        def __getattr__(self, name):
            return getattr(self.cur, name)

        def execute(self, sql, params=None):
            value = self.cur.execute(sql, params)
            if (
                (
                    phase == "after_repo"
                    and sql.startswith("SELECT pg_advisory_xact_lock")
                )
                or (phase == "after_item" and "FOR UPDATE OF wi" in sql)
                or (phase == "after_rows" and "ORDER BY id FOR UPDATE" in sql)
            ):
                pause()
            return value

    def effect(cur, *args, **kwargs):
        if phase == "before_repo":
            pause()
        return participant(CursorBoundary(cur), *args, **kwargs)

    def result(*args, **kwargs):
        value = record(*args, **kwargs)
        if phase == "after_result":
            pause()
        return value

    monkeypatch.setattr(pg, "reserve_in_transaction", effect)
    monkeypatch.setattr(pg, "_record_idempotent_result", result)

    def reserve_worker():
        peer = sibling(store)
        try:
            outcomes["reserve"] = invoke(peer, item)
        except Exception as exc:
            errors["reserve"] = exc
        finally:
            peer.conn.close()

    def activate_worker():
        peer = sibling(store)
        activation_conn.append(peer.conn)
        try:
            from sprintctl.maintenance_capability import (
                PostgresMaintenanceCapabilityStore,
            )

            activation_started.set()
            _MaintenanceHistories._activate(
                PostgresMaintenanceCapabilityStore(peer), capability_id, revision
            )
            outcomes["activate"] = True
        except Exception as exc:
            errors["activate"] = exc
        finally:
            peer.conn.close()

    reserve_thread = threading.Thread(target=reserve_worker)
    reserve_thread.start()
    assert entered.wait(10)
    activate_thread = threading.Thread(target=activate_worker)
    activate_thread.start()
    assert activation_started.wait(10)
    try:
        if phase == "before_repo":
            activate_thread.join(10)
            assert not activate_thread.is_alive() and outcomes.get("activate") is True
        else:
            waiting = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with store.conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) AS n FROM pg_locks WHERE pid=%s AND NOT granted AND locktype='advisory'",
                        (activation_conn[0].info.backend_pid,),
                    )
                    waiting = cur.fetchone()["n"] > 0
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "activation must actually wait on the held repository lock"
    finally:
        proceed.set()
        reserve_thread.join(15)
        activate_thread.join(15)
    assert not reserve_thread.is_alive() and not activate_thread.is_alive()
    if phase == "before_repo":
        assert isinstance(errors.get("reserve"), ApplicationRejection)
        assert errors["reserve"].code == "reservations-disabled-maintenance"
        assert "disabled" in str(errors["reserve"])
        assert counts(store) == {"rows": 0, "events": 0, "keys": 0}
        assert not pg.list_releases(store, item)
    else:
        from sprintctl.maintenance_capability import MaintenanceCapabilityError

        assert "reserve" in outcomes and isinstance(
            errors.get("activate"), MaintenanceCapabilityError
        )
        assert "reservation" in str(errors["activate"])
        assert counts(store) == {"rows": 1, "events": 1, "keys": 1}


def test_native_reserve_explicit_takeover_once_and_scope_isolation(native):
    store, item = native
    old = pg.reserve(store, item, actor="old", session_id="old-session")
    verification = pg.reserve(
        store, item, actor="review", session_id="review", role="verification"
    )
    observation = pg.reserve(
        store, item, actor="observer", session_id="observer", role="observation"
    )
    first = invoke(store, item, interrupt_existing=True)
    assert pg.get_reservation(store, old["id"])["state"] == "interrupted"
    assert pg.get_reservation(store, verification["id"])["state"] == "active"
    assert pg.get_reservation(store, observation["id"])["state"] == "active"
    before = counts(store)
    replay = invoke(store, item, interrupt_existing=True)
    assert (
        replay["replayed"]
        and replay["admission_snapshot"] == first["admission_snapshot"]
    )
    assert counts(store) == before
    other_workspace = context()
    other_workspace.identity.workspace_id = "ws-other"
    assert invoke(store, item, ctx=other_workspace)["id"] != first["id"]


def test_native_reserve_missing_retained_row_never_recreates(native):
    store, item = native
    first = invoke(store, item)
    # Disposable corruption fixture, never a production repair path.
    with store.conn.cursor() as cur:
        cur.execute(
            "DELETE FROM reservation WHERE repo_id=%s AND id=%s",
            (store.repo_id, first["id"]),
        )
    store.conn.commit()
    with pytest.raises(ApplicationRejection) as exc:
        invoke(store, item)
    assert exc.value.code == "reservation-receipt-unavailable"
    assert counts(store) == {"rows": 0, "events": 1, "keys": 1}


def test_native_reserve_current_overlap_format_matches_legacy_in_non_utc_session(
    native,
):
    import json
    from datetime import timezone

    store, item = native
    with store.conn.cursor() as cur:
        cur.execute("SHOW TimeZone")
        previous_zone = cur.fetchone()["TimeZone"]
        cur.execute("SELECT set_config('TimeZone', 'Pacific/Auckland', false)")
    store.conn.commit()
    try:
        legacy = WorkApplication.postgres(store).invoke(
            "work.reservation.reserve",
            {
                "item_id": item,
                "actor": "native-test",
                "session_id": "legacy-overlap",
            },
            context(bound=False),
        )["reservation"]
        first = invoke(store, item)
        # Match the legacy hosted row's JSON-safe shape for the identical overlap.
        legacy_json = json.loads(
            json.dumps(
                legacy,
                default=lambda value: value.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
        )
        expected = reservation.annotate_conflicts(
            dict(first["admission_snapshot"]), [legacy_json]
        )
        assert (
            first["conflicting_reservations"]
            == first["admission_snapshot"]["conflicting_reservations"]
            == expected["conflicting_reservations"]
        )
        assert first["conflict_severity"] == expected["conflict_severity"]
        # A later commit changes current overlaps without rewriting admission.
        pg.reserve(store, item, actor="later", session_id="later", role="observation")
        replay = invoke(store, item)
        assert replay["admission_snapshot"] == first["admission_snapshot"]
        assert len(replay["conflicting_reservations"]) == 2
        assert len(replay["admission_snapshot"]["conflicting_reservations"]) == 1
    finally:
        store.conn.rollback()
        with store.conn.cursor() as cur:
            cur.execute("SELECT set_config('TimeZone', %s, false)", (previous_zone,))
        store.conn.commit()


def test_native_reserve_active_same_session_replay_never_advances_activity(native):
    store, item = native
    first = invoke(store, item)
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE reservation SET last_activity_at='2020-01-01T00:00:00Z' WHERE repo_id=%s AND id=%s",
            (store.repo_id, first["id"]),
        )
    store.conn.commit()
    before = pg.get_reservation(store, first["id"])
    replay = invoke(store, item)
    assert (
        replay["replayed"]
        and replay["state"] == "active"
        and replay["session_id"] == "native-session"
    )
    assert reservation.parse_time(replay["last_activity_at"]) == reservation.parse_time(
        before["last_activity_at"]
    )
    assert replay["admission_snapshot"] == first["admission_snapshot"]
    assert OPERATION not in reservation.ACTIVITY_OPERATIONS
    assert "work.reservation.reserve" not in reservation.ACTIVITY_OPERATIONS


def test_native_reserve_participant_refuses_a_different_event_connection(native):
    store, item = native
    peer = sibling(store)
    try:
        with peer.conn.cursor() as cur:
            with pytest.raises(RuntimeError, match="share its event connection"):
                pg.reserve_in_transaction(
                    cur, store, item, actor="different", session_id="different"
                )
        assert counts(store) == {"rows": 0, "events": 0, "keys": 0}
        assert not pg.list_releases(store, item)
    finally:
        peer.conn.close()
