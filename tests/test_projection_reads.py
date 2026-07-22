"""Tests for guarded projection-backed reads (work item #1162).

Covers three layers:

1. ``sprintctl/projection_reads.py`` -- the per-repository opt-in config,
   mirroring the conventions already exercised in ``test_authority_config.py``
   and ``test_pilot_cli.py``.
2. ``sprintctl/projection.py:assess_freshness`` -- schema-version, staleness,
   and never-synchronized detection.
3. The CLI wiring in ``sprintctl/cli.py`` -- ``item show`` actually swaps its
   event-history source when the cached projection is healthy, and ``item
   list`` / ``next-work`` / ``usage --context`` disclose freshness without
   changing their content or (for the bare-array endpoints) their JSON shape.

Since ``pilot sync`` refuses to run against a local SQLite backend (see
``test_pilot_cli.py::test_pilot_sync_is_guarded_in_local_backend``), the
"healthy projection" scenarios below populate the cache directly with
``projection.apply_ingested_records``, using the exact record shape
``sprintctl/sync.py:_cached_record`` produces from a real mirrored outbox
record. This reproduces what a real `pilot sync` against a remote backend
would have written, without requiring PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from sprintctl import db, outbox, pilot, projection, projection_reads
from sprintctl.cli import cli


def _configure_repo_marker(tmp_path) -> None:
    state = tmp_path / ".sprintctl"
    state.mkdir(exist_ok=True)
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": tmp_path.name}), encoding="utf-8"
    )


def _cache_outbox_records(outbox_conn, projection_conn, *, advanced_at: str | None = None):
    """Apply every outbox record into the projection cache at offsets 1..N.

    Mirrors the record shape ``sync.py:_cached_record`` builds from a
    ``pg.IngestedRecord`` -- here built directly from the local
    ``outbox.OutboxRecord`` a real sync pass would have uploaded and
    round-tripped unchanged.
    """
    records = outbox.list_records(outbox_conn)
    cached = []
    for offset, record in enumerate(records, start=1):
        cached.append(
            projection.CachedIngestRecord(
                ingest_offset=offset,
                record={
                    "origin_stream_id": record.origin_stream_id,
                    "origin_seq": record.origin_seq,
                    "event_id": record.event_id,
                    "schema_version": record.schema_version,
                    "record_class": record.record_class,
                    "event_type": record.event_type,
                    "actor": record.actor,
                    "runtime_session_id": record.runtime_session_id,
                    "occurred_at": record.occurred_at,
                    "basis_revision": record.basis_revision,
                    "correlation_id": record.correlation_id,
                    "causation_id": record.causation_id,
                    "payload": record.payload,
                    "payload_sha256": record.payload_sha256,
                    "created_at": record.created_at,
                },
            )
        )
    return projection.apply_ingested_records(projection_conn, cached, advanced_at=advanced_at)


# ---------------------------------------------------------------------------
# projection_reads.py -- per-repository config
# ---------------------------------------------------------------------------


def test_status_defaults_disabled_without_creating_state(tmp_path):
    paths = projection_reads.projection_reads_paths(repo_root=tmp_path)
    status = projection_reads.projection_reads_status(repo_root=tmp_path, environ={})

    assert status.enabled is False
    assert status.source == "default"
    assert status.configured is False
    assert paths.config_path == tmp_path / ".sprintctl" / "projection-reads.json"
    assert not paths.state_dir.exists()


def test_enable_disable_round_trip_is_atomic_private_write(tmp_path):
    enabled = projection_reads.set_projection_reads_enabled(True, repo_root=tmp_path)
    assert enabled.enabled is True
    assert enabled.paths.config_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(enabled.paths.config_path.read_text()) == {"version": 1, "enabled": True}

    disabled = projection_reads.set_projection_reads_enabled(False, repo_root=tmp_path)
    assert disabled.enabled is False
    assert disabled.configured is True
    status = projection_reads.projection_reads_status(repo_root=tmp_path, environ={})
    assert status.source == "config"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "expected an object"),
        ('{"version": 1}', "missing enabled"),
        ('{"version": 1, "enabled": true, "extra": 1}', "unknown extra"),
        ('{"version": 2, "enabled": true}', "unsupported projection reads config version"),
    ],
)
def test_rejects_invalid_config_shape(tmp_path, raw, message):
    paths = projection_reads.projection_reads_paths(repo_root=tmp_path)
    paths.state_dir.mkdir()
    paths.config_path.write_text(raw)
    with pytest.raises(projection_reads.ProjectionReadsConfigError, match=message):
        projection_reads.projection_reads_status(repo_root=tmp_path, environ={})


def test_env_var_overrides_persisted_config_in_both_directions(tmp_path):
    projection_reads.set_projection_reads_enabled(True, repo_root=tmp_path)

    off = projection_reads.projection_reads_status(
        repo_root=tmp_path, environ={"SPRINTCTL_PROJECTION_READS": "0"}
    )
    assert off.enabled is False
    assert off.source == "env"

    on = projection_reads.projection_reads_status(
        repo_root=tmp_path, environ={"SPRINTCTL_PROJECTION_READS": "yes"}
    )
    assert on.enabled is True
    assert on.source == "env"

    with pytest.raises(projection_reads.ProjectionReadsConfigError, match="invalid"):
        projection_reads.projection_reads_status(
            repo_root=tmp_path, environ={"SPRINTCTL_PROJECTION_READS": "maybe"}
        )


# ---------------------------------------------------------------------------
# projection.py -- assess_freshness
# ---------------------------------------------------------------------------


def test_assess_freshness_reports_never_synchronized_for_empty_cache(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    freshness = projection.assess_freshness(conn)
    assert freshness.healthy is False
    assert freshness.fallback_reason == "never-synchronized"
    assert freshness.schema_version == projection.PROJECTION_SCHEMA_VERSION
    conn.close()


def test_assess_freshness_healthy_when_recently_advanced(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    projection.apply_ingested_records(
        conn,
        [projection.CachedIngestRecord(1, {"kind": "x"})],
        advanced_at="2026-07-20T11:59:50Z",
    )
    freshness = projection.assess_freshness(conn, now=now, stale_after_seconds=300)
    assert freshness.healthy is True
    assert freshness.fallback_reason is None
    assert freshness.age_seconds == pytest.approx(10, abs=1)
    conn.close()


def test_assess_freshness_reports_stale_past_threshold(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    projection.apply_ingested_records(
        conn,
        [projection.CachedIngestRecord(1, {"kind": "x"})],
        advanced_at="2026-07-20T11:00:00Z",
    )
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    freshness = projection.assess_freshness(conn, now=now, stale_after_seconds=300)
    assert freshness.healthy is False
    assert freshness.fallback_reason == "stale"
    conn.close()


def test_assess_freshness_reports_schema_upgrade_required(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    projection.apply_ingested_records(
        conn,
        [projection.CachedIngestRecord(1, {"kind": "x"})],
        advanced_at="2026-07-20T11:59:50Z",
    )
    conn.execute("UPDATE cached_projection_meta SET schema_version = 999 WHERE singleton = 1")
    conn.commit()
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    freshness = projection.assess_freshness(conn, now=now)
    assert freshness.healthy is False
    assert freshness.fallback_reason == "schema-upgrade-required"
    conn.close()


# ---------------------------------------------------------------------------
# CLI wiring: item show
# ---------------------------------------------------------------------------


def test_item_show_default_backend_only_and_projection_key_reports_disabled(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    db.create_event(
        conn, active_sprint["id"], actor="dev", event_type="decision",
        work_item_id=iid, payload={"summary": "Use RS256"},
    )

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["projection"]["enabled"] is False
    assert data["projection"]["source"] == "backend"
    assert len(data["events"]) == 1

    text_result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
    assert "Projection:" not in text_result.output


def test_item_show_falls_back_explicitly_when_projection_missing(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    db.create_event(
        conn, active_sprint["id"], actor="dev", event_type="decision",
        work_item_id=iid, payload={"summary": "Use RS256"},
    )
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["projection"]["enabled"] is True
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "missing"
    # Fallback still serves correct content from backend.
    assert len(data["events"]) == 1

    text_result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
    assert "Projection: source=backend fallback=missing" in text_result.output


def test_item_show_serves_events_from_healthy_projection_with_backend_parity(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")

    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
    added = runner.invoke(
        cli,
        [
            "event", "add", "--sprint-id", str(active_sprint["id"]),
            "--item-id", str(iid), "--type", "work.completed", "--actor", "agent-a",
            "--payload", '{"summary":"shipped the endpoint"}', "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["shadow_pilot"]["status"] == "mirrored"

    # Capture the ground-truth backend view before enabling projection reads.
    baseline = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    assert baseline.exit_code == 0
    backend_events = json.loads(baseline.output)["events"]
    assert len(backend_events) == 1

    pilot_paths = pilot.shadow_pilot_paths(repo_root=tmp_path)
    producer = outbox.open_outbox(pilot_paths.outbox_path)
    cache = projection.open_cached_projection(pilot_paths.projection_path)
    try:
        current = datetime.now(timezone.utc).replace(microsecond=0)
        _cache_outbox_records(
            producer,
            cache,
            advanced_at=current.isoformat().replace("+00:00", "Z"),
        )
    finally:
        producer.close()
        cache.close()

    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["projection"]["source"] == "projection"
    assert data["projection"]["fallback_reason"] is None
    assert data["projection"]["watermark_offset"] == 1

    # Parity: the projection-backed event list matches the backend's, field
    # for field, because the mirrored envelope carries the original event
    # id, sprint id, actor, type, created_at, and payload. The mirrored
    # envelope's payload has been through the observation contract's
    # canonicalization, so it is compared semantically (parsed) rather than
    # as a byte-identical JSON string -- the backend's raw column value is a
    # JSON-encoded string while the reconstructed event exposes the already
    # -parsed object.
    assert len(data["events"]) == len(backend_events) == 1
    projected_event, backend_event = data["events"][0], backend_events[0]
    for field in ("id", "sprint_id", "work_item_id", "source_type", "actor", "event_type", "created_at"):
        assert projected_event[field] == backend_event[field], field
    assert projected_event["payload"] == json.loads(backend_event["payload"])

    text_result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
    assert "Projection: source=projection watermark_offset=1" in text_result.output


def test_item_show_falls_back_when_never_synchronized(runner, conn, active_sprint, tmp_path):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
    # Touch the projection file into existence without ever applying records.
    pilot_paths = pilot.shadow_pilot_paths(repo_root=tmp_path)
    projection.open_cached_projection(pilot_paths.projection_path).close()
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    data = json.loads(result.output)
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "never-synchronized"


def test_item_show_falls_back_when_stale(runner, conn, active_sprint, tmp_path, monkeypatch):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
    pilot_paths = pilot.shadow_pilot_paths(repo_root=tmp_path)
    cache = projection.open_cached_projection(pilot_paths.projection_path)
    try:
        projection.apply_ingested_records(
            cache,
            [projection.CachedIngestRecord(1, {"kind": "x"})],
            advanced_at="2020-01-01T00:00:00Z",
        )
    finally:
        cache.close()
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    data = json.loads(result.output)
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "stale"


def test_item_show_falls_back_when_schema_upgrade_required(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
    pilot_paths = pilot.shadow_pilot_paths(repo_root=tmp_path)
    cache = projection.open_cached_projection(pilot_paths.projection_path)
    try:
        projection.apply_ingested_records(
            cache,
            [projection.CachedIngestRecord(1, {"kind": "x"})],
        )
        cache.execute("UPDATE cached_projection_meta SET schema_version = 999 WHERE singleton = 1")
        cache.commit()
    finally:
        cache.close()
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    data = json.loads(result.output)
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "schema-upgrade-required"


def test_env_var_enables_projection_reads_without_persisted_config(
    runner, conn, active_sprint, tmp_path, monkeypatch
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
    monkeypatch.setenv("SPRINTCTL_PROJECTION_READS", "1")

    result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
    data = json.loads(result.output)
    assert data["projection"]["enabled"] is True
    assert data["projection"]["fallback_reason"] == "missing"


# ---------------------------------------------------------------------------
# CLI wiring: item list / next-work / usage --context -- disclosure only
# ---------------------------------------------------------------------------


def test_item_list_json_shape_never_gains_a_projection_key(runner, conn, active_sprint, tmp_path):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    db.create_work_item(conn, active_sprint["id"], tid, "Item A")
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1


def test_item_list_text_discloses_unsupported_read_surface_when_enabled(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    db.create_work_item(conn, active_sprint["id"], tid, "Item A")
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["item", "list"])
    assert result.exit_code == 0, result.output
    assert "Projection: source=backend fallback=missing" in result.output


def test_next_work_explain_json_gains_projection_key_when_enabled(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
    db.create_work_item(conn, active_sprint["id"], tid, "Item A")
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(
        cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--json", "--explain"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["projection"]["enabled"] is True
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "missing"


def test_usage_context_default_json_shape_is_unchanged(runner, active_sprint):
    result = runner.invoke(cli, ["usage", "--context", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "projection" not in data


def test_usage_context_gains_projection_key_when_enabled(runner, active_sprint, tmp_path):
    _configure_repo_marker(tmp_path)
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["usage", "--context", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["projection"]["enabled"] is True
    assert data["projection"]["source"] == "backend"
    assert data["projection"]["fallback_reason"] == "missing"


# ---------------------------------------------------------------------------
# CLI: projection-reads status/enable/disable
# ---------------------------------------------------------------------------


def test_projection_reads_cli_round_trip(runner, tmp_path):
    _configure_repo_marker(tmp_path)

    initial = runner.invoke(cli, ["projection-reads", "status", "--json"])
    assert initial.exit_code == 0, initial.output
    assert json.loads(initial.output)["enabled"] is False

    enabled = runner.invoke(cli, ["projection-reads", "enable", "--json"])
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["enabled"] is True

    status = runner.invoke(cli, ["projection-reads", "status", "--json"])
    assert json.loads(status.output)["enabled"] is True
    assert json.loads(status.output)["health"] == "missing"

    disabled = runner.invoke(cli, ["projection-reads", "disable", "--json"])
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["enabled"] is False
