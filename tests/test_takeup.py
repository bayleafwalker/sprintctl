import json

from sprintctl import db


def _takeup(conn, sprint_id, actor, instance_id, **payload):
    return db.create_event(
        conn,
        sprint_id,
        actor,
        "sprint-taken-up",
        payload={"instance_id": instance_id, **payload},
    )


def _release(conn, sprint_id, actor, instance_id, **payload):
    return db.create_event(
        conn,
        sprint_id,
        actor,
        "sprint-released",
        payload={"instance_id": instance_id, **payload},
    )


def test_migration_adds_takeup_event_lookup_index(conn):
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'index' AND name = 'idx_event_sprint_type_ts'
        """
    ).fetchone()
    assert row is not None


def test_list_active_sprints_allows_multiple_active_sprints(conn, active_sprint):
    second_id = db.create_sprint(conn, "S2", status="active")

    active = db.list_active_sprints(conn)

    assert {sprint["id"] for sprint in active} == {active_sprint["id"], second_id}


def test_takeup_history_pairs_release_by_matched_event_id(conn, active_sprint):
    first = _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        hostname="host-a",
        pid=111,
        context="cockpit-realign",
    )
    _release(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        reason="done",
        matched_takeup_event_id=first,
    )

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert history["active_takeups"] == []
    assert history["unmatched_releases"] == []
    assert history["released_takeups"] == [
        {
            "sprint_id": active_sprint["id"],
            "actor": "agent-a",
            "actor_kind": "agent",
            "instance_id": "inst-a",
            "hostname": "host-a",
            "pid": 111,
            "taken_up_at": history["released_takeups"][0]["taken_up_at"],
            "taken_up_event_id": first,
            "context": "cockpit-realign",
            "forced": False,
            "released_at": history["released_takeups"][0]["released_at"],
            "released_event_id": history["released_takeups"][0]["released_event_id"],
            "reason": "done",
            "matched_takeup_event_id": first,
        }
    ]


def test_active_takeups_are_current_per_actor_instance(conn, active_sprint):
    old = _takeup(conn, active_sprint["id"], "agent-a", "inst-a", context="old")
    new = _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        context="new",
        forced=True,
    )
    other = _takeup(conn, active_sprint["id"], "agent-b", "inst-b")

    active = db.list_active_takeups(conn, active_sprint["id"])

    assert [row["taken_up_event_id"] for row in active] == [new, other]
    assert old not in [row["taken_up_event_id"] for row in active]
    assert active[0]["context"] == "new"
    assert active[0]["forced"] is True


def test_release_without_prior_takeup_is_reported(conn, active_sprint):
    release_id = _release(conn, active_sprint["id"], "agent-a", "inst-a", reason="cleanup")

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert history["active_takeups"] == []
    assert history["released_takeups"] == []
    assert history["unmatched_releases"] == [
        {
            "sprint_id": active_sprint["id"],
            "actor": "agent-a",
            "actor_kind": "agent",
            "instance_id": "inst-a",
            "hostname": None,
            "pid": None,
            "released_at": history["unmatched_releases"][0]["released_at"],
            "released_event_id": release_id,
            "reason": "cleanup",
            "matched_takeup_event_id": None,
        }
    ]


def test_release_without_instance_matches_latest_takeup_for_actor(conn, active_sprint):
    older = _takeup(conn, active_sprint["id"], "agent-a", "older")
    newer = _takeup(conn, active_sprint["id"], "agent-a", "newer")
    _release(conn, active_sprint["id"], "agent-a", None, reason="latest")

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert [row["taken_up_event_id"] for row in history["active_takeups"]] == [older]
    assert [row["taken_up_event_id"] for row in history["released_takeups"]] == [newer]
    assert history["released_takeups"][0]["reason"] == "latest"


def test_takeup_events_remain_plain_json_payloads(conn, active_sprint):
    event_id = _takeup(conn, active_sprint["id"], "agent-a", "inst-a")
    row = conn.execute("SELECT payload FROM event WHERE id = ?", (event_id,)).fetchone()

    payload = json.loads(row["payload"])

    assert payload["tags"] == ["takeup"]
