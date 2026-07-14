"""Per-repository configuration for the observation-only shadow pilot.

The pilot is deliberately opt-in.  This module owns configuration and path
derivation only: it does not write observations, apply projections, or alter
the current SQLite/PostgreSQL authority path.  Keeping those concerns here
would make disabling the pilot less trustworthy during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import tempfile

from . import backend


SHADOW_PILOT_CONFIG_VERSION = 1
"""The only supported on-disk shadow-pilot configuration version."""

_STATE_DIRECTORY_NAME = ".sprintctl"
_CONFIG_FILENAME = "shadow-pilot.json"
_OUTBOX_FILENAME = "shadow-pilot-outbox.db"
_PROJECTION_FILENAME = "shadow-pilot-projection.db"


class ShadowPilotConfigError(ValueError):
    """The per-repository pilot configuration is malformed or unsafe."""


class ShadowPilotState(StrEnum):
    """A stable, CLI-ready representation of the pilot's effective state."""

    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class ShadowPilotPaths:
    """All pilot files derived from one repository root.

    There are intentionally no user-configurable storage paths.  A pilot
    repository owns its files below ``.sprintctl`` and can disable the pilot
    by changing one small config file.
    """

    repo_root: Path
    state_dir: Path
    config_path: Path
    outbox_path: Path
    projection_path: Path


@dataclass(frozen=True, slots=True)
class ShadowPilotConfig:
    """Validated, versioned configuration persisted for one repository."""

    enabled: bool = False
    version: int = SHADOW_PILOT_CONFIG_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ShadowPilotConfigError("shadow pilot enabled must be a boolean")
        if self.version != SHADOW_PILOT_CONFIG_VERSION:
            raise ShadowPilotConfigError(
                "unsupported shadow pilot config version "
                f"{self.version!r}; expected {SHADOW_PILOT_CONFIG_VERSION}"
            )

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class ShadowPilotStatus:
    """Effective, serializable state for a future CLI status command."""

    state: ShadowPilotState
    configured: bool
    paths: ShadowPilotPaths

    @property
    def enabled(self) -> bool:
        return self.state is ShadowPilotState.ENABLED

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "enabled": self.enabled,
            "configured": self.configured,
            "repo_root": str(self.paths.repo_root),
            "config_path": str(self.paths.config_path),
            "outbox_path": str(self.paths.outbox_path),
            "projection_path": str(self.paths.projection_path),
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
        raise ShadowPilotConfigError(f"shadow pilot repo root is not a directory: {repo_root}")
    return resolved


def _validated_paths(repo_root: Path) -> ShadowPilotPaths:
    root = _validated_repo_root(repo_root)
    state_dir = root / _STATE_DIRECTORY_NAME
    # resolve() follows an existing symlink; rejecting an escaped state
    # directory prevents a repo-local setting from redirecting pilot storage.
    if not _is_within(state_dir.resolve(), root):
        raise ShadowPilotConfigError(
            f"shadow pilot state directory must remain under {root}: {state_dir}"
        )
    paths = ShadowPilotPaths(
        repo_root=root,
        state_dir=state_dir,
        config_path=state_dir / _CONFIG_FILENAME,
        outbox_path=state_dir / _OUTBOX_FILENAME,
        projection_path=state_dir / _PROJECTION_FILENAME,
    )
    for path in (paths.config_path, paths.outbox_path, paths.projection_path):
        if not _is_within(path.resolve(), state_dir.resolve()):
            raise ShadowPilotConfigError(
                f"shadow pilot path must remain under {state_dir}: {path}"
            )
    return paths


def shadow_pilot_paths(
    *, cwd: Path | None = None, repo_root: Path | None = None
) -> ShadowPilotPaths:
    """Derive the complete, fixed pilot layout for a repository.

    ``repo_root`` is useful to callers that already resolved repository
    identity.  Otherwise the existing backend resolver is used so nested
    repository working directories behave the same way as sprintctl commands.
    """
    if repo_root is None:
        resolved_root, _repo_id, _marker = backend.resolve_repo_identity(cwd)
        if resolved_root is None:
            raise ShadowPilotConfigError(
                "cannot resolve a repository for shadow pilot configuration"
            )
        repo_root = resolved_root
    return _validated_paths(repo_root)


def _parse_config(path: Path) -> ShadowPilotConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowPilotConfigError(f"invalid shadow pilot config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ShadowPilotConfigError(f"invalid shadow pilot config {path}: expected an object")

    expected = {"version", "enabled"}
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown or missing:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ShadowPilotConfigError(
            f"invalid shadow pilot config {path}: {'; '.join(details)}"
        )
    return ShadowPilotConfig(enabled=raw["enabled"], version=raw["version"])


def load_shadow_pilot_config(paths: ShadowPilotPaths) -> ShadowPilotConfig:
    """Load config or return the safe disabled default when absent."""
    # Revalidate a caller-provided dataclass so a later CLI cannot inject a
    # path from another repository by constructing ShadowPilotPaths directly.
    expected = _validated_paths(paths.repo_root)
    if paths != expected:
        raise ShadowPilotConfigError("shadow pilot paths must be derived from the repo root")
    if not paths.config_path.exists():
        return ShadowPilotConfig()
    if not paths.config_path.is_file():
        raise ShadowPilotConfigError(
            f"shadow pilot config is not a regular file: {paths.config_path}"
        )
    return _parse_config(paths.config_path)


def shadow_pilot_status(
    *, cwd: Path | None = None, repo_root: Path | None = None
) -> ShadowPilotStatus:
    """Return effective state without creating files or changing authority."""
    paths = shadow_pilot_paths(cwd=cwd, repo_root=repo_root)
    config = load_shadow_pilot_config(paths)
    return ShadowPilotStatus(
        state=ShadowPilotState.ENABLED if config.enabled else ShadowPilotState.DISABLED,
        configured=paths.config_path.exists(),
        paths=paths,
    )


def set_shadow_pilot_enabled(
    enabled: bool,
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
) -> ShadowPilotStatus:
    """Explicitly persist the repository's opt-in or opt-out decision.

    The write is atomic and the resulting file is private to the current user.
    It is the sole mutation in this module; all runtime paths remain derived.
    """
    if not isinstance(enabled, bool):
        raise ShadowPilotConfigError("shadow pilot enabled must be a boolean")
    paths = shadow_pilot_paths(cwd=cwd, repo_root=repo_root)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The directory may have appeared after initial resolution, so prove it
    # still has not become an out-of-repository symlink before the write.
    paths = _validated_paths(paths.repo_root)
    payload = json.dumps(
        ShadowPilotConfig(enabled=enabled).to_dict(), sort_keys=True, indent=2
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
    return shadow_pilot_status(repo_root=paths.repo_root)
