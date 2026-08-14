"""Per-repository configuration for guarded projection-backed reads.

This module owns the opt-in toggle only: it does not build read models,
judge projection freshness, or change any backend write path.  When enabled,
CLI read commands (see ``sprintctl/cli.py``) consult the cached projection
already populated by the normal sync path (``sprintctl/sync.py``) and fall
back to the current backend explicitly
whenever that cache is missing, stale, on an incompatible schema, or has
never been synchronized -- see ``sprintctl/projection.py:assess_freshness``.

The persisted per-repository file mirrors the existing opt-in conventions in
``sprintctl/authority_config.py`` (authority-command configuration): a small
versioned JSON document fixed
below ``.sprintctl``, defaulting to disabled, written atomically.  A
``SPRINTCTL_PROJECTION_READS`` environment variable can override the
persisted file for one invocation (handy for CI/tests and quick opt-in
without touching repo-local state); the env var wins when set.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from . import backend


PROJECTION_READS_CONFIG_VERSION = 1
_STATE_DIRECTORY_NAME = ".sprintctl"
_CONFIG_FILENAME = "projection-reads.json"
ENV_VAR = "SPRINTCTL_PROJECTION_READS"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class ProjectionReadsConfigError(ValueError):
    """The per-repository projection-reads configuration is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class ProjectionReadsPaths:
    """The one file this module owns, derived from a repository root."""

    repo_root: Path
    state_dir: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class ProjectionReadsConfig:
    """Validated, versioned configuration persisted for one repository."""

    enabled: bool = False
    version: int = PROJECTION_READS_CONFIG_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ProjectionReadsConfigError("projection reads enabled must be a boolean")
        if self.version != PROJECTION_READS_CONFIG_VERSION:
            raise ProjectionReadsConfigError(
                "unsupported projection reads config version "
                f"{self.version!r}; expected {PROJECTION_READS_CONFIG_VERSION}"
            )

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class ProjectionReadsStatus:
    """Effective, serializable enable/disable state for one repository."""

    enabled: bool
    source: str  # "env" | "config" | "default"
    configured: bool
    paths: ProjectionReadsPaths

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "configured": self.configured,
            "repo_root": str(self.paths.repo_root),
            "config_path": str(self.paths.config_path),
        }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validated_repo_root(repo_root: Path) -> Path:
    resolved = repo_root.resolve()
    if not resolved.is_dir():
        raise ProjectionReadsConfigError(
            f"projection reads repo root is not a directory: {repo_root}"
        )
    return resolved


def _validated_paths(repo_root: Path) -> ProjectionReadsPaths:
    root = _validated_repo_root(repo_root)
    state_dir = root / _STATE_DIRECTORY_NAME
    if not _is_within(state_dir.resolve(), root):
        raise ProjectionReadsConfigError(
            f"projection reads state directory must remain under {root}: {state_dir}"
        )
    config_path = state_dir / _CONFIG_FILENAME
    if not _is_within(config_path.resolve(), state_dir.resolve()):
        raise ProjectionReadsConfigError(
            f"projection reads config path must remain under {state_dir}: {config_path}"
        )
    return ProjectionReadsPaths(repo_root=root, state_dir=state_dir, config_path=config_path)


def projection_reads_paths(
    *, cwd: Path | None = None, repo_root: Path | None = None
) -> ProjectionReadsPaths:
    """Derive the fixed projection-reads config path for a repository."""
    if repo_root is None:
        resolved_root, _repo_id, _marker = backend.resolve_repo_identity(cwd)
        if resolved_root is None:
            raise ProjectionReadsConfigError(
                "cannot resolve a repository for projection reads configuration"
            )
        repo_root = resolved_root
    return _validated_paths(repo_root)


def _parse_config(path: Path) -> ProjectionReadsConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionReadsConfigError(f"invalid projection reads config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectionReadsConfigError(f"invalid projection reads config {path}: expected an object")

    expected = {"version", "enabled"}
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown or missing:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ProjectionReadsConfigError(
            f"invalid projection reads config {path}: {'; '.join(details)}"
        )
    return ProjectionReadsConfig(enabled=raw["enabled"], version=raw["version"])


def load_projection_reads_config(paths: ProjectionReadsPaths) -> ProjectionReadsConfig:
    """Load config or return the safe disabled default when absent."""
    expected = _validated_paths(paths.repo_root)
    if paths != expected:
        raise ProjectionReadsConfigError("projection reads paths must be derived from the repo root")
    if not paths.config_path.exists():
        return ProjectionReadsConfig()
    if not paths.config_path.is_file():
        raise ProjectionReadsConfigError(
            f"projection reads config is not a regular file: {paths.config_path}"
        )
    return _parse_config(paths.config_path)


def _env_override(environ: Mapping[str, str]) -> bool | None:
    raw = environ.get(ENV_VAR)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ProjectionReadsConfigError(
        f"invalid {ENV_VAR}={raw!r}; expected one of "
        f"{sorted(_TRUE_VALUES | _FALSE_VALUES)}"
    )


def projection_reads_status(
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectionReadsStatus:
    """Return effective state without creating files or changing authority.

    Precedence: ``SPRINTCTL_PROJECTION_READS`` (if set) overrides the
    persisted per-repository file; the file overrides the disabled default.
    """
    env = environ if environ is not None else os.environ
    paths = projection_reads_paths(cwd=cwd, repo_root=repo_root)
    override = _env_override(env)
    if override is not None:
        return ProjectionReadsStatus(
            enabled=override, source="env", configured=paths.config_path.exists(), paths=paths
        )
    config = load_projection_reads_config(paths)
    source = "config" if paths.config_path.exists() else "default"
    return ProjectionReadsStatus(
        enabled=config.enabled, source=source, configured=paths.config_path.exists(), paths=paths
    )


def set_projection_reads_enabled(
    enabled: bool,
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
) -> ProjectionReadsStatus:
    """Explicitly persist the repository's opt-in or opt-out decision.

    The write is atomic and the resulting file is private to the current
    user.  It is the sole mutation in this module.  ``SPRINTCTL_PROJECTION_READS``
    still overrides this persisted value at read time -- this only changes
    the on-disk default.
    """
    if not isinstance(enabled, bool):
        raise ProjectionReadsConfigError("projection reads enabled must be a boolean")
    paths = projection_reads_paths(cwd=cwd, repo_root=repo_root)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = _validated_paths(paths.repo_root)
    payload = json.dumps(
        ProjectionReadsConfig(enabled=enabled).to_dict(), sort_keys=True, indent=2
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{_CONFIG_FILENAME}.", dir=paths.state_dir, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, paths.config_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return projection_reads_status(repo_root=paths.repo_root)


__all__ = [
    "ENV_VAR",
    "PROJECTION_READS_CONFIG_VERSION",
    "ProjectionReadsConfig",
    "ProjectionReadsConfigError",
    "ProjectionReadsPaths",
    "ProjectionReadsStatus",
    "load_projection_reads_config",
    "projection_reads_paths",
    "projection_reads_status",
    "set_projection_reads_enabled",
]
