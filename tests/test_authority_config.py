from __future__ import annotations

import hashlib
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
        credential_dir=paths.credential_dir,
    )
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="must be derived"):
        authority_config.load_authority_command_config(forged)


def test_pending_credential_survives_restart_and_is_removed_after_promotion(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    secret = "claim-proof-only-known-locally"
    credential_ref = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()

    stored = authority_config.store_pending_authority_credential(
        paths,
        event_id=event_id,
        credential_ref=credential_ref,
        secret=secret,
    )
    restarted_paths = authority_config.authority_command_paths(repo_root=tmp_path)
    recovered = authority_config.load_pending_authority_credential(
        restarted_paths,
        event_id=event_id,
    )

    assert recovered == stored
    assert stat_mode(paths.credential_dir) == 0o700
    sidecar = paths.credential_dir / f"{event_id}.json"
    assert stat_mode(sidecar) == 0o600
    assert set(json.loads(sidecar.read_text())) == {
        "event_id",
        "credentials",
        "recovery_credential_ref",
    }
    assert "secret" not in authority_config.authority_command_status(
        repo_root=tmp_path
    ).to_dict()
    assert authority_config.remove_pending_authority_credential(
        restarted_paths,
        event_id=event_id,
    ) is True
    assert not sidecar.exists()
    assert authority_config.load_pending_authority_credential(
        restarted_paths,
        event_id=event_id,
    ) is None


def test_pending_credential_event_id_is_idempotent_but_cannot_be_rebound(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    first_secret = "first-proof"
    first_ref = "sha256:" + hashlib.sha256(first_secret.encode()).hexdigest()
    first = authority_config.store_pending_authority_credential(
        paths,
        event_id=event_id,
        credential_ref=first_ref,
        secret=first_secret,
    )
    assert authority_config.store_pending_authority_credential(
        paths,
        event_id=event_id,
        credential_ref=first_ref,
        secret=first_secret,
    ) == first

    second_secret = "second-proof"
    second_ref = "sha256:" + hashlib.sha256(second_secret.encode()).hexdigest()
    with pytest.raises(
        authority_config.AuthorityCommandConfigError,
        match="already has different proof material",
    ):
        authority_config.store_pending_authority_credential(
            paths,
            event_id=event_id,
            credential_ref=second_ref,
            secret=second_secret,
        )


def test_pending_command_can_retain_old_and_rotated_proofs_for_retry(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    old_secret = "old-proof"
    new_secret = "new-proof"
    old_ref = "sha256:" + hashlib.sha256(old_secret.encode()).hexdigest()
    new_ref = "sha256:" + hashlib.sha256(new_secret.encode()).hexdigest()

    stored = authority_config.store_pending_authority_credentials(
        paths,
        event_id=event_id,
        credentials={old_ref: old_secret, new_ref: new_secret},
        recovery_credential_ref=new_ref,
    )
    recovered = authority_config.load_pending_authority_credential(
        authority_config.authority_command_paths(repo_root=tmp_path),
        event_id=event_id,
    )

    assert recovered == stored
    assert recovered.credentials == {old_ref: old_secret, new_ref: new_secret}
    assert recovered.credential_ref == new_ref
    assert recovered.secret == new_secret


def test_pending_credential_rejects_digest_mismatch_and_unsafe_permissions(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    with pytest.raises(
        authority_config.AuthorityCommandConfigError,
        match="does not match the local secret digest",
    ):
        authority_config.store_pending_authority_credential(
            paths,
            event_id=event_id,
            credential_ref="sha256:" + "0" * 64,
            secret="actual-proof",
        )

    secret = "actual-proof"
    credential_ref = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()
    authority_config.store_pending_authority_credential(
        paths,
        event_id=event_id,
        credential_ref=credential_ref,
        secret=secret,
    )
    sidecar = paths.credential_dir / f"{event_id}.json"
    sidecar.chmod(0o644)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="unsafe permissions"):
        authority_config.load_pending_authority_credential(paths, event_id=event_id)

    sidecar.chmod(0o600)
    paths.credential_dir.chmod(0o755)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="unsafe permissions"):
        authority_config.load_pending_authority_credential(paths, event_id=event_id)


def test_pending_credential_rejects_tampering_and_path_traversal(tmp_path):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    event_id = str(uuid4())
    secret = "actual-proof"
    credential_ref = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()
    authority_config.store_pending_authority_credential(
        paths,
        event_id=event_id,
        credential_ref=credential_ref,
        secret=secret,
    )
    sidecar = paths.credential_dir / f"{event_id}.json"
    raw = json.loads(sidecar.read_text())
    raw["credentials"][credential_ref] = "tampered-proof"
    sidecar.write_text(json.dumps(raw))
    sidecar.chmod(0o600)
    with pytest.raises(authority_config.AuthorityCommandConfigError, match="digest mismatch"):
        authority_config.load_pending_authority_credential(paths, event_id=event_id)

    for unsafe_event_id in ("../escape", f"{event_id}/../../escape", "not-a-uuid"):
        with pytest.raises(authority_config.AuthorityCommandConfigError, match="event_id"):
            authority_config.load_pending_authority_credential(
                paths,
                event_id=unsafe_event_id,
            )


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
