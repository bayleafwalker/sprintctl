"""Validate portable work history before staging it for a restore."""

from __future__ import annotations

import json
import re
from datetime import datetime

from . import pg, releases, work_archive

_SECTIONS = (
    "sprints", "tracks", "items", "dependencies", "references", "releases",
    "release_commits", "decisions", "reports", "acceptance_receipts",
)
_METADATA = ("schema_version", "workspace_id", "repo_id", "observed_at")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TERMINAL = {"accept": "accepted", "reject": "rejected", "withdraw": "withdrawn", "supersede": "superseded"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _id(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _index(rows: list[dict], field: str, check) -> dict:
    result = {}
    for row in rows:
        value = row.get(field)
        _require(check(value) and value not in result, f"archive contains an invalid or duplicate {field}")
        result[value] = row
    return result


def _lookup(index: dict, value: object, label: str) -> dict:
    _require(_id(value) and value in index, f"archive contains a missing {label}")
    return index[value]


def _validate_size(archive: dict) -> None:
    records = 0
    for section in _SECTIONS:
        rows = archive[section]
        _require(isinstance(rows, list), f"archive {section} must be an array")
        records += len(rows)
        _require(records <= work_archive.MAX_RECORDS, "archive record limit exceeded")
        _require(all(isinstance(row, dict) for row in rows), f"archive {section} must contain objects")
    try:
        encoded = json.dumps(archive, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (UnicodeError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError("archive cannot be encoded as JSON") from exc
    _require(len(encoded) <= work_archive.MAX_BYTES, "archive byte limit exceeded")


def validate_archive(archive: dict, *, expected_repo_id: str) -> None:
    """Verify structure, relationships, and digests, without creating authority.

    The archive is caller-supplied history. These checks do not authenticate
    the source or establish a current authorization or a destination receipt.
    """
    _require(isinstance(archive, dict) and set(archive) == {*_METADATA, *_SECTIONS}, "archive fields are invalid")
    _require(archive["schema_version"] == work_archive.SCHEMA_VERSION, "unsupported archive version")
    _require(_text(expected_repo_id) and archive["repo_id"] == expected_repo_id, "archive is for a different repository")
    _require(_text(archive["workspace_id"]), "archive workspace identity is missing")
    try:
        timestamp = datetime.fromisoformat(archive["observed_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid archive observation time") from exc
    _require(timestamp.utcoffset() is not None, "archive observation time needs a time zone")
    _validate_size(archive)

    sprints = _index(archive["sprints"], "id", _id)
    tracks = _index(archive["tracks"], "id", _id)
    items = _index(archive["items"], "id", _id)
    decisions = _index(archive["decisions"], "id", _id)
    _index(archive["items"], "aggregate_uuid", _text)
    for track in tracks.values():
        _lookup(sprints, track.get("sprint_id"), "track sprint")
    for item in items.values():
        sprint_id = item.get("sprint_id")
        _lookup(sprints, sprint_id, "item sprint")
        track = _lookup(tracks, item.get("track_id"), "item track")
        _require(track.get("sprint_id") == sprint_id, "item track belongs to a different sprint")
        terminal_id = item.get("terminal_decision_id")
        if terminal_id is None:
            _require(item.get("resolution") is None, "item resolution requires a terminal decision")
        else:
            terminal = _lookup(decisions, terminal_id, "terminal decision")
            kind = terminal.get("kind")
            _require(
                _text(kind) and kind in _TERMINAL and terminal.get("work_item_id") == item["id"]
                and item.get("status") == "done" and item.get("resolution") == _TERMINAL[kind],
                "item terminal decision is inconsistent",
            )
    for edge in _index(archive["dependencies"], "id", _id).values():
        _lookup(items, edge.get("item_id"), "dependency item")
        _lookup(items, edge.get("blocked_item_id"), "blocking item")
        _require(edge["item_id"] != edge["blocked_item_id"], "an item cannot block itself")
    for ref in _index(archive["references"], "id", _id).values():
        _lookup(items, ref.get("work_item_id"), "reference item")

    release_records = _index(archive["releases"], "release_digest", _digest)
    _index(archive["releases"], "id", _id)
    for release in release_records.values():
        item = _lookup(items, release.get("work_item_id"), "release item")
        revision = release.get("item_revision")
        contract, refs = release.get("acceptance_contract"), release.get("context_refs")
        _require(
            _text(revision) and isinstance(contract, dict) and isinstance(refs, list)
            and all(isinstance(ref, dict) for ref in refs),
            "invalid release basis",
        )
        _require(
            releases.release_digest(item["aggregate_uuid"], revision, contract, refs) == release["release_digest"],
            "archive release digest does not match its basis",
        )
    for commit in _index(archive["release_commits"], "id", _id).values():
        digest = commit.get("release_digest")
        _require(_digest(digest) and digest in release_records, "commit release is missing")
        releases.validate_commit_sha(commit.get("commit_sha"))

    for decision in decisions.values():
        _validate_decision(decision, items, sprints, release_records)
    reports = _index(archive["reports"], "report_id", _text)
    for report in reports.values():
        _validate_report(report, items, decisions)
    receipts = _index(archive["acceptance_receipts"], "decision_id", _id)
    for receipt in receipts.values():
        _validate_receipt(receipt, reports, decisions)


def _validate_decision(decision, items, sprints, release_records):
    item_id, sprint_id = decision.get("work_item_id"), decision.get("sprint_id")
    _require((item_id is None) != (sprint_id is None), "decision needs exactly one subject")
    if item_id is not None:
        _lookup(items, item_id, "decision item")
    else:
        _lookup(sprints, sprint_id, "decision sprint")
    digest = decision.get("release_digest")
    if digest is not None:
        _require(
            _digest(digest) and digest in release_records
            and release_records[digest].get("work_item_id") == item_id,
            "decision release does not bind to its item",
        )
    if decision.get("superseded_by_item_id") is not None:
        _lookup(items, decision["superseded_by_item_id"], "superseding item")


def _validate_report(report, items, decisions):
    _lookup(items, report.get("work_item_id"), "report item")
    outcome, summary = report.get("outcome"), report.get("summary")
    payload, checks = report.get("payload"), report.get("checks")
    _require(
        _text(outcome) and isinstance(summary, str) and isinstance(payload, dict)
        and isinstance(checks, list) and _text(report.get("principal_id")),
        "report payload is invalid",
    )
    _require(
        pg.outcome_payload_digest(outcome, summary, payload, checks) == report.get("payload_digest"),
        "archive report digest does not match its payload",
    )
    if report.get("decision_id") is not None:
        decision = _lookup(decisions, report["decision_id"], "report decision")
        _require(
            decision.get("work_item_id") == report["work_item_id"] and report.get("disposition") == "settled",
            "report decision does not bind to its item",
        )
    else:
        _require(report.get("disposition") != "settled", "settled report decision is missing")


def _validate_receipt(receipt, reports, decisions):
    decision = decisions.get(receipt["decision_id"])
    report_id = receipt.get("report_id")
    report = reports.get(report_id) if _text(report_id) else None
    reviewer = receipt.get("reviewer_principal_id")
    _require(
        decision is not None and report is not None and _text(reviewer)
        and decision.get("kind") == "accept"
        and report.get("disposition") == "settled"
        and report.get("decision_id") == decision["id"]
        and _id(receipt.get("item_id")) and receipt["item_id"] == decision["work_item_id"]
        and receipt["item_id"] == report["work_item_id"]
        and reviewer.rsplit(":", 1)[0] != report["principal_id"].rsplit(":", 1)[0]
        and receipt.get("payload_digest") == report["payload_digest"]
        and decision.get("evidence_digests") == [report["payload_digest"]]
        and _digest(receipt.get("release_digest")) and receipt["release_digest"] == decision.get("release_digest"),
        "acceptance receipt does not bind to the exported decision and report",
    )


def load_archive(path, *, expected_repo_id: str) -> dict:
    """Load one bounded UTF-8 JSON file, rejecting ambiguous duplicate keys."""
    def unique_object(pairs):
        value = {}
        for key, entry in pairs:
            _require(key not in value, "archive contains a duplicate JSON key")
            value[key] = entry
        return value

    with path.open("rb") as stream:
        data = stream.read(work_archive.MAX_BYTES + 1)
    _require(len(data) <= work_archive.MAX_BYTES, "archive byte limit exceeded")
    try:
        archive = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("archive is not valid UTF-8 JSON") from exc
    validate_archive(archive, expected_repo_id=expected_repo_id)
    return archive
