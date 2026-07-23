import json
import sys
from collections import namedtuple

import pytest

from sprintctl import backend

_VersionInfo = namedtuple("_VersionInfo", ["major", "minor", "micro"])

_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason=(
        "served mode requires Python 3.12+; this test exercises behavior only "
        "reachable past that version gate"
    ),
)


def _write_profile(path, **overrides):
    payload = {
        "schema_version": "vuoro-client-profile/v1",
        "id": "workstation-vuoro-shared",
        "revision": 3,
        "source_environment_id": "workstation-linux",
        "target": {
            "environment_id": "vuoro-shared",
            "environment_class": "production",
            "endpoint": "https://vuoro-shared.apps.kotona.app",
        },
        "credential_ref": "file:~/.config/vuoro/credentials/vuoro-shared-workstation",
        "required_authorities": ["work:read"],
        "production_endpoint_denied": False,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@_requires_312
def test_served_mode_requires_profile(tmp_path):
    (tmp_path / ".git").mkdir()
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "served"},
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=served requires SPRINTCTL_VUORO_PROFILE" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_served_mode_rejects_url(tmp_path):
    (tmp_path / ".git").mkdir()
    profile_path = _write_profile(tmp_path / "profile.json")
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(profile_path),
                "SPRINTCTL_URL": "postgresql://example/db",
            },
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=served cannot be combined with SPRINTCTL_URL" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_served_mode_requires_python_312(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    profile_path = _write_profile(tmp_path / "profile.json")
    monkeypatch.setattr(backend.sys, "version_info", _VersionInfo(3, 11, 0))
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(profile_path),
            },
        )
    except backend.BackendConfigError as exc:
        assert "requires Python 3.12+" in str(exc)
        assert "current interpreter is 3.11" in str(exc)
    else:
        raise AssertionError("expected backend config error")


@_requires_312
def test_served_mode_requires_repo_identity(tmp_path):
    profile_path = _write_profile(tmp_path / "profile.json")
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(profile_path),
            },
        )
    except backend.BackendConfigError as exc:
        assert "cannot resolve repo_id for served mode" in str(exc)
    else:
        raise AssertionError("expected backend config error")


@_requires_312
def test_served_mode_loads_profile_fields(tmp_path):
    (tmp_path / ".git").mkdir()
    profile_path = _write_profile(tmp_path / "profile.json")
    config = backend.load_backend_config(
        cwd=tmp_path,
        environ={
            "SPRINTCTL_BACKEND": "served",
            "SPRINTCTL_VUORO_PROFILE": str(profile_path),
        },
    )
    assert config.mode == "served"
    assert config.served_profile is not None
    assert config.served_profile.name == "workstation-vuoro-shared"
    assert config.served_profile.endpoint == "https://vuoro-shared.apps.kotona.app"
    assert config.served_profile.expected_environment == "vuoro-shared"
    assert config.served_profile.credential_ref == (
        "file:~/.config/vuoro/credentials/vuoro-shared-workstation"
    )


@_requires_312
def test_served_mode_missing_profile_file_errors(tmp_path):
    (tmp_path / ".git").mkdir()
    missing = tmp_path / "does-not-exist.json"
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(missing),
            },
        )
    except backend.BackendConfigError as exc:
        assert "cannot read the configured credential file" not in str(exc)
        assert "cannot read SPRINTCTL_VUORO_PROFILE" in str(exc)
    else:
        raise AssertionError("expected backend config error")


@_requires_312
def test_served_mode_invalid_json_errors(tmp_path):
    (tmp_path / ".git").mkdir()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{not-json", encoding="utf-8")
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(profile_path),
            },
        )
    except backend.BackendConfigError as exc:
        assert "invalid JSON in profile" in str(exc)
    else:
        raise AssertionError("expected backend config error")


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"schema_version": "vuoro-client-profile/v0"}, "unsupported schema_version"),
        ({"id": ""}, "missing a non-empty 'id'"),
        ({"target": "not-an-object"}, "missing a 'target' object"),
        (
            {"target": {"environment_id": "vuoro-shared", "environment_class": "production", "endpoint": "http://insecure"}},
            "must be an https:// URL",
        ),
        (
            {"target": {"environment_class": "production", "endpoint": "https://vuoro-shared.apps.kotona.app"}},
            "missing a non-empty target.environment_id",
        ),
        ({"credential_ref": "env:SOME_VAR"}, "must be a 'file:' reference"),
        (
            {"production_endpoint_denied": True},
            "self-contradictory profile",
        ),
    ],
)
@_requires_312
def test_served_mode_rejects_malformed_profiles(tmp_path, overrides, expected_message):
    (tmp_path / ".git").mkdir()
    profile_path = _write_profile(tmp_path / "profile.json", **overrides)
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "served",
                "SPRINTCTL_VUORO_PROFILE": str(profile_path),
            },
        )
    except backend.BackendConfigError as exc:
        assert expected_message in str(exc)
    else:
        raise AssertionError("expected backend config error")


@_requires_312
def test_backend_marker_accepts_served(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    marker_dir.joinpath("backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    profile_path = _write_profile(tmp_path / "profile.json")

    config = backend.load_backend_config(
        cwd=tmp_path,
        environ={
            "SPRINTCTL_BACKEND": "served",
            "SPRINTCTL_VUORO_PROFILE": str(profile_path),
        },
    )
    assert config.mode == "served"
    assert config.marker.backend == "served"


def test_backend_marker_rejects_unknown_value(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    marker_dir.joinpath("backend.json").write_text(
        json.dumps({"backend": "cloud", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    try:
        backend.load_backend_config(cwd=tmp_path, environ={})
    except backend.BackendConfigError as exc:
        assert "Expected 'local', 'remote', or 'served'" in str(exc)
    else:
        raise AssertionError("expected backend config error")
