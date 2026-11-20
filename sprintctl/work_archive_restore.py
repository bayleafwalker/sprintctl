"""Restore the task graph while retaining historical authority as source data."""

from __future__ import annotations

import hashlib
from collections import Counter

from . import pg, releases
from .work_archive_validation import validate_archive

# Fixed destination fields. Unknown source columns never become SQL identifiers.
_ROW_FIELDS = (
    ("sprints", "sprint", "name goal start_date end_date status created_at kind"),
    ("tracks", "track", "sprint_id name description created_at"),
    ("items", "work_item", "sprint_id track_id title description status priority created_at updated_at"),
    ("references", "ref", "work_item_id ref_type url label created_at"),
    ("dependencies", "dep", "item_id blocked_item_id created_at"),
)
_SOURCE_KIND = "customer-work-archive/v1"


class RestoreConflict(ValueError):
    """Destination already has work or a different archived restore."""


def restore_repository(store: pg.PgStore, archive: dict, *, workspace_id: str, actor: str) -> dict:
    """Apply validated data in the caller's transaction and return its identity map."""
    validate_archive(archive, expected_repo_id=store.repo_id)
    if not workspace_id or not actor.strip():
        raise ValueError("restore requires a destination workspace and administrator")
    canonical = releases.canonical_json(archive)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '3s'")
        # Creating any task requires a sprint. Block concurrent sprint inserts
        # while establishing that this repository is empty.
        cur.execute("LOCK TABLE sprint IN SHARE ROW EXCLUSIVE MODE")
        cur.execute(
            "SELECT workspace_id, archive_digest, item_ids FROM work_archive_restore WHERE repo_id=%s",
            (store.repo_id,),
        )
        prior = cur.fetchone()
        if prior is not None:
            if prior["workspace_id"] == workspace_id and prior["archive_digest"] == digest:
                return {"archive_digest": digest, "item_ids": prior["item_ids"]}
            raise RestoreConflict("repository already has a different work archive")
        cur.execute("SELECT 1 FROM sprint WHERE repo_id=%s LIMIT 1", (store.repo_id,))
        if cur.fetchone():
            raise RestoreConflict("restore requires a repository with no sprints or tasks")

        cur.execute(f"SET LOCAL {pg.LEGACY_IMPORT_SETTING} = 'on'")
        maps = _restore_rows(cur, store.repo_id, archive)
        item_ids = {str(source): target for source, target in maps["work_item"].items()}
        _restore_status_and_policy(cur, store.repo_id, archive, maps["work_item"], actor)
        cur.execute(
            "INSERT INTO work_archive_restore(repo_id,workspace_id,archive_digest,restored_by,source_archive,item_ids) "
            "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (store.repo_id, workspace_id, digest, actor, canonical, releases.canonical_json(item_ids)),
        )
    return {"archive_digest": digest, "item_ids": item_ids}


def _restore_rows(cur, repo_id, archive):
    maps = {table: {} for _, table, _ in _ROW_FIELDS}
    fks = {
        "sprint_id": "sprint",
        "track_id": "track",
        "work_item_id": "work_item",
        "item_id": "work_item",
        "blocked_item_id": "work_item",
    }
    for section, table, fields in _ROW_FIELDS:
        for row in archive[section]:
            data = {name: row[name] for name in fields.split() if name in row}
            for column, parent in fks.items():
                if column in data:
                    data[column] = maps[parent][data[column]]
            if table == "work_item":
                legacy_closed = row["status"] == "done" and row.get("terminal_decision_id") is None
                if legacy_closed:
                    data["legacy"] = True
                elif data["status"] in {"active", "done"}:
                    data["status"] = "pending"
            maps[table][row["id"]] = pg._import_row(cur, table, repo_id, data, returning_id=True)
    return maps


def _restore_status_and_policy(cur, repo_id, archive, item_map, actor):
    by_id = {row["id"]: row for row in archive["decisions"]}
    revisions = Counter(
        row["work_item_id"] for row in archive["decisions"] if row["kind"] == "revise"
    )
    review_items = {
        row["work_item_id"] for row in archive["releases"]
        if row["revise_count"] == revisions[row["work_item_id"]]
        and row["acceptance_contract"].get("review_protocol") == releases.BROWSER_REVIEW_PROTOCOL
    }
    for source in archive["items"]:
        item_id = item_map[source["id"]]
        decision_id = source.get("terminal_decision_id")
        if decision_id is not None:
            old = by_id[decision_id]
            decision = pg._import_row(
                cur, "work_decision", repo_id, {
                    "kind": old["kind"], "work_item_id": item_id, "actor": actor,
                    "legacy_source": _SOURCE_KIND,
                    "rationale": f"Restored source decision {decision_id} as imported history.",
                    "superseded_by_item_id": item_map.get(old.get("superseded_by_item_id")),
                }, returning_id=True,
            )
            cur.execute(
                "UPDATE work_item SET status='done',resolution=%s,terminal_decision_id=%s "
                "WHERE repo_id=%s AND id=%s",
                (source["resolution"], decision, repo_id, item_id),
            )
        elif source["status"] != "done" and source["id"] in review_items:
            item, revision, revise_count = pg._release_basis_locked(cur, repo_id, item_id)
            pg._freeze_release_locked(
                cur, repo_id, item, revision, revise_count, actor=actor,
                acceptance_contract={
                    "verification_profile": "human-authorized",
                    "review_protocol": releases.BROWSER_REVIEW_PROTOCOL,
                },
            )


def read_restored_source(store: pg.PgStore, *, workspace_id: str) -> dict | None:
    """Return the retained source and mapping for this destination workspace."""
    with pg.repeatable_read_snapshot(store) as snapshot, snapshot.conn.cursor() as cur:
        cur.execute(
            "SELECT archive_digest, restored_by, restored_at, source_archive, item_ids "
            "FROM work_archive_restore WHERE repo_id=%s AND workspace_id=%s",
            (store.repo_id, workspace_id),
        )
        row = cur.fetchone()
        return pg._norm(row) if row is not None else None
