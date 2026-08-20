"""Small command adapter for the Sprintctl work-item context pilot.

Projection hooks fail open.  A recognized item-status mutation precheck fails
closed when validation is unavailable.  Neither path performs a mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import backend, served


_FULL_EVENTS = frozenset({"SessionStart", "SubagentStart"})
_DELTA_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse"})
_RECOGNIZED_STATUS_TOOLS = frozenset(
    {"mcp__sprintctl__item_status", "sprintctl.item_status"}
)


class Cursor(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, revision: str) -> None: ...


class MemoryCursor:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def put(self, key: str, revision: str) -> None:
        self.values[key] = revision


class FileCursor:
    """Disposable, per-consumer revision file; never an authority."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.revision"

    def _safe_root(self, *, create: bool) -> bool:
        try:
            if create:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.root.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not self.root.is_symlink()
            and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o077 == 0
        )

    def get(self, key: str) -> str | None:
        if not self._safe_root(create=False):
            return None
        try:
            path = self._path(key)
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 != 0
            ):
                return None
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def put(self, key: str, revision: str) -> None:
        try:
            if not self._safe_root(create=True):
                return
            path = self._path(key)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(revision + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError:
            # Cursor loss may duplicate a later projection; it cannot authorize.
            return


def _additional_context(event: str, context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


def _denied(reason: str, context: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if context:
        output["additionalContext"] = context
    return {"hookSpecificOutput": output}


def _context(projection: dict[str, Any]) -> str:
    return json.dumps(
        {"volatile_context": projection},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class VolatileContextHookAdapter:
    def __init__(
        self,
        *,
        repo_id: str,
        item_id: int,
        project: Callable[[int], dict[str, Any]],
        validate: Callable[[int, str | None], dict[str, Any]],
        cursor: Cursor,
    ) -> None:
        self.repo_id = repo_id
        self.item_id = item_id
        self.project = project
        self.validate = validate
        self.cursor = cursor

    def _cursor_key(self, event: Mapping[str, Any]) -> str:
        return ":".join(
            (
                self.repo_id,
                str(self.item_id),
                str(event.get("harness") or "native"),
                str(event.get("session_id") or "unknown-session"),
                str(event.get("agent_id") or "root"),
            )
        )

    def _projection(self, event: Mapping[str, Any], *, full: bool) -> dict[str, Any]:
        try:
            response = self.project(self.item_id)
            projection = response["projection"]
            revision = projection["revision"]
            key = self._cursor_key(event)
            if not full and self.cursor.get(key) == revision:
                return {}
            self.cursor.put(key, revision)
            return _additional_context(
                str(event["hook_event_name"]), _context(projection)
            )
        except Exception:
            return {}  # context enrichment is non-blocking by contract

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        name = event.get("hook_event_name")
        if name in _FULL_EVENTS:
            return self._projection(event, full=True)
        if name in _DELTA_EVENTS:
            return self._projection(event, full=False)
        if name != "PreToolUse":
            return {}

        tool_name = event.get("tool_name")
        if tool_name not in _RECOGNIZED_STATUS_TOOLS:
            return {}
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, Mapping):
            return _denied("recognized Sprintctl status mutation has no structured input")
        item_id = tool_input.get("item_id")
        if item_id != self.item_id:
            return _denied(
                "recognized Sprintctl status mutation does not match the bound item"
            )
        expected_revision = tool_input.get("expected_revision")
        if expected_revision is not None and not isinstance(expected_revision, str):
            expected_revision = None
        try:
            result = self.validate(self.item_id, expected_revision)
        except Exception:
            return _denied("Sprintctl mutation precheck is unavailable")
        if result["allowed"]:
            return {}
        return _denied(result["reason"], _context(result["projection"]))


def _adapter(environ: Mapping[str, str], cwd: Path) -> VolatileContextHookAdapter:
    item_id = int(environ["SPRINTCTL_CONTEXT_ITEM_ID"])
    config = backend.load_backend_config(cwd=cwd, environ=environ)
    if config.mode != "served" or config.served_profile is None or config.repo_id is None:
        raise ValueError("volatile context pilot requires a resolved served backend")
    profile = config.served_profile
    repo_id = config.repo_id
    cursor_root = Path(
        environ.get(
            "SPRINTCTL_VOLATILE_CURSOR_DIR",
            f"/tmp/sprintctl-volatile-context-{os.getuid()}",
        )
    )
    return VolatileContextHookAdapter(
        repo_id=repo_id,
        item_id=item_id,
        project=lambda selected: served.read_item_projection(
            profile, repo_id=repo_id, item_id=selected
        ),
        validate=lambda selected, revision: served.validate_item_status_mutation(
            profile,
            repo_id=repo_id,
            item_id=selected,
            expected_revision=revision,
        ),
        cursor=FileCursor(cursor_root),
    )


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    try:
        adapter = _adapter(os.environ, Path(event.get("cwd") or Path.cwd()))
        output = adapter.handle(event)
    except Exception:
        recognized = (
            event.get("hook_event_name") == "PreToolUse"
            and event.get("tool_name") in _RECOGNIZED_STATUS_TOOLS
        )
        output = (
            _denied("Sprintctl mutation precheck is unavailable") if recognized else {}
        )
    if output:
        json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()


__all__ = ["FileCursor", "MemoryCursor", "VolatileContextHookAdapter", "main"]
