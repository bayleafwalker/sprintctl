"""Read a bounded repository snapshot for customer work portability."""

from __future__ import annotations

import json

from . import pg

SCHEMA_VERSION = "vuoro-work-archive/v1"
MAX_RECORDS = 10_000
MAX_BYTES = 8 * 1024 * 1024

# Keep the file contract independent of table layouts. In particular, never
# export locks, actor bindings, opaque replay keys, remote hints or event blobs.
_REPOSITORY_QUERIES = {
    "sprints": (
        "SELECT id, name, goal, start_date, end_date, status, created_at, kind "
        "FROM sprint WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "tracks": (
        "SELECT id, sprint_id, name, description, created_at "
        "FROM track WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "items": (
        "SELECT id, sprint_id, track_id, title, description, status, resolution, "
        "priority, terminal_decision_id, aggregate_uuid::text, created_at, updated_at "
        "FROM work_item WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "dependencies": (
        "SELECT id, item_id, blocked_item_id, created_at "
        "FROM dep WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "references": (
        "SELECT id, work_item_id, ref_type, url, label, created_at "
        "FROM ref WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "releases": (
        "SELECT id, work_item_id, item_revision, revise_count, release_digest, "
        "acceptance_contract, context_refs, created_at, actor "
        "FROM work_release WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "release_commits": (
        "SELECT id, release_digest, commit_sha, observed_at "
        "FROM release_commit WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
    "decisions": (
        "SELECT id, kind, work_item_id, sprint_id, release_digest, evidence_digests, "
        "rationale, actor, superseded_by_item_id, legacy_source, created_at "
        "FROM work_decision WHERE repo_id=%s ORDER BY id LIMIT %s"
    ),
}


class ArchiveLimitExceeded(ValueError):
    """The complete repository archive exceeds the supported download budget."""


def export_repository(store: pg.PgStore, *, workspace_id: str) -> dict:
    """Export task history under one read-only snapshot of the work owner."""
    if not workspace_id:
        raise ValueError("archive export requires a workspace identity")
    archive = {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "repo_id": store.repo_id,
    }
    remaining = MAX_RECORDS
    with pg.repeatable_read_snapshot(store) as snapshot, snapshot.conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute("SELECT transaction_timestamp() AS at")
        archive["observed_at"] = cur.fetchone()["at"].isoformat()
        for section, query in _REPOSITORY_QUERIES.items():
            cur.execute(query, (snapshot.repo_id, remaining + 1))
            archive[section] = [pg._norm(row) for row in cur]
            remaining -= len(archive[section])
            if remaining < 0:
                raise ArchiveLimitExceeded("archive record limit exceeded")
        cur.execute(
            "SELECT report_id, work_item_id, principal_id, outcome, summary, payload, "
            "checks, payload_digest, disposition, reason_code, verification_profile, "
            "decision_id, created_at FROM work_outcome_report "
            "WHERE repo_id=%s AND workspace_id=%s "
            "ORDER BY work_item_id, created_at, report_id LIMIT %s",
            (snapshot.repo_id, workspace_id, remaining + 1),
        )
        reports = [pg._norm(row) for row in cur]
        remaining -= len(reports)
        if remaining < 0:
            raise ArchiveLimitExceeded("archive record limit exceeded")
        archive["reports"] = reports
        by_decision = {}
        for report in reports:
            if report["decision_id"] is not None:
                by_decision.setdefault(report["decision_id"], []).append(report)
        receipts = []
        for decision in archive["decisions"]:
            if decision["id"] in by_decision:
                receipt = pg._find_review_receipt(
                    cur, snapshot.repo_id, decision, by_decision[decision["id"]]
                )
                if receipt is not None:
                    receipts.append(receipt)
        if len(receipts) > remaining:
            raise ArchiveLimitExceeded("archive record limit exceeded")
        archive["acceptance_receipts"] = receipts
    encoded = json.dumps(archive, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BYTES:
        raise ArchiveLimitExceeded("archive byte limit exceeded")
    return archive
