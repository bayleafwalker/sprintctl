"""Protected exact-CWD service handoff fixtures (no live credentials)."""
import json
import os
import stat
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from sprintctl import backend, served
from sprintctl.vuoro_credentials import (
    CredentialResolutionError,
    resolve_delegated_profile_credential,
)

TOKEN = "vrd_" + "A" * 52
_VersionInfo = namedtuple("_VersionInfo", ["major", "minor", "micro"])


def _simulate_adapter_caller(monkeypatch, *, uid=None, groups=()):
    """Model an adapter UID handoff to another UID in an isolated temp tree.

    Chown is unavailable to ordinary CI users; actual mode/ACL decisions are
    replaced with the equivalent effective-caller permission calculation.
    This qualifies reader logic only, not production ownership deployment.
    """
    caller_uid = uid if uid is not None else os.geteuid() + 1
    group_ids = frozenset(groups)
    monkeypatch.setattr("sprintctl.vuoro_credentials.os.geteuid", lambda: caller_uid)
    monkeypatch.setattr("sprintctl.vuoro_credentials.os.getgroups", lambda: list(group_ids))

    def access(path, mode, *, effective_ids=False, follow_symlinks=True):
        try:
            info = Path(path).stat() if follow_symlinks else Path(path).lstat()
        except OSError:
            return False
        if caller_uid == 0:
            return True
        bits = stat.S_IMODE(info.st_mode)
        if caller_uid == info.st_uid:
            allowed = bits >> 6
        elif info.st_gid in group_ids:
            allowed = bits >> 3
        else:
            allowed = bits
        return not (mode & os.W_OK) or bool(allowed & 0o2)

    monkeypatch.setattr("sprintctl.vuoro_credentials.os.access", access)


def _handoff(root: Path, monkeypatch, *, profile_mode=0o644, folder_mode=0o750):
    adapter_uid = os.geteuid()
    adapter_gid = os.getegid()
    # The adapter-owned sticky workroot protects its directory entries; its
    # group is the handoff reader group.
    root.chmod(0o3770)
    folder = root / ".vuoro"
    folder.mkdir()
    folder.chmod(folder_mode)
    profile_path = folder / "profile.json"
    credential_path = folder / "credential"
    raw = {
        "schema_version": "vuoro-client-profile/v1",
        "id": "delegated-worktree",
        "revision": 1,
        "target": {
            "environment_id": "vuoro-dev",
            "environment_class": "development",
            "endpoint": "https://vuoro.example.test",
        },
        "credential_ref": f"file:{credential_path.absolute()}",
        "required_authorities": ["work:read", "work:evidence"],
        "production_endpoint_denied": False,
    }
    profile_path.write_text(json.dumps(raw), encoding="utf-8")
    profile_path.chmod(profile_mode)
    credential_path.write_text(TOKEN, encoding="ascii")
    credential_path.chmod(0o640)
    # The current user owns fixture files as the adapter. Model the deployed
    # service account as a different uid in the handoff reader.
    _simulate_adapter_caller(monkeypatch, uid=adapter_uid + 1, groups={adapter_gid})
    return profile_path, credential_path


def _served_marker(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)
    marker = root / ".sprintctl"
    marker.mkdir(exist_ok=True)
    (marker / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": root.name}), encoding="utf-8"
    )


def _past_python_gate(monkeypatch):
    if sys.version_info < (3, 12):
        monkeypatch.setattr(backend.sys, "version_info", _VersionInfo(3, 12, 0))


def test_marker_plus_exact_cwd_trusted_profile_selects_served(tmp_path, monkeypatch):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    _profile_path, _credential = _handoff(tmp_path, monkeypatch)
    config = backend.load_backend_config(cwd=tmp_path, environ={})
    assert config.mode == "served"
    assert config.served_profile is not None
    assert config.served_profile.trusted_implicit_delegated is True
    assert config.served_profile.source_path == tmp_path / ".vuoro" / "profile.json"


def test_marker_without_exact_cwd_handoff_keeps_existing_mismatch(tmp_path, monkeypatch):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    with pytest.raises(backend.BackendConfigError, match="marker requires served"):
        backend.load_backend_config(cwd=tmp_path, environ={})


def test_explicit_local_precedence_does_not_read_implicit_handoff(tmp_path, monkeypatch):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    _handoff(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "_load_implicit_delegated_profile",
                        lambda *_: pytest.fail("explicit local mode inspected the handoff"))
    with pytest.raises(backend.BackendConfigError, match="cannot be used in repo"):
        backend.load_backend_config(cwd=tmp_path, environ={"SPRINTCTL_BACKEND": "local"})


def test_explicit_profile_keeps_normal_reader_and_precedence(tmp_path, monkeypatch):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    _handoff(tmp_path, monkeypatch)
    external = tmp_path / "explicit.json"
    external.write_text(json.dumps({
        "schema_version": "vuoro-client-profile/v1", "id": "explicit", "target": {
            "environment_id": "vuoro-dev", "endpoint": "https://vuoro.example.test",
        }, "credential_ref": "file:~/.config/vuoro/legacy-token",
    }), encoding="utf-8")
    profile = backend.load_backend_config(cwd=tmp_path, environ={
        "SPRINTCTL_BACKEND": "served", "SPRINTCTL_VUORO_PROFILE": str(external),
    }).served_profile
    assert profile is not None and profile.name == "explicit"
    assert profile.trusted_implicit_delegated is False
    assert served._default_client_credential_resolver(profile) is served.resolve_file_credential


