import os

import pytest

from sprintctl.vuoro_credentials import CredentialResolutionError, resolve_file_credential


def _write_secret(path, content: bytes, mode: int = 0o600):
    path.write_bytes(content)
    os.chmod(path, mode)
    return path


def test_resolves_absolute_path(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"topsecret\n")
    assert resolve_file_credential(f"file:{secret}") == "topsecret"


def test_resolves_home_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr("sprintctl.vuoro_credentials.Path.home", lambda: tmp_path)
    config_dir = tmp_path / ".config" / "vuoro" / "credentials"
    config_dir.mkdir(parents=True)
    _write_secret(config_dir / "vuoro-shared-workstation", b"topsecret\n")

    resolved = resolve_file_credential("file:~/.config/vuoro/credentials/vuoro-shared-workstation")
    assert resolved == "topsecret"


def test_strips_exactly_one_trailing_newline(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"topsecret\n\n")
    assert resolve_file_credential(f"file:{secret}") == "topsecret\n"


def test_no_trailing_newline_is_unaffected(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"topsecret")
    assert resolve_file_credential(f"file:{secret}") == "topsecret"


def test_rejects_empty_file(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"")
    with pytest.raises(CredentialResolutionError, match="empty"):
        resolve_file_credential(f"file:{secret}")


def test_rejects_file_that_is_only_a_newline(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"\n")
    with pytest.raises(CredentialResolutionError, match="empty"):
        resolve_file_credential(f"file:{secret}")


def test_rejects_wrong_permissions(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"topsecret\n", mode=0o644)
    with pytest.raises(CredentialResolutionError, match="mode 0600"):
        resolve_file_credential(f"file:{secret}")


def test_rejects_symlink(tmp_path):
    real = _write_secret(tmp_path / "real-cred", b"topsecret\n")
    link = tmp_path / "cred-link"
    link.symlink_to(real)
    with pytest.raises(CredentialResolutionError, match="symlink"):
        resolve_file_credential(f"file:{link}")


def test_rejects_directory(tmp_path):
    directory = tmp_path / "cred-dir"
    directory.mkdir()
    os.chmod(directory, 0o600)
    with pytest.raises(CredentialResolutionError, match="regular file"):
        resolve_file_credential(f"file:{directory}")


def test_rejects_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(CredentialResolutionError, match="cannot stat"):
        resolve_file_credential(f"file:{missing}")


def test_rejects_unsupported_scheme(tmp_path):
    with pytest.raises(CredentialResolutionError, match="expected 'file:'"):
        resolve_file_credential("env:SOME_VAR")


def test_rejects_relative_path_without_tilde(tmp_path):
    with pytest.raises(CredentialResolutionError, match="absolute or start with"):
        resolve_file_credential("file:relative/path")


def test_error_never_renders_file_contents(tmp_path):
    secret = _write_secret(tmp_path / "cred", b"do-not-leak-me\n", mode=0o644)
    try:
        resolve_file_credential(f"file:{secret}")
    except CredentialResolutionError as exc:
        assert "do-not-leak-me" not in str(exc)
    else:
        raise AssertionError("expected credential resolution error")
