import json

import pytest

from sprintctl import backend
import sprintctl.cli as cli_module
from sprintctl.cli import cli


def test_missing_backend_defaults_to_local(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("SPRINTCTL_BACKEND", raising=False)
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)

    config = backend.load_backend_config(cwd=tmp_path)

    assert config.mode == "local"
    assert config.repo_root == tmp_path
    assert config.repo_id == tmp_path.name


def test_url_only_keeps_the_normal_client_local_and_never_opens_postgres(
    tmp_path, monkeypatch, runner
):
    (tmp_path / ".git").mkdir()
    db_path = tmp_path / "sprintctl.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPRINTCTL_BACKEND", raising=False)
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://ignored@example.invalid/work")
    monkeypatch.setenv("SPRINTCTL_DB", str(db_path))

    config = backend.load_backend_config(cwd=tmp_path)
    assert config.mode == "local"

    from sprintctl import pg
    monkeypatch.setattr(
        pg,
        "get_connection",
        lambda _url: pytest.fail("URL-only local mode opened PostgreSQL"),
    )
    result = runner.invoke(cli, ["sprint", "list"])
    assert result.exit_code == 0, result.output
    assert db_path.exists()


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


def test_remote_mode_is_retired_before_url_or_driver_resolution(tmp_path):
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "remote"},
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
        assert "will not open a direct PostgreSQL client" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_remote_mode_is_retired_before_repo_identity_resolution(tmp_path):
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={
                "SPRINTCTL_BACKEND": "remote",
                "SPRINTCTL_URL": "postgresql://example/db",
            },
    )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_legacy_remote_marker_is_retired_even_when_local_is_selected(tmp_path):
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
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
        assert "Update the repository marker to 'served'" in str(exc)
    else:
        raise AssertionError("expected backend config error")


def test_backend_marker_repo_id_mismatch_errors(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": "wrong"}),
        encoding="utf-8",
    )

    config = backend.load_backend_config(cwd=tmp_path, environ={})

    assert config.repo_id == "wrong"
    assert config.repo_source == "marker"


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


def test_scoped_id_parser_accepts_bare_and_explicit_references():
    assert backend.parse_scoped_id("42") == (None, 42)
    assert backend.parse_scoped_id("sprintctl#42") == ("sprintctl", 42)


def test_scoped_id_parser_rejects_malformed_references():
    for value in ("0", "sprintctl#0", "#42", "sprintctl#", "a#1#2", "bad repo#2"):
        try:
            backend.parse_scoped_id(value)
        except backend.ReferenceParseError as exc:
            assert "invalid id reference" in str(exc)
        else:
            raise AssertionError(f"expected malformed reference error for {value!r}")


def test_markerless_remote_cannot_bypass_retirement_with_scoped_opt_in(tmp_path):
    repo = tmp_path / "explicit-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    environment = {
        "SPRINTCTL_BACKEND": "remote",
        "SPRINTCTL_URL": "postgresql://example/db",
        "SPRINTCTL_REPO_ID": "env-repo",
    }
    try:
        backend.load_backend_config(cwd=repo, environ=environment)
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
    else:
        raise AssertionError("expected marker-less remote backend to be rejected")

    config = backend.load_backend_config(
        cwd=repo,
        environ=environment,
        explicit_repo_id=repo.name,
        allow_markerless_nonlocal=True,
        allow_legacy_direct_remote=True,
    )
    assert config.repo_id == repo.name
    assert config.repo_source == "flag"


def test_legacy_remote_marker_without_repo_id_is_retired(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote"}), encoding="utf-8"
    )
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "remote", "SPRINTCTL_URL": "postgresql://example/db"},
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
    else:
        raise AssertionError("expected marker without repo_id to be rejected")


def test_normal_remote_command_fails_before_postgres_import(tmp_path, monkeypatch, runner):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": tmp_path.name}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://example.invalid/sprintctl")

    from sprintctl import pg
    monkeypatch.setattr(
        pg,
        "get_connection",
        lambda _url: pytest.fail("retired remote configuration opened PostgreSQL"),
    )

    result = runner.invoke(cli, ["sprint", "list"])

    assert result.exit_code == 1
    assert "SPRINTCTL_BACKEND=remote is retired" in result.output


def test_explicit_scope_cannot_bypass_legacy_remote_marker(tmp_path):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": "marker-repo"}),
        encoding="utf-8",
    )
    try:
        backend.load_backend_config(
            cwd=tmp_path,
            environ={"SPRINTCTL_BACKEND": "remote", "SPRINTCTL_URL": "postgresql://example/db"},
            explicit_repo_id="other-repo",
        )
    except backend.BackendConfigError as exc:
        assert "SPRINTCTL_BACKEND=remote is retired" in str(exc)
    else:
        raise AssertionError("expected conflicting explicit repo scope to be rejected")


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
    assert "SPRINTCTL_BACKEND=remote is retired" in result.output
    assert not (tmp_path / ".sprintctl").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["item", "show", "--id", "1"],
        ["item", "list"],
        ["sprint", "show"],
        ["event", "list", "--sprint-id", "1"],
    ],
)
def test_markerless_remote_preflight_fails_closed_for_each_named_read(
    tmp_path, monkeypatch, runner, args
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://example/db")
    monkeypatch.delenv("SPRINTCTL_REPO_ID", raising=False)

    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    assert "SPRINTCTL_BACKEND=remote is retired" in result.output
    assert not (tmp_path / ".sprintctl").exists()


def test_local_item_show_not_found_reports_env_resolved_context(tmp_path, monkeypatch, runner):
    workdir = tmp_path / "unrelated"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "local")
    monkeypatch.setenv("SPRINTCTL_REPO_ID", "env-repo")
    monkeypatch.setenv("SPRINTCTL_DB", str(tmp_path / "sprintctl.db"))

    result = runner.invoke(cli, ["item", "show", "--id", "1"])

    assert result.exit_code == 1
    assert "Item #1 not found." in result.output
    assert "Context: repo=env-repo (source=env) backend=local target=local SQLite" in result.output


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
    assert "SPRINTCTL_BACKEND=remote is retired" in result.output
    assert not (marker_dir / "sprintctl.db").exists()