def test_no_ancestor_profile_search(tmp_path, monkeypatch):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    parent_profile, _credential = _handoff(tmp_path, monkeypatch)
    child = tmp_path / "nested"
    child.mkdir()
    with pytest.raises(backend.BackendConfigError, match="marker requires served"):
        backend.load_backend_config(cwd=child, environ={})
    assert parent_profile.exists()


@pytest.mark.parametrize("tamper", ["profile_symlink", "folder_symlink", "fifo", "malformed_profile",
                                     "writable_profile", "writable_folder", "not_sticky_workroot",
                                     "caller_owns_profile"])
def test_untrusted_present_handoff_refuses_without_local_fallback(tmp_path, monkeypatch, tamper):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    profile, _credential = _handoff(tmp_path, monkeypatch)
    folder = tmp_path / ".vuoro"
    if tamper == "profile_symlink":
        real = folder / "real-profile"
        profile.rename(real)
        profile.symlink_to(real)
    elif tamper == "folder_symlink":
        real_folder = tmp_path / "real-vuoro"
        folder.rename(real_folder)
        folder.symlink_to(real_folder)
    elif tamper == "fifo":
        profile.unlink()
        os.mkfifo(profile)
    elif tamper == "malformed_profile":
        profile.write_text("not-json", encoding="utf-8")
    elif tamper == "writable_profile":
        profile.chmod(0o660)
    elif tamper == "writable_folder":
        folder.chmod(0o770)
    elif tamper == "not_sticky_workroot":
        tmp_path.chmod(0o770)
    elif tamper == "caller_owns_profile":
        _simulate_adapter_caller(
            monkeypatch, uid=profile.stat().st_uid, groups={profile.stat().st_gid}
        )
    with pytest.raises(backend.ServedProfileError):
        backend.load_backend_config(cwd=tmp_path, environ={})


@pytest.mark.parametrize(
    ("profile_mode", "folder_mode"),
    [(0o664, 0o750), (0o644, 0o770)],
)
def test_future_group_writers_refuse_even_when_current_caller_lacks_group(
    tmp_path, monkeypatch, profile_mode, folder_mode
):
    _served_marker(tmp_path)
    _past_python_gate(monkeypatch)
    _handoff(
        tmp_path, monkeypatch, profile_mode=profile_mode, folder_mode=folder_mode
    )
    # The modeled caller can read through the public profile bits but is not a
    # member of the group that has write permission. The structural mode
    # allowlist must reject the future writer regardless of current access.
    _simulate_adapter_caller(monkeypatch, uid=os.geteuid() + 1, groups=())
    with pytest.raises(backend.ServedProfileError):
        backend.load_backend_config(cwd=tmp_path, environ={})


def test_delegated_reader_is_fixed_readonly_and_strict(tmp_path, monkeypatch):
    _profile, credential = _handoff(tmp_path, monkeypatch)
    profile = backend._load_implicit_delegated_profile(tmp_path)
    assert profile is not None
    assert resolve_delegated_profile_credential(profile, profile.credential_ref) == TOKEN
    with pytest.raises(CredentialResolutionError):
        resolve_delegated_profile_credential(profile, "file:/tmp/other-token")
    credential.unlink()
    credential.symlink_to(tmp_path / "other-token")
    (tmp_path / "other-token").write_text(TOKEN, encoding="ascii")
    with pytest.raises(CredentialResolutionError):
        resolve_delegated_profile_credential(profile, profile.credential_ref)
    credential.unlink()
    os.mkfifo(credential)
    with pytest.raises(CredentialResolutionError):
        resolve_delegated_profile_credential(profile, profile.credential_ref)
    credential.unlink()
    credential.write_text(TOKEN + "\n", encoding="ascii")
    credential.chmod(0o640)
    with pytest.raises(CredentialResolutionError):
        resolve_delegated_profile_credential(profile, profile.credential_ref)
    credential.write_text(TOKEN, encoding="ascii")
    credential.chmod(0o640)
    assert served._default_client_credential_resolver(profile)(profile.credential_ref) == TOKEN


def test_delegated_callback_selected_only_for_trusted_implicit_source(tmp_path):
    legacy = backend.ServedProfile("legacy", "https://x", "file:~/token", "dev", tmp_path / "p")
    assert served._default_client_credential_resolver(legacy) is served.resolve_file_credential


@pytest.mark.parametrize("factory", [
    served.native_reserve_invoker,
    served.native_evidence_invoker,
    served.native_proposal_invoker,
])
def test_native_sync_callbacks_keep_the_delegated_reader(tmp_path, monkeypatch, factory):
    profile_path, credential_path = _handoff(tmp_path, monkeypatch)
    profile = backend._load_implicit_delegated_profile(tmp_path)
    assert profile is not None
    selected = []

    async def capture(_profile, _operation, _arguments, _credential_resolver=None, **_kwargs):
        selected.append(_credential_resolver)
        return {}

    monkeypatch.setattr(served, "resolve_delegated_profile_credential", lambda _p, _r: TOKEN)
    monkeypatch.setattr(served, "_invoke_operation", capture)
    call = factory(profile, repo_id="repo")
    if factory is served.native_reserve_invoker:
        call("work.run.resolve-v1", {}, None)
    elif factory is served.native_evidence_invoker:
        call("work.run.resolve-v1", {})
    else:
        call("work.run.resolve-v1", {})
    assert len(selected) == 1
    assert selected[0](profile.credential_ref) == TOKEN
    assert profile.source_path == profile_path
    assert Path(profile.credential_ref.removeprefix("file:")) == credential_path
