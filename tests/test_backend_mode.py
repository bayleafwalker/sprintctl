import json

from sprintctl import backend
from sprintctl.cli import cli


def test_missing_backend_defaults_to_local(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("SPRINTCTL_BACKEND", raising=False)
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)

    config = backend.load_backend_config(cwd=tmp_path)

    assert config.mode == "local"
    assert config.repo_root == tmp_path
    assert config.repo_id == tmp_path.name


def test_invalid_backend_value_errors(tmp_path):
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "other"},
        )
    except backend.BackendConfigError as exc:
        assert "invalid SPRINTCTL_BACKEND='other'" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_invalid_backend_marker_json_errors(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text("{not-json", encoding="utf-8")

    try:
        backend.load_backend_config(cwd=tmp_path, environ={})
    except backend.BackendConfigError as exc:
        assert "invalid backend marker" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_invalid_backend_marker_shape_errors(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(json.dumps(["remote"]), encoding="utf-8")

    try:
        backend.load_backend_config(cwd=tmp_path, environ={})
    except backend.BackendConfigError as exc:
        assert "expected a JSON object" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_remote_mode_requires_url(tmp_path):
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "remote"},
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote requires SPRINTCTL_URL" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_remote_mode_requires_repo_identity(tmp_path):
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "remote",
                "SPRINTCTL_URL": "postgresql://example/db",
            },
        )
    except backend.BackendConfigError as exc:
        assert "cannot resolve repo_id for remote mode" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_backend_marker_mode_mismatch_errors(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )

    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "local"},
        )
    except backend.BackendConfigError as exc:
        assert f"SPRINTCTL_BACKEND=local cannot be used in repo '{tmp_path.name}'" in str(exc)
        assert "repo marker requires remote" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_backend_marker_repo_id_mismatch_errors(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": "wrong"}),
        encoding="utf-8",
    )

    try:
        backend.load_backend_config(cwd=tmp_path, environ={})
    except backend.BackendConfigError as exc:
        assert "repo marker mismatch: marker repo_id='wrong'" in str(exc)
        assert f"directory name resolves to '{tmp_path.name}'" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_repo_identity_prefers_backend_marker(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    marker_dir = repo / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": "repo"}),
        encoding="utf-8",
    )

    repo_root, repo_id, marker = backend.resolve_repo_identity(nested)

    assert repo_root == repo
    assert repo_id == "repo"
    assert marker is not None


def test_repo_identity_uses_sqlite_marker_before_git(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    sprintctl_dir = repo / ".sprintctl"
    sprintctl_dir.mkdir()
    (sprintctl_dir / "sprintctl.db").write_text("", encoding="utf-8")

    repo_root, repo_id, marker = backend.resolve_repo_identity(nested)

    assert repo_root == repo
    assert repo_id == "repo"
    assert marker is None


def test_repo_identity_uses_sqlite_directory_sentinel_before_git(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    sprintctl_dir = repo / ".sprintctl"
    sprintctl_dir.mkdir()
    (sprintctl_dir / "sprintctl.db").mkdir()

    repo_root, repo_id, marker = backend.resolve_repo_identity(nested)

    assert repo_root == repo
    assert repo_id == "repo"
    assert marker is None


def test_cli_preflight_errors_before_creating_local_db(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://example/db")

    result = runner.invoke(cli, ["sprint", "list"])

    assert result.exit_code == 1
    assert "cannot resolve repo_id for remote mode" in result.output
    assert not (tmp_path / ".sprintctl").exists()


def test_cli_local_marker_mismatch_errors_before_sqlite_open(tmp_path, monkeypatch, runner):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "local")

    result = runner.invoke(cli, ["sprint", "list"])

    assert result.exit_code == 1
    assert "repo marker requires remote" in result.output
    assert not (marker_dir / "sprintctl.db").exists()
