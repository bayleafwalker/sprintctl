from __future__ import annotations

import json
from pathlib import Path

import pytest

from sprintctl import pilot


def test_status_defaults_to_disabled_without_writing_files(tmp_path: Path) -> None:
    paths = pilot.shadow_pilot_paths(repo_root=tmp_path)

    status = pilot.shadow_pilot_status(repo_root=tmp_path)

    assert status.state is pilot.ShadowPilotState.DISABLED
    assert status.enabled is False
    assert status.configured is False
    assert status.paths == paths
    assert paths.config_path == tmp_path / ".sprintctl" / "shadow-pilot.json"
    assert paths.outbox_path == tmp_path / ".sprintctl" / "shadow-pilot-outbox.db"
    assert paths.projection_path == tmp_path / ".sprintctl" / "shadow-pilot-projection.db"
    assert not paths.state_dir.exists()
    assert status.to_dict()["state"] == "disabled"


def test_explicit_enable_and_disable_are_repo_local(tmp_path: Path) -> None:
    enabled = pilot.set_shadow_pilot_enabled(True, repo_root=tmp_path)

    assert enabled.state is pilot.ShadowPilotState.ENABLED
    assert enabled.configured is True
    assert json.loads(enabled.paths.config_path.read_text()) == {"enabled": True, "version": 1}
    assert enabled.paths.config_path.stat().st_mode & 0o777 == 0o600

    disabled = pilot.set_shadow_pilot_enabled(False, repo_root=tmp_path)

    assert disabled.state is pilot.ShadowPilotState.DISABLED
    assert disabled.configured is True
    assert json.loads(disabled.paths.config_path.read_text()) == {"enabled": False, "version": 1}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "expected an object"),
        ('{"version": 1}', "missing enabled"),
        ('{"version": 1, "enabled": true, "path": "/tmp/outbox"}', "unknown path"),
        ('{"version": 2, "enabled": true}', "unsupported shadow pilot config version"),
        ('{"version": 1, "enabled": "yes"}', "enabled must be a boolean"),
    ],
)
def test_rejects_malformed_or_path_overriding_config(
    tmp_path: Path, raw: str, message: str
) -> None:
    paths = pilot.shadow_pilot_paths(repo_root=tmp_path)
    paths.state_dir.mkdir()
    paths.config_path.write_text(raw)

    with pytest.raises(pilot.ShadowPilotConfigError, match=message):
        pilot.shadow_pilot_status(repo_root=tmp_path)


def test_rejects_state_directory_symlink_outside_repo(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-state"
    outside.mkdir()
    (tmp_path / ".sprintctl").symlink_to(outside, target_is_directory=True)

    with pytest.raises(pilot.ShadowPilotConfigError, match="must remain under"):
        pilot.shadow_pilot_paths(repo_root=tmp_path)


def test_load_rejects_caller_forged_paths(tmp_path: Path) -> None:
    expected = pilot.shadow_pilot_paths(repo_root=tmp_path)
    forged = pilot.ShadowPilotPaths(
        repo_root=expected.repo_root,
        state_dir=expected.state_dir,
        config_path=tmp_path / "config.json",
        outbox_path=expected.outbox_path,
        projection_path=expected.projection_path,
    )

    with pytest.raises(pilot.ShadowPilotConfigError, match="must be derived"):
        pilot.load_shadow_pilot_config(forged)
