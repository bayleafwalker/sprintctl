"""Bounded rollback of supported local cache read selection, not authority."""
from datetime import datetime, timezone
import json

import pytest

from sprintctl import db, outbox, projection, projection_reads, sync
from sprintctl.cli import cli
from tests.test_projection_reads import _cache_outbox_records, _configure_repo_marker


@pytest.mark.parametrize("enabling_override", [False, True])
def test_disable_restores_effective_backend_reads_without_changing_data(
    runner, conn, active_sprint, tmp_path, monkeypatch, enabling_override
):
    monkeypatch.delenv("SPRINTCTL_PROJECTION_READS", raising=False)
    _configure_repo_marker(tmp_path)
    track = db.get_or_create_track(conn, active_sprint["id"], "rollout")
    item = db.create_work_item(conn, active_sprint["id"], track, "retained authority")
    added = runner.invoke(cli, ["event", "add", "--sprint-id", str(active_sprint["id"]),
        "--item-id", str(item), "--type", "work.completed", "--actor", "example",
        "--payload", '{"summary":"reported example, not acceptance"}', "--json"])
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["synchronization"]["status"] == "mirrored"
    paths = sync.repository_sync_paths(cwd=tmp_path)
    producer = outbox.open_outbox(paths.outbox_path)
    cache = projection.open_cached_projection(paths.projection_path)
    try:
        _cache_outbox_records(producer, cache,
            advanced_at=datetime.now(timezone.utc).isoformat())
    finally:
        producer.close()
        cache.close()
    other = tmp_path / "unrelated-repository"
    other.mkdir()
    _configure_repo_marker(other)
    other_status = projection_reads.set_projection_reads_enabled(True, repo_root=other)
    other_config = other_status.paths.config_path.read_bytes()
    authoritative_records = list(conn.iterdump())
    cache_bytes = paths.projection_path.read_bytes()

    def read():
        result = runner.invoke(cli, ["item", "show", "--id", str(item), "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    assert read()["projection"]["source"] == "backend"  # default off
    assert runner.invoke(cli, ["projection-reads", "enable"]).exit_code == 0
    projected = read()
    assert projected["projection"]["source"] == "projection"
    if enabling_override:
        monkeypatch.setenv("SPRINTCTL_PROJECTION_READS", "1")
    assert runner.invoke(cli, ["projection-reads", "disable"]).exit_code == 0
    assert json.loads((tmp_path / ".sprintctl/projection-reads.json").read_text())["enabled"] is False
    effective = read()
    assert effective["projection"]["source"] == ("projection" if enabling_override else "backend")
    if enabling_override:
        status = projection_reads.projection_reads_status(repo_root=tmp_path)
        assert status.enabled is True and status.source == "env"
        monkeypatch.delenv("SPRINTCTL_PROJECTION_READS")
        effective = read()
    assert effective["projection"]["source"] == "backend"
    assert effective["projection"]["enabled"] is False
    assert len(effective["events"]) == len(projected["events"]) == 1
    assert paths.projection_path.read_bytes() == cache_bytes
    assert list(conn.iterdump()) == authoritative_records
    assert other_status.paths.config_path.read_bytes() == other_config
    unrelated = projection_reads.projection_reads_status(repo_root=other, environ={})
    assert unrelated.enabled is True and unrelated.source == "config"
