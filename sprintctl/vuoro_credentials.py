"""Resolves ``file:`` credential references for served-mode sprintctl.

Deliberately independent of ``vuoro_client`` so it can be imported and unit
tested without the ``served`` extra installed. Implements the exact contract
in ``docs/runbooks/vuoro-workstation-cutover.md``: expand ``~/`` for the
effective user only, require a regular mode-0600 file owned by that user,
strip exactly one trailing newline, reject empty content, and never render
the reference or its contents in an error.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_FILE_PREFIX = "file:"


class CredentialResolutionError(ValueError):
    pass


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


__all__ = ["CredentialResolutionError", "resolve_file_credential"]
