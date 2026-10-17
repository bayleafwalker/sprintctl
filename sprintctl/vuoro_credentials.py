"""Resolves ``file:`` credential references for served-mode sprintctl.

Deliberately independent of ``vuoro_client`` so it can be imported and unit
tested without the ``served`` extra installed. Implements the exact contract
in ``docs/runbooks/vuoro-workstation-cutover.md``: expand ``~/`` for the
effective user only, require a regular mode-0600 file owned by that user,
strip exactly one trailing newline, reject empty content, and never render
the reference or its contents in an error.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

_FILE_PREFIX = "file:"
_DELEGATED_TOKEN = re.compile(rb"vrd_[A-Z2-7]{52}\Z")
_HANDOFF_NAMES = frozenset({"profile.json", "credential"})


class CredentialResolutionError(ValueError):
    pass


def _access_writable(path: Path) -> bool:
    """Use effective-ID/ACL-aware access checks where the platform supports it."""
    try:
        return os.access(path, os.W_OK, effective_ids=True, follow_symlinks=False)
    except (TypeError, NotImplementedError):
        return os.access(path, os.W_OK, follow_symlinks=False)


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )


def read_delegated_handoff_file(cwd: Path, name: str, *, maximum_bytes: int) -> bytes:
    """Read one protected file under the exact-CWD ``.vuoro`` handoff.

    The adapter owns a setgid/sticky ``03770`` workroot, its ``0750``
    ``.vuoro`` directory and both files. The caller can traverse/read the
    handoff but cannot replace or modify it. The exact mode allowlist prevents
    a different, currently-unmatched group from gaining write access later.
    Directory-relative no-follow opens and pre/post inode checks
    keep symlinks, FIFOs and replacement races out of the read path.
    """
    if name not in _HANDOFF_NAMES or maximum_bytes < 1:
        raise CredentialResolutionError("Error: trusted delegated handoff is invalid.")
    root_fd = directory_fd = file_fd = None
    try:
        root_path = cwd.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(root_path, flags)
        root = os.fstat(root_fd)
        caller = os.geteuid()
        directory_fd = os.open(".vuoro", flags, dir_fd=root_fd)
        directory = os.fstat(directory_fd)
        directory_path = root_path / ".vuoro"
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        file_path = directory_path / name
        if (
            not stat.S_ISDIR(root.st_mode)
            or root.st_uid == caller
            or stat.S_IMODE(root.st_mode) != 0o3770
            or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != root.st_uid
            or directory.st_uid == caller
            or directory.st_gid != root.st_gid
            or stat.S_IMODE(directory.st_mode) != 0o750
            or _access_writable(directory_path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != directory.st_uid
            or before.st_gid != directory.st_gid
            or before.st_uid == caller
            or _access_writable(file_path)
            or (
                name == "profile.json"
                and stat.S_IMODE(before.st_mode) not in {0o640, 0o644}
            )
            or (name == "credential" and stat.S_IMODE(before.st_mode) != 0o640)
            or before.st_size > maximum_bytes
        ):
            raise CredentialResolutionError("Error: trusted delegated handoff is invalid.")

        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(file_fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        named_directory = os.stat(".vuoro", dir_fd=root_fd, follow_symlinks=False)
        if (
            len(data) > maximum_bytes
            or len(data) != before.st_size
            or _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(named)
            or _stat_identity(directory) != _stat_identity(named_directory)
        ):
            raise CredentialResolutionError("Error: trusted delegated handoff is invalid.")
        return data
    except FileNotFoundError:
        raise
    except CredentialResolutionError:
        raise
    except OSError as exc:
        raise CredentialResolutionError("Error: trusted delegated handoff is unavailable.") from exc
    finally:
        for fd in (file_fd, directory_fd, root_fd):
            if fd is not None:
                os.close(fd)


def resolve_delegated_profile_credential(profile: object, ref: str) -> str:
    """Read the fixed sibling delegated token for a trusted implicit profile."""
    source_path = getattr(profile, "source_path", None)
    if not getattr(profile, "trusted_implicit_delegated", False) or not isinstance(source_path, Path):
        raise CredentialResolutionError("Error: trusted delegated credential is unavailable.")
    if source_path.name != "profile.json" or source_path.parent.name != ".vuoro":
        raise CredentialResolutionError("Error: trusted delegated credential is unavailable.")
    try:
        root = source_path.parent.parent.resolve(strict=True)
        expected_profile = root / ".vuoro" / "profile.json"
        expected_ref = f"file:{(root / '.vuoro' / 'credential').absolute()}"
        if source_path != expected_profile or ref != expected_ref:
            raise CredentialResolutionError("Error: trusted delegated credential is unavailable.")
        profile_raw = read_delegated_handoff_file(root, "profile.json", maximum_bytes=1_048_576)
        profile_json = json.loads(profile_raw.decode("utf-8"))
        if not isinstance(profile_json, dict) or profile_json.get("credential_ref") != expected_ref:
            raise CredentialResolutionError("Error: trusted delegated credential is unavailable.")
        raw = read_delegated_handoff_file(root, "credential", maximum_bytes=128)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CredentialResolutionError) as exc:
        raise CredentialResolutionError("Error: trusted delegated credential is unavailable.") from exc
    if _DELEGATED_TOKEN.fullmatch(raw) is None:
        raise CredentialResolutionError("Error: trusted delegated credential is invalid.")
    return raw.decode("ascii")


def resolve_file_credential(ref: str) -> str:
    if not ref.startswith(_FILE_PREFIX):
        raise CredentialResolutionError(
            "Error: unsupported credential reference scheme; expected 'file:'."
        )

    raw_path = ref[len(_FILE_PREFIX):]
    if raw_path.startswith("~/"):
        path = Path.home() / raw_path[2:]
    elif raw_path.startswith("/"):
        path = Path(raw_path)
    else:
        raise CredentialResolutionError(
            "Error: credential file reference must be absolute or start with '~/'."
        )

    try:
        # lstat, not stat: a symlink must be rejected as itself, not resolved
        # and checked at its target (avoids a swapped-target race).
        file_stat = path.lstat()
    except OSError as exc:
        raise CredentialResolutionError(
            "Error: cannot stat the configured credential file."
        ) from exc

    if stat.S_ISLNK(file_stat.st_mode):
        raise CredentialResolutionError(
            "Error: the configured credential file must not be a symlink."
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise CredentialResolutionError(
            "Error: the configured credential file must be a regular file."
        )
    if file_stat.st_uid != os.geteuid():
        raise CredentialResolutionError(
            "Error: the configured credential file is not owned by the current user."
        )
    mode_bits = stat.S_IMODE(file_stat.st_mode)
    if mode_bits != 0o600:
        raise CredentialResolutionError(
            "Error: the configured credential file must have mode 0600."
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CredentialResolutionError(
            "Error: cannot read the configured credential file."
        ) from exc

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw:
        raise CredentialResolutionError(
            "Error: the configured credential file is empty."
        )

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialResolutionError(
            "Error: the configured credential file is not valid UTF-8."
        ) from exc


__all__ = [
    "CredentialResolutionError",
    "read_delegated_handoff_file",
    "resolve_delegated_profile_credential",
    "resolve_file_credential",
]
