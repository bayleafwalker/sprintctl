from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SERVED_MIN_PYTHON = (3, 12)
SERVED_PROFILE_SCHEMA_VERSION = "vuoro-client-profile/v1"


class BackendConfigError(ValueError):
    pass


class ServedProfileError(BackendConfigError):
    pass


class ReferenceParseError(BackendConfigError):
    pass


@dataclass(frozen=True, slots=True)
class BackendMarker:
    path: Path
    backend: str
    repo_id: str | None


@dataclass(frozen=True, slots=True)
class ServedProfile:
    """The subset of a validated vuoro-client-profile/v1 file sprintctl needs.

    Deliberately independent of ``vuoro_client.Profile`` so importing this
    module never requires the served extra to be installed; ``served.py``
    converts this into a ``vuoro_client.Profile`` at call time.
    """

    name: str
    endpoint: str
    credential_ref: str
    expected_environment: str
    source_path: Path


@dataclass(frozen=True, slots=True)
class BackendConfig:
    mode: str
    url: str | None
    repo_root: Path | None
    repo_id: str | None
    repo_source: str
    marker: BackendMarker | None
    served_profile: ServedProfile | None = None


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
    if not isinstance(raw, dict):
        raise BackendConfigError(
            f"Error: invalid backend marker {path}: expected a JSON object."
        )
    backend = raw.get("backend")
    if backend not in {"local", "remote", "served"}:
        raise BackendConfigError(
            f"Error: invalid backend marker backend={backend!r}. "
            "Expected 'local', 'remote', or 'served'."
        )
    repo_id = raw.get("repo_id")
    return BackendMarker(path=path, backend=backend, repo_id=str(repo_id) if repo_id else None)


def resolve_repo_identity(cwd: Path | None = None) -> tuple[Path | None, str | None, BackendMarker | None]:
    start = cwd or Path.cwd()
    marker_path = _find_upward(start, ".sprintctl/backend.json")
    marker = _load_marker(marker_path) if marker_path else None
    if marker is not None:
        repo_root = marker.path.parent.parent
        # The committed marker is stable across a renamed checkout.  The
        # directory name remains a compatibility fallback only when no marker
        # exists; it must not turn a clone rename into an identity error.
        repo_id = marker.repo_id or repo_root.name
    else:
        sqlite_path = _find_upward(start, ".sprintctl/sprintctl.db")
        if sqlite_path is not None:
            repo_root = sqlite_path.parent.parent
            repo_id = repo_root.name
        else:
            git_path = _find_upward(start, ".git")
            repo_root = git_path.parent if git_path is not None else None
            repo_id = repo_root.name if repo_root is not None else None

    return repo_root, repo_id, marker


def parse_scoped_id(value: str | int, *, field: str = "id") -> tuple[str | None, int]:
    """Parse either a local numeric ID or an explicit ``repo#id`` reference.

    This is intentionally a presentation-layer parser: database identity and
    schema remain unchanged.  Command groups can adopt it incrementally while
    sharing one strict malformed-reference error contract.
    """
    raw = str(value)
    if raw.isdecimal() and int(raw) > 0:
        return None, int(raw)
    if raw.count("#") != 1:
        raise ReferenceParseError(
            f"Error: invalid {field} reference {raw!r}; use a positive integer or repo#id."
        )
    repo_id, identifier = raw.split("#", 1)
    if not repo_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", repo_id):
        raise ReferenceParseError(
            f"Error: invalid {field} reference {raw!r}; repository name is malformed."
        )
    if not identifier.isdecimal() or int(identifier) < 1:
        raise ReferenceParseError(
            f"Error: invalid {field} reference {raw!r}; ID must be a positive integer."
        )
    return repo_id, int(identifier)


