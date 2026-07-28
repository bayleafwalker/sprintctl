"""Per-repository configuration for authority-command rollout.

The configuration is fixed below ``.sprintctl`` and defaults to ``off``.
This module selects rollout behavior only; it does not arbitrate or apply any
authority command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping
from uuid import UUID

from . import backend


AUTHORITY_COMMAND_CONFIG_VERSION = 1
_STATE_DIRECTORY_NAME = ".sprintctl"
_CONFIG_FILENAME = "authority-command.json"
_OUTBOX_FILENAME = "authority-command-outbox.db"
_CREDENTIAL_DIRECTORY_NAME = "authority-credentials"
_TERMINAL_DIRECTORY_NAME = "authority-terminal-decisions"


class AuthorityCommandConfigError(ValueError):
    """The repository authority-command configuration is malformed or unsafe."""


class AuthorityCommandMode(StrEnum):
    """How authority commands participate in the rollout."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


@dataclass(frozen=True, slots=True)
class AuthorityCommandPaths:
    repo_root: Path
    state_dir: Path
    config_path: Path
    outbox_path: Path
    credential_dir: Path
    terminal_dir: Path


@dataclass(frozen=True, slots=True)
class AuthorityCommandConfig:
    mode: AuthorityCommandMode | str = AuthorityCommandMode.OFF
    version: int = AUTHORITY_COMMAND_CONFIG_VERSION

    def __post_init__(self) -> None:
        try:
            mode = AuthorityCommandMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise AuthorityCommandConfigError(
                "authority command mode must be off, shadow, or enforce"
            ) from exc
        if self.version != AUTHORITY_COMMAND_CONFIG_VERSION:
            raise AuthorityCommandConfigError(
                "unsupported authority command config version "
                f"{self.version!r}; expected {AUTHORITY_COMMAND_CONFIG_VERSION}"
            )
        object.__setattr__(self, "mode", mode)

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "mode": self.mode.value}


@dataclass(frozen=True, slots=True)
class AuthorityCommandStatus:
    config: AuthorityCommandConfig
    configured: bool
    paths: AuthorityCommandPaths

    @property
    def mode(self) -> AuthorityCommandMode:
        return self.config.mode

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "configured": self.configured,
            "repo_root": str(self.paths.repo_root),
            "config_path": str(self.paths.config_path),
            "outbox_path": str(self.paths.outbox_path),
        }


@dataclass(frozen=True, slots=True)
class PendingAuthorityCredential:
    """Transient proofs retained locally for retry and new-proof recovery."""

    event_id: str
    credentials: Mapping[str, str]
    recovery_credential_ref: str | None = None

    @property
    def credential_ref(self) -> str:
        if self.recovery_credential_ref is not None:
            return self.recovery_credential_ref
        if len(self.credentials) == 1:
            return next(iter(self.credentials))
        raise AuthorityCommandConfigError(
            "authority command has multiple proofs and no recovery proof"
        )

    @property
    def secret(self) -> str:
        return self.credentials[self.credential_ref]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validated_repo_root(repo_root: Path) -> Path:
    resolved = repo_root.resolve()
    if not resolved.is_dir():
        raise AuthorityCommandConfigError(
            f"authority command repo root is not a directory: {repo_root}"
        )
    return resolved


def _validated_paths(repo_root: Path) -> AuthorityCommandPaths:
    root = _validated_repo_root(repo_root)
    state_dir = root / _STATE_DIRECTORY_NAME
    resolved_state = state_dir.resolve()
    if not _is_within(resolved_state, root):
        raise AuthorityCommandConfigError(
            f"authority command state directory must remain under {root}: {state_dir}"
        )
    paths = AuthorityCommandPaths(
        repo_root=root,
        state_dir=state_dir,
        config_path=state_dir / _CONFIG_FILENAME,
        outbox_path=state_dir / _OUTBOX_FILENAME,
        credential_dir=state_dir / _CREDENTIAL_DIRECTORY_NAME,
        terminal_dir=state_dir / _TERMINAL_DIRECTORY_NAME,
    )
    for path in (paths.config_path, paths.outbox_path, paths.credential_dir, paths.terminal_dir):
        if not _is_within(path.resolve(), resolved_state):
            raise AuthorityCommandConfigError(
                f"authority command path must remain under {state_dir}: {path}"
            )
    return paths


def authority_command_paths(
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
) -> AuthorityCommandPaths:
    if repo_root is None:
        resolved_root, _repo_id, _marker = backend.resolve_repo_identity(cwd)
        if resolved_root is None:
            raise AuthorityCommandConfigError(
                "cannot resolve a repository for authority command configuration"
            )
        repo_root = resolved_root
    return _validated_paths(repo_root)


