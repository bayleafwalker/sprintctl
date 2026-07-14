import json
from uuid import UUID, uuid4

from sprintctl import db, pg


def test_new_sprints_and_work_items_have_stable_portable_uuids(conn):
    sprint_id = db.create_sprint(conn, "UUID sprint", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "delivery")
    item_id = db.create_work_item(conn, sprint_id, track_id, "UUID item")

    sprint = db.get_sprint(conn, sprint_id)
    item = db.get_work_item(conn, item_id)

    assert sprint["aggregate_uuid"] == str(UUID(sprint["aggregate_uuid"]))
    assert item["aggregate_uuid"] == str(UUID(item["aggregate_uuid"]))
    assert sprint["aggregate_uuid"] != item["aggregate_uuid"]


def test_aggregate_uuid_migration_backfills_existing_rows(tmp_path):
    path = tmp_path / "pre-uuid.db"
    conn = db.get_connection(path)
    for version, migrate in (
        (1, db._migration_1),
        (2, db._migration_2),
        (3, db._migration_3),
        (4, db._migration_4),
        (5, db._migration_5),
        (6, db._migration_6),
        (7, db._migration_7),
        (8, db._migration_8),
        (9, db._migration_9),
    ):
        db._run_migration(conn, version, migrate, foreign_keys_off=version == 5)
    sprint_id = conn.execute(
        "INSERT INTO sprint (name, status) VALUES (?, ?)", ("Existing", "active")
    ).lastrowid
    track_id = conn.execute(
        "INSERT INTO track (sprint_id, name) VALUES (?, ?)", (sprint_id, "delivery")
    ).lastrowid
    item_id = conn.execute(
        "INSERT INTO work_item (sprint_id, track_id, title) VALUES (?, ?, ?)",
        (sprint_id, track_id, "Existing item"),
    ).lastrowid
    conn.commit()

    db.init_db(conn)

    assert UUID(db.get_sprint(conn, sprint_id)["aggregate_uuid"])
    assert UUID(db.get_work_item(conn, item_id)["aggregate_uuid"])
    conn.close()


def test_postgres_row_normalization_keeps_aggregate_uuid_json_serializable():
    aggregate_uuid = uuid4()

    normalized = pg._norm({"aggregate_uuid": aggregate_uuid})

    assert normalized["aggregate_uuid"] == str(aggregate_uuid)
    assert json.loads(json.dumps(normalized))["aggregate_uuid"] == str(aggregate_uuid)
