from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from sprintctl import authority_config


def test_status_defaults_off_without_creating_state(tmp_path: Path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    status = authority_config.authority_command_status(repo_root=tmp_path)

    assert status.mode is authority_config.AuthorityCommandMode.OFF
    assert status.configured is False
    assert status.paths == paths
    assert paths.config_path == tmp_path / ".sprintctl" / "authority-command.json"
    assert paths.outbox_path == tmp_path / ".sprintctl" / "authority-command-outbox.db"
    assert not paths.state_dir.exists()


def test_shadow_enforce_and_off_rollback_are_atomic_private_writes(tmp_path: Path):
    shadow = authority_config.set_authority_command_mode("shadow", repo_root=tmp_path)
    assert shadow.mode is authority_config.AuthorityCommandMode.SHADOW
    assert shadow.paths.config_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(shadow.paths.config_path.read_text()) == {"mode": "shadow", "version": 1}

    enforced = authority_config.set_authority_command_mode("enforce", repo_root=tmp_path)
    assert enforced.mode is authority_config.AuthorityCommandMode.ENFORCE

    rolled_back = authority_config.set_authority_command_mode("off", repo_root=tmp_path)
    assert rolled_back.mode is authority_config.AuthorityCommandMode.OFF
    assert rolled_back.configured is True
    assert json.loads(rolled_back.paths.config_path.read_text()) == {"mode": "off", "version": 1}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "expected an object"),
        ('{"version": 1}', "missing mode"),
        ('{"version": 1, "mode": "shadow", "path": "/tmp/outbox"}', "unknown path"),
        ('{"version": 2, "mode": "off"}', "unsupported authority command config version"),
        ('{"version": 1, "mode": "unsafe"}', "mode must be off, shadow, or enforce"),
    ],
)
def test_rejects_invalid_mode_shape_and_path_override(tmp_path, raw, message):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    paths.state_dir.mkdir()
    paths.config_path.write_text(raw)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match=message):
        authority_config.authority_command_status(repo_root=tmp_path)


def test_rejects_state_directory_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-authority-state"
    outside.mkdir(exist_ok=True)
    (tmp_path / ".sprintctl").symlink_to(outside, target_is_directory=True)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="must remain under"):
        authority_config.authority_command_paths(repo_root=tmp_path)


def test_load_rejects_forged_paths(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    forged = authority_config.AuthorityCommandPaths(
        repo_root=paths.repo_root,
        state_dir=paths.state_dir,
        config_path=tmp_path / "escaped.json",
        outbox_path=paths.outbox_path,
        terminal_dir=paths.terminal_dir,
    )
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="must be derived"):
        authority_config.load_authority_command_config(forged)


def test_terminal_receipt_is_private_and_rejects_unsafe_event_ids(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    authority_config.mark_terminal_authority_decision(
        paths, event_id=event_id, outcome="accepted"
    )
    receipt = paths.terminal_dir / f"{event_id}.json"
    assert stat_mode(paths.terminal_dir) == 0o700
    assert stat_mode(receipt) == 0o600
    assert authority_config.is_terminal_authority_decision(paths, event_id=event_id) is True

    receipt.chmod(0o644)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="unsafe permissions"):
        authority_config.is_terminal_authority_decision(paths, event_id=event_id)

    receipt.chmod(0o600)
    paths.terminal_dir.chmod(0o755)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="unsafe permissions"):
        authority_config.is_terminal_authority_decision(paths, event_id=event_id)
    paths.terminal_dir.chmod(0o700)

    receipt.write_text(json.dumps({"event_id": event_id, "outcome": "tampered"}))
    receipt.chmod(0o600)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="invalid authority terminal receipt"):
        authority_config.is_terminal_authority_decision(paths, event_id=event_id)

    for unsafe_event_id in ("../escape", f"{event_id}/../../escape", "not-a-uuid"):
        with pytest.raises(authority_config.AuthorityCommandConfigError, match="event_id"):
            authority_config.is_terminal_authority_decision(paths, event_id=unsafe_event_id)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