def _parse_config(path: Path) -> AuthorityCommandConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityCommandConfigError(
            f"invalid authority command config {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise AuthorityCommandConfigError(
            f"invalid authority command config {path}: expected an object"
        )
    expected = {"version", "mode"}
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise AuthorityCommandConfigError(
            f"invalid authority command config {path}: " + "; ".join(details)
        )
    return AuthorityCommandConfig(mode=raw["mode"], version=raw["version"])


def load_authority_command_config(paths: AuthorityCommandPaths) -> AuthorityCommandConfig:
    expected = _validated_paths(paths.repo_root)
    if paths != expected:
        raise AuthorityCommandConfigError(
            "authority command paths must be derived from the repo root"
        )
    if not paths.config_path.exists():
        return AuthorityCommandConfig()
    if not paths.config_path.is_file():
        raise AuthorityCommandConfigError(
            f"authority command config is not a regular file: {paths.config_path}"
        )
    return _parse_config(paths.config_path)


def authority_command_status(
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
) -> AuthorityCommandStatus:
    paths = authority_command_paths(cwd=cwd, repo_root=repo_root)
    return AuthorityCommandStatus(
        config=load_authority_command_config(paths),
        configured=paths.config_path.exists(),
        paths=paths,
    )


def set_authority_command_mode(
    mode: AuthorityCommandMode | str,
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
) -> AuthorityCommandStatus:
    config = AuthorityCommandConfig(mode=mode)
    paths = authority_command_paths(cwd=cwd, repo_root=repo_root)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = _validated_paths(paths.repo_root)
    payload = json.dumps(config.to_dict(), sort_keys=True, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{_CONFIG_FILENAME}.",
        dir=paths.state_dir,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, paths.config_path)
        directory_fd = os.open(paths.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return authority_command_status(repo_root=paths.repo_root)


def _canonical_event_id(event_id: str | UUID) -> str:
    try:
        canonical = str(UUID(str(event_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuthorityCommandConfigError(
            "authority credential event_id must be a UUID"
        ) from exc
    if str(event_id) != canonical:
        raise AuthorityCommandConfigError(
            "authority credential event_id must be a canonical UUID"
        )
    return canonical


def _credential_ref(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise AuthorityCommandConfigError(
            "authority credential secret must be a non-empty string"
        )
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _credential_path(paths: AuthorityCommandPaths, event_id: str | UUID) -> Path:
    expected = _validated_paths(paths.repo_root)
    if paths != expected:
        raise AuthorityCommandConfigError(
            "authority command paths must be derived from the repo root"
        )
    canonical_event_id = _canonical_event_id(event_id)
    path = paths.credential_dir / f"{canonical_event_id}.json"
    if not _is_within(path.resolve(), paths.credential_dir.resolve()):
        raise AuthorityCommandConfigError(
            "authority credential path must remain under its fixed directory"
        )
    return path


def _terminal_path(paths: AuthorityCommandPaths, event_id: str | UUID) -> Path:
    expected = _validated_paths(paths.repo_root)
    if paths != expected:
        raise AuthorityCommandConfigError("authority command paths must be derived from the repo root")
    canonical_event_id = _canonical_event_id(event_id)
    path = paths.terminal_dir / f"{canonical_event_id}.json"
    if not _is_within(path.resolve(), paths.terminal_dir.resolve()):
        raise AuthorityCommandConfigError("authority terminal path must remain under its fixed directory")
    return path


def _require_private(path: Path, *, directory: bool) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AuthorityCommandConfigError(
            f"cannot inspect authority credential path {path}: {exc}"
        ) from exc
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        kind = "directory" if directory else "file"
        raise AuthorityCommandConfigError(
            f"authority credential path is not a regular {kind}: {path}"
        )
    expected_mode = 0o700 if directory else 0o600
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        raise AuthorityCommandConfigError(
            f"authority credential path has unsafe permissions {actual_mode:04o}; "
            f"expected {expected_mode:04o}: {path}"
        )


def _prepare_credential_directory(paths: AuthorityCommandPaths) -> None:
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.credential_dir.mkdir(mode=0o700, exist_ok=True)
    _require_private(paths.credential_dir, directory=True)


def store_pending_authority_credential(
    paths: AuthorityCommandPaths,
    *,
    event_id: str | UUID,
    credential_ref: str,
    secret: str,
) -> PendingAuthorityCredential:
    """Atomically persist one proof before sending its authority command."""
    return store_pending_authority_credentials(
        paths,
        event_id=event_id,
        credentials={credential_ref: secret},
        recovery_credential_ref=credential_ref,
    )


def store_pending_authority_credentials(
    paths: AuthorityCommandPaths,
    *,
    event_id: str | UUID,
    credentials: Mapping[str, str],
    recovery_credential_ref: str | None = None,
) -> PendingAuthorityCredential:
    """Atomically persist all transient proofs required to retry one command."""
    path = _credential_path(paths, event_id)
    canonical_event_id = _canonical_event_id(event_id)
    canonical_credentials: dict[str, str] = {}
    for credential_ref, secret in sorted(credentials.items()):
        if credential_ref != _credential_ref(secret):
            raise AuthorityCommandConfigError(
                "authority credential_ref does not match the local secret digest"
            )
        canonical_credentials[credential_ref] = secret
    if not canonical_credentials:
        raise AuthorityCommandConfigError("authority command credentials must not be empty")
    if recovery_credential_ref is not None and recovery_credential_ref not in canonical_credentials:
        raise AuthorityCommandConfigError(
            "authority recovery credential_ref must identify a stored proof"
        )
    _prepare_credential_directory(paths)
    existing = load_pending_authority_credential(paths, event_id=canonical_event_id)
    requested = PendingAuthorityCredential(
        canonical_event_id,
        canonical_credentials,
        recovery_credential_ref,
    )
    if existing is not None:
        if existing != requested:
            raise AuthorityCommandConfigError(
                "authority credential event_id already has different proof material"
            )
        return existing
    payload = json.dumps(
        {
            "event_id": canonical_event_id,
            "credentials": canonical_credentials,
            "recovery_credential_ref": recovery_credential_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{canonical_event_id}.",
        dir=paths.credential_dir,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        directory_fd = os.open(paths.credential_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return requested


def load_pending_authority_credential(
    paths: AuthorityCommandPaths,
    *,
    event_id: str | UUID,
) -> PendingAuthorityCredential | None:
    """Recover and locally verify proof material after process restart."""
    path = _credential_path(paths, event_id)
    canonical_event_id = _canonical_event_id(event_id)
    if not path.exists():
        return None
    _require_private(paths.credential_dir, directory=True)
    _require_private(path, directory=False)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityCommandConfigError(
            f"invalid authority credential sidecar {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise AuthorityCommandConfigError(
            f"invalid authority credential sidecar shape: {path}"
        )
    if set(raw) == {"event_id", "credential_ref", "secret"}:
        raw = {
            "event_id": raw["event_id"],
            "credentials": {raw["credential_ref"]: raw["secret"]},
            "recovery_credential_ref": raw["credential_ref"],
        }
    if set(raw) != {"event_id", "credentials", "recovery_credential_ref"}:
        raise AuthorityCommandConfigError(
            f"invalid authority credential sidecar shape: {path}"
        )
    if raw["event_id"] != canonical_event_id:
        raise AuthorityCommandConfigError(
            f"authority credential sidecar event_id does not match its filename: {path}"
        )
    if not isinstance(raw["credentials"], dict) or not raw["credentials"]:
        raise AuthorityCommandConfigError(
            f"invalid authority credential sidecar credentials: {path}"
        )
    canonical_credentials: dict[str, str] = {}
    for credential_ref, secret in sorted(raw["credentials"].items()):
        if credential_ref != _credential_ref(secret):
            raise AuthorityCommandConfigError(
                f"authority credential sidecar digest mismatch: {path}"
            )
        canonical_credentials[credential_ref] = secret
    recovery_ref = raw["recovery_credential_ref"]
    if recovery_ref is not None and recovery_ref not in canonical_credentials:
        raise AuthorityCommandConfigError(
            f"authority credential sidecar recovery ref is missing: {path}"
        )
    return PendingAuthorityCredential(
        event_id=canonical_event_id,
        credentials=canonical_credentials,
        recovery_credential_ref=recovery_ref,
    )


def remove_pending_authority_credential(
    paths: AuthorityCommandPaths,
    *,
    event_id: str | UUID,
) -> bool:
    """Remove locally persisted proof after the command is promoted."""
    path = _credential_path(paths, event_id)
    if not path.exists():
        return False
    _require_private(paths.credential_dir, directory=True)
    _require_private(path, directory=False)
    path.unlink()
    directory_fd = os.open(paths.credential_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def mark_terminal_authority_decision(
    paths: AuthorityCommandPaths, *, event_id: str | UUID, outcome: str,
    served_decision: Mapping[str, object] | None = None,
    quarantine_reason: str | None = None,
) -> None:
    """Durably acknowledge a direct terminal response without altering outbox.

    The producer log is immutable.  This local receipt lets sync skip a
    command that was already conclusively accepted or rejected, while an
    unknown transport outcome has no receipt and remains replayable.
    """
    if outcome not in {
        "accepted", "rejected", "absent-from-served-ledger", "quarantined-divergent-stream",
    }:
        raise AuthorityCommandConfigError(
            "terminal authority outcome must be accepted, rejected, absent-from-served-ledger, "
            "or quarantined-divergent-stream"
        )
    if outcome == "absent-from-served-ledger" and served_decision is not None:
        raise AuthorityCommandConfigError("an absent served record cannot carry a served decision")
    if outcome == "quarantined-divergent-stream":
        if not isinstance(quarantine_reason, str) or not quarantine_reason.strip():
            raise AuthorityCommandConfigError("a quarantined stream requires a non-empty reason")
        if served_decision is not None:
            raise AuthorityCommandConfigError("a quarantined stream cannot carry a served decision")
    elif quarantine_reason is not None:
        raise AuthorityCommandConfigError("only a quarantined stream may carry a quarantine reason")
    path = _terminal_path(paths, event_id)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.terminal_dir.mkdir(mode=0o700, exist_ok=True)
    _require_private(paths.terminal_dir, directory=True)
    payload_value: dict[str, object] = {
        "event_id": _canonical_event_id(event_id), "outcome": outcome,
    }
    if served_decision is not None:
        # Durable local recovery evidence, never an authority decision of its
        # own.  The served decision remains authoritative.
        payload_value["served_decision"] = dict(served_decision)
    if quarantine_reason is not None:
        payload_value["quarantine_reason"] = quarantine_reason.strip()
    payload = json.dumps(payload_value, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=paths.terminal_dir, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(paths.terminal_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def archive_terminal_authority_outbox(
    paths: AuthorityCommandPaths, *, origin_stream_id: str, reason: str,
) -> Path:
    """Archive a fully-terminal authority outbox before starting a new stream.

    The immutable SQLite database is retained under ``.sprintctl``.  This
    function never edits producer records or served state; callers must first
    prove every authority command has a terminal local receipt.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise AuthorityCommandConfigError("authority stream rollover requires a non-empty reason")
    canonical_stream = _canonical_event_id(origin_stream_id)
    source = paths.outbox_path
    if not source.exists():
        raise AuthorityCommandConfigError("authority outbox does not exist")
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = paths.state_dir / f"authority-command-outbox.{canonical_stream}.quarantined.db"
    if archive.exists():
        raise AuthorityCommandConfigError(f"authority outbox archive already exists: {archive}")
    os.replace(source, archive)
    os.chmod(archive, 0o600)
    manifest = paths.state_dir / f"authority-command-outbox.{canonical_stream}.rollover.json"
    payload = json.dumps({
        "origin_stream_id": canonical_stream,
        "archived_outbox": archive.name,
        "reason": reason.strip(),
    }, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{manifest.name}.", dir=paths.state_dir, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, manifest)
        directory_fd = os.open(paths.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return archive


def is_terminal_authority_decision(paths: AuthorityCommandPaths, *, event_id: str | UUID) -> bool:
    path = _terminal_path(paths, event_id)
    if not path.exists():
        return False
    _require_private(paths.terminal_dir, directory=True)
    _require_private(path, directory=False)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityCommandConfigError(f"invalid authority terminal receipt {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("event_id") != _canonical_event_id(event_id):
        raise AuthorityCommandConfigError(f"invalid authority terminal receipt {path}")
    outcome = value.get("outcome")
    if outcome not in {"accepted", "rejected", "absent-from-served-ledger", "quarantined-divergent-stream"}:
        raise AuthorityCommandConfigError(f"invalid authority terminal receipt {path}")
    if outcome == "quarantined-divergent-stream":
        if not isinstance(value.get("quarantine_reason"), str) or not value["quarantine_reason"].strip():
            raise AuthorityCommandConfigError(f"invalid authority terminal receipt {path}")
    elif "quarantine_reason" in value:
        raise AuthorityCommandConfigError(f"invalid authority terminal receipt {path}")
    return True


__all__ = [
    "AUTHORITY_COMMAND_CONFIG_VERSION",
    "AuthorityCommandConfig",
    "AuthorityCommandConfigError",
    "AuthorityCommandMode",
    "AuthorityCommandPaths",
    "AuthorityCommandStatus",
    "PendingAuthorityCredential",
    "authority_command_paths",
    "authority_command_status",
    "archive_terminal_authority_outbox",
    "load_authority_command_config",
    "load_pending_authority_credential",
    "is_terminal_authority_decision",
    "mark_terminal_authority_decision",
    "remove_pending_authority_credential",
    "set_authority_command_mode",
    "store_pending_authority_credential",
    "store_pending_authority_credentials",
]
