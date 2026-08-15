"""Read-only project binding resolution for cross-repository views."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from uuid import UUID


_REPO_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_RENDER_LEVELS = {"full", "baseline", "none"}
_MEMBER_ACCESS = {"write", "reference"}
_ROLE_PRESETS = {"planner", "worker", "reviewer"}
_ROLE_MODELS = {"Sol", "Luna"}
_ROLE_BEHAVIORS = {"high", "xhigh"}
_ROLE_TOOL_MODES = {"read-only", "write"}


class ProjectConfigError(ValueError):
    """The requested project binding is absent or invalid."""


@dataclass(frozen=True, slots=True)
class ProjectMember:
    repo_id: str
    backlog: bool
    render: str
    relationship: str | None
    access: str | None
    repository: str | None
    default_ref: str | None
    path_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    path: Path
    project_id: str
    display_name: str
    home_repo: str
    members: tuple[ProjectMember, ...]

    @property
    def backlog_members(self) -> tuple[ProjectMember, ...]:
        return tuple(member for member in self.members if member.backlog)

    def summary(self) -> dict:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "home_repo": self.home_repo,
            "project_file": str(self.path),
            "backlog_repos": [member.repo_id for member in self.backlog_members],
        }


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProjectConfigError(
            f"project.toml {field} must be a non-empty string without surrounding whitespace"
        )
    return value


def _project_id(value: object) -> str:
    text = _required_text(value, "project_id")
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ProjectConfigError("project.toml project_id must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != text:
        raise ProjectConfigError("project.toml project_id must be a canonical UUIDv4")
    return text


def _member(raw: object, index: int) -> ProjectMember:
    field = f"members[{index}]"
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"project.toml {field} must be a table")
    unknown = sorted(
        set(raw)
        - {
            "repo_id",
            "backlog",
            "render",
            "relationship",
            "access",
            "repository",
            "default_ref",
            "path_notes",
        }
    )
    if unknown:
        raise ProjectConfigError(
            f"project.toml {field} has unknown fields: {', '.join(unknown)}"
        )
    repo_id = _required_text(raw.get("repo_id"), f"{field}.repo_id")
    if not _REPO_ID.fullmatch(repo_id):
        raise ProjectConfigError(
            f"project.toml {field}.repo_id is invalid: {repo_id!r}"
        )
    backlog = raw.get("backlog")
    if not isinstance(backlog, bool):
        raise ProjectConfigError(f"project.toml {field}.backlog must be a boolean")
    render = raw.get("render")
    if render not in _RENDER_LEVELS:
        raise ProjectConfigError(
            f"project.toml {field}.render must be one of: baseline, full, none"
        )
    relationship_raw = raw.get("relationship")
    relationship = (
        _required_text(relationship_raw, f"{field}.relationship")
        if relationship_raw is not None
        else None
    )
    access_raw = raw.get("access")
    if access_raw is not None and access_raw not in _MEMBER_ACCESS:
        raise ProjectConfigError(
            f"project.toml {field}.access must be one of: reference, write"
        )
    repository_raw = raw.get("repository")
    repository = (
        _required_text(repository_raw, f"{field}.repository")
        if repository_raw is not None
        else None
    )
    default_ref_raw = raw.get("default_ref")
    default_ref = (
        _required_text(default_ref_raw, f"{field}.default_ref")
        if default_ref_raw is not None
        else None
    )
    notes = raw.get("path_notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise ProjectConfigError(
            f"project.toml {field}.path_notes must be an array of strings"
        )
    return ProjectMember(
        repo_id,
        backlog,
        render,
        relationship,
        access_raw,
        repository,
        default_ref,
        tuple(notes),
    )


def _validate_role_presets(raw: object) -> None:
    """Accept only descriptive project role metadata.

    Sprintctl does not route models or grant execution authority.  It still
    validates this Agentops-owned binding field so project-scoped read commands
    remain compatible without silently accepting authority-shaped extensions.
    """
    if not isinstance(raw, dict):
        raise ProjectConfigError("project.toml role_presets must be a table")
    unknown_roles = sorted(set(raw) - _ROLE_PRESETS)
    if unknown_roles:
        raise ProjectConfigError(
            "project.toml role_presets has unsupported roles: "
            + ", ".join(unknown_roles)
        )
    for role, preset in raw.items():
        field = f"project.toml role_presets.{role}"
        if not isinstance(preset, dict):
            raise ProjectConfigError(f"{field} must be a table")
        expected = {"model", "behavior", "tool_mode"}
        if set(preset) != expected:
            unsupported = sorted(set(preset) - expected)
            missing = sorted(expected - set(preset))
            details = []
            if unsupported:
                details.append(f"unsupported fields: {', '.join(unsupported)}")
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            raise ProjectConfigError(f"{field} has " + "; ".join(details))
        if preset["model"] not in _ROLE_MODELS:
            raise ProjectConfigError(f"{field}.model must be Sol or Luna")
        if preset["behavior"] not in _ROLE_BEHAVIORS:
            raise ProjectConfigError(f"{field}.behavior must be high or xhigh")
        if preset["tool_mode"] not in _ROLE_TOOL_MODES:
            raise ProjectConfigError(f"{field}.tool_mode must be read-only or write")


def load_project(path: Path) -> ProjectBinding:
    project_path = path.expanduser().resolve()
    if project_path.name != "project.toml":
        raise ProjectConfigError(
            f"project binding must be named project.toml: {project_path}"
        )
    try:
        raw = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectConfigError(f"project binding not found: {project_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectConfigError(
            f"cannot read project binding {project_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProjectConfigError("project.toml must contain a table")
    unknown = sorted(
        set(raw)
        - {
            "schema_version",
            "project_id",
            "display_name",
            "home_repo",
            "members",
            "role_presets",
        }
    )
    if unknown:
        raise ProjectConfigError(
            f"project.toml has unknown fields: {', '.join(unknown)}"
        )
    if raw.get("schema_version") != 1:
        raise ProjectConfigError("project.toml schema_version must be 1")
    if "role_presets" in raw:
        _validate_role_presets(raw["role_presets"])
    display_name = _required_text(raw.get("display_name"), "display_name")
    home_repo = _required_text(raw.get("home_repo"), "home_repo")
    if not _REPO_ID.fullmatch(home_repo):
        raise ProjectConfigError(f"project.toml home_repo is invalid: {home_repo!r}")
    raw_members = raw.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise ProjectConfigError(
            "project.toml members must be a non-empty array of tables"
        )
    members = tuple(_member(value, index) for index, value in enumerate(raw_members))
    member_ids = [member.repo_id for member in members]
    if len(member_ids) != len(set(member_ids)):
        raise ProjectConfigError("project.toml member repo_id values must be unique")
    if home_repo not in member_ids:
        raise ProjectConfigError("project.toml home_repo must appear in members")
    if not any(member.backlog for member in members):
        raise ProjectConfigError("project.toml must select at least one backlog member")
    return ProjectBinding(
        path=project_path,
        project_id=_project_id(raw.get("project_id")),
        display_name=display_name,
        home_repo=home_repo,
        members=members,
    )


def _context_project_path(path: Path) -> Path | None:
    context_path = path / "project.context.json"
    if not context_path.is_file():
        return None
    try:
        raw = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectConfigError(
            f"cannot read resolved project context {context_path}: {exc}"
        ) from exc
    canonical = raw.get("canonical_project") if isinstance(raw, dict) else None
    if not isinstance(canonical, str) or not canonical:
        raise ProjectConfigError(
            f"resolved project context {context_path} has no canonical_project"
        )
    return Path(canonical)


def resolve_project_path(value: str | Path, *, cwd: Path | None = None) -> Path:
    """Resolve a file, directory, or materialized-folder location to project.toml."""
    supplied = Path(value).expanduser()
    start = supplied if supplied.is_absolute() else (cwd or Path.cwd()) / supplied
    start = start.resolve()
    if start.is_file():
        return start
    current = start.parent if start.name == "project.toml" else start
    for directory in (current, *current.parents):
        direct = directory / "project.toml"
        if direct.is_file():
            return direct
        from_context = _context_project_path(directory)
        if from_context is not None:
            return from_context.expanduser().resolve()
    raise ProjectConfigError(f"no project.toml found from {start}")