def _load_served_profile(path: Path) -> ServedProfile:
    """Parse and minimally validate a vuoro-client-profile/v1 file.

    This intentionally does not reimplement the full JSON-Schema check that
    ``validate_vuoro_profiles.py`` (agentops) already runs as a pre-selection
    operator gate; it only checks what sprintctl needs to fail clearly on a
    malformed or mismatched file rather than construct a broken profile.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServedProfileError(
            f"Error: cannot read SPRINTCTL_VUORO_PROFILE at {path}: {exc}"
        ) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ServedProfileError(f"Error: invalid JSON in profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ServedProfileError(f"Error: profile {path} must be a JSON object.")

    schema_version = raw.get("schema_version")
    if schema_version != SERVED_PROFILE_SCHEMA_VERSION:
        raise ServedProfileError(
            f"Error: profile {path} has unsupported schema_version={schema_version!r}; "
            f"expected {SERVED_PROFILE_SCHEMA_VERSION!r}."
        )

    name = raw.get("id")
    if not isinstance(name, str) or not name:
        raise ServedProfileError(f"Error: profile {path} is missing a non-empty 'id'.")

    target = raw.get("target")
    if not isinstance(target, dict):
        raise ServedProfileError(f"Error: profile {path} is missing a 'target' object.")

    endpoint = target.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ServedProfileError(
            f"Error: profile {path} target.endpoint must be an https:// URL."
        )

    expected_environment = target.get("environment_id")
    if not isinstance(expected_environment, str) or not expected_environment:
        raise ServedProfileError(
            f"Error: profile {path} is missing a non-empty target.environment_id."
        )

    credential_ref = raw.get("credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref.startswith("file:"):
        raise ServedProfileError(
            f"Error: profile {path} credential_ref must be a 'file:' reference."
        )

    if (
        target.get("environment_class") == "production"
        and raw.get("production_endpoint_denied") is True
    ):
        raise ServedProfileError(
            f"Error: profile {path} targets a production environment_class but sets "
            "production_endpoint_denied=true; refusing to load a self-contradictory profile."
        )

    return ServedProfile(
        name=name,
        endpoint=endpoint,
        credential_ref=credential_ref,
        expected_environment=expected_environment,
        source_path=path,
    )


def load_backend_config(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    explicit_repo_id: str | None = None,
    allow_markerless_nonlocal: bool = False,
) -> BackendConfig:
    env = environ if environ is not None else os.environ
    mode = env.get("SPRINTCTL_BACKEND") or "local"
    if mode not in {"local", "remote", "served"}:
        raise BackendConfigError(
            f"Error: invalid SPRINTCTL_BACKEND='{mode}'. Expected 'local', 'remote', or 'served'."
        )
    url = env.get("SPRINTCTL_URL")
    if mode == "remote" and not url:
        raise BackendConfigError("Error: SPRINTCTL_BACKEND=remote requires SPRINTCTL_URL.")

    served_profile: ServedProfile | None = None
    if mode == "served":
        if url:
            raise BackendConfigError(
                "Error: SPRINTCTL_BACKEND=served cannot be combined with SPRINTCTL_URL. "
                "Served mode derives its endpoint from SPRINTCTL_VUORO_PROFILE only."
            )
        if sys.version_info < SERVED_MIN_PYTHON:
            current = f"{sys.version_info.major}.{sys.version_info.minor}"
            required = ".".join(str(part) for part in SERVED_MIN_PYTHON)
            raise BackendConfigError(
                f"Error: SPRINTCTL_BACKEND=served requires Python {required}+ "
                f"(vuoro-client's minimum); the current interpreter is {current}."
            )
        profile_path_raw = env.get("SPRINTCTL_VUORO_PROFILE")
        if not profile_path_raw:
            raise BackendConfigError(
                "Error: SPRINTCTL_BACKEND=served requires SPRINTCTL_VUORO_PROFILE."
            )
        served_profile = _load_served_profile(Path(profile_path_raw).expanduser())

    repo_root, inferred_repo_id, marker = resolve_repo_identity(cwd)
    env_repo_id = env.get("SPRINTCTL_REPO_ID")
    marker_repo_id = marker.repo_id if marker is not None else None
    if explicit_repo_id:
        repo_id = explicit_repo_id
        repo_source = "flag"
    elif env_repo_id:
        repo_id = env_repo_id
        repo_source = "env"
    elif marker_repo_id:
        repo_id = marker_repo_id
        repo_source = "marker"
    else:
        repo_id = inferred_repo_id
        repo_source = "cwd" if inferred_repo_id is not None else "unresolved"

    # A marker is the committed repository identity.  Neither a flag nor an
    # environment override may silently repoint a checkout at another tenant.
    if marker_repo_id is not None and repo_id is not None and repo_id != marker_repo_id:
        raise BackendConfigError(
            f"Error: repo scope mismatch: {repo_source} repo_id='{repo_id}' "
            f"but repo marker requires '{marker_repo_id}'."
        )
    # Without a marker, an explicit repo scope must still corroborate an
    # available cwd identity rather than silently cross a repository boundary.
    if (
        marker is None
        and explicit_repo_id is not None
        and inferred_repo_id is not None
        and explicit_repo_id != inferred_repo_id
    ):
        raise BackendConfigError(
            f"Error: repo scope mismatch: flag repo_id='{explicit_repo_id}' "
            f"but cwd resolves to '{inferred_repo_id}'."
        )
    if mode in {"remote", "served"} and repo_id is None:
        raise BackendConfigError(
            f"Error: cannot resolve repo_id for {mode} mode. Run from inside a repository "
            "with .sprintctl/backend.json or .git."
        )
    if marker is not None and marker.backend != mode:
        assert repo_id is not None
        raise BackendConfigError(
            f"Error: SPRINTCTL_BACKEND={mode} cannot be used in repo '{repo_id}'; "
            f"repo marker requires {marker.backend}."
        )
    if mode in {"remote", "served"} and marker_repo_id is None:
        if not explicit_repo_id or not allow_markerless_nonlocal:
            raise BackendConfigError(
                f"Error: backend-uncorroborated: {mode} mode without a repository marker identity "
                "requires both --repo-id and --allow-markerless-nonlocal for this invocation."
            )

    return BackendConfig(
        mode=mode,
        url=url,
        repo_root=repo_root,
        repo_id=repo_id,
        repo_source=repo_source,
        marker=marker,
        served_profile=served_profile,
    )


def require_local_backend() -> BackendConfig:
    config = load_backend_config()
    if config.mode == "remote":
        raise BackendConfigError("Error: remote backend is not implemented yet.")
    return config


def require_remote_backend() -> BackendConfig:
    config = load_backend_config()
    if config.mode != "remote":
        raise BackendConfigError(
            "Error: this command requires SPRINTCTL_BACKEND=remote "
            f"(current: '{config.mode}')."
        )
    return config
