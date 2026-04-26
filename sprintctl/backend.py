from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class BackendConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BackendMarker:
    path: Path
    backend: str
    repo_id: str | None


@dataclass(frozen=True, slots=True)
class BackendConfig:
    mode: str
    url: str | None
    repo_root: Path | None
    repo_id: str | None
    marker: BackendMarker | None


def _parents_from(path: Path) -> list[Path]:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    return [current, *current.parents]


def _find_upward(start: Path, relative: str) -> Path | None:
    for directory in _parents_from(start):
        candidate = directory / relative
        if candidate.exists():
            return candidate
    return None


def _load_marker(path: Path) -> BackendMarker:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackendConfigError(f"Error: invalid backend marker {path}: {exc}") from exc
    backend = raw.get("backend")
    if backend not in {"local", "remote"}:
        raise BackendConfigError(
            f"Error: invalid backend marker backend={backend!r}. Expected 'local' or 'remote'."
        )
    repo_id = raw.get("repo_id")
    return BackendMarker(path=path, backend=backend, repo_id=str(repo_id) if repo_id else None)


def resolve_repo_identity(cwd: Path | None = None) -> tuple[Path | None, str | None, BackendMarker | None]:
    start = cwd or Path.cwd()
    marker_path = _find_upward(start, ".sprintctl/backend.json")
    marker = _load_marker(marker_path) if marker_path else None
    if marker is not None:
        repo_root = marker.path.parent.parent
        repo_id = repo_root.name
    else:
        sqlite_path = _find_upward(start, ".sprintctl/sprintctl.db")
        if sqlite_path is not None:
            repo_root = sqlite_path.parent.parent
            repo_id = repo_root.name
        else:
            git_path = _find_upward(start, ".git")
            repo_root = git_path.parent if git_path is not None else None
            repo_id = repo_root.name if repo_root is not None else None

    if marker is not None and marker.repo_id is not None and marker.repo_id != repo_id:
        raise BackendConfigError(
            f"Error: repo marker mismatch: marker repo_id='{marker.repo_id}' "
            f"but directory name resolves to '{repo_id}'."
        )
    return repo_root, repo_id, marker


def load_backend_config(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackendConfig:
    env = environ if environ is not None else os.environ
    mode = env.get("SPRINTCTL_BACKEND") or "local"
    if mode not in {"local", "remote"}:
        raise BackendConfigError(
            f"Error: invalid SPRINTCTL_BACKEND='{mode}'. Expected 'local' or 'remote'."
        )
    url = env.get("SPRINTCTL_URL")
    if mode == "remote" and not url:
        raise BackendConfigError("Error: SPRINTCTL_BACKEND=remote requires SPRINTCTL_URL.")

    repo_root, repo_id, marker = resolve_repo_identity(cwd)
    if mode == "remote" and repo_id is None:
        raise BackendConfigError(
            "Error: cannot resolve repo_id for remote mode. Run from inside a repository "
            "with .sprintctl/backend.json or .git."
        )
    if marker is not None and marker.backend != mode:
        assert repo_id is not None
        raise BackendConfigError(
            f"Error: SPRINTCTL_BACKEND={mode} cannot be used in repo '{repo_id}'; "
            f"repo marker requires {marker.backend}."
        )

    return BackendConfig(mode=mode, url=url, repo_root=repo_root, repo_id=repo_id, marker=marker)


def require_local_backend() -> BackendConfig:
    config = load_backend_config()
    if config.mode == "remote":
        raise BackendConfigError("Error: remote backend is not implemented yet.")
    return config
