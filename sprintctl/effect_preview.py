"""Static, redacted declaration reporting; never an effect validation gate.

Git's numstat parser runs outside a repository, with no inherited Git config
or credentials. It parses stdin and applies nothing. The reconciler still
owns applicability, staged-content checks and actual execution.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
from typing import Any, Mapping

from .effect_intent import MAX_UNIFIED_DIFF, canonical_intent_digest

SCHEMA = "sprintctl-declared-effect-preview/v1"
MAX_PATHS = 200


class PreviewBindingError(ValueError):
    pass


def _safe_path(path: str) -> bool:
    return (bool(path) and path.isprintable() and not path.startswith(("/", "-", '"'))
            and ":" not in path and "\\" not in path
            and not any(p in ("", ".", "..", ".git") for p in path.split("/")))


def _declarations(diff: str) -> tuple[str, list[dict[str, Any]]]:
    if len(diff) > MAX_UNIFIED_DIFF or "\x00" in diff:
        return "unsupported", []
    # No repository, global/system configuration, credential or hook execution.
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull}
    try:
        with tempfile.TemporaryDirectory(prefix="sprintctl-preview-", dir="/tmp") as cwd:
            env["GIT_CEILING_DIRECTORIES"] = cwd  # never discover an ancestor repo
            result = subprocess.run(["git", "apply", "--numstat", "-z", "-"],
                                    input=diff.encode("utf-8"), capture_output=True,
                                    cwd=cwd, env=env, timeout=5, check=False)
        if result.returncode:
            return "unsupported", []
        rows: dict[str, dict[str, Any]] = {}
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            added, removed, raw_path = record.split(b"\t", 2)
            path = raw_path.decode("utf-8")
            if not _safe_path(path) or not added.isdigit() or not removed.isdigit():
                return "unsupported", []
            row = rows.setdefault(path, {"path": path, "added_lines": 0,
                                         "removed_lines": 0, "source_only": False})
            row["added_lines"] += int(added)
            row["removed_lines"] += int(removed)
        # numstat omits rename/copy sources. Require a complete extended-header
        # pair associated with this exact Git file header and a destination
        # numstat actually parsed. Preamble/trailing text is not a declaration.
        sources = []
        header_paths = None
        metadata = {}
        in_headers = False
        def finish_header():
            if not metadata:
                return True
            for kind in ("rename", "copy"):
                source = metadata.get(kind + " from")
                target = metadata.get(kind + " to")
                if source is not None or target is not None:
                    if (header_paths is None or (source, target) != header_paths
                            or target not in rows or not _safe_path(source)):
                        return False
                    sources.append(source)
            return True
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                if not finish_header():
                    return "unsupported", []
                metadata = {}
                fields = shlex.split(line)
                header_paths = ((fields[2][2:], fields[3][2:]) if len(fields) == 4
                                and fields[2].startswith("a/") and fields[3].startswith("b/") else None)
                in_headers = True
            elif line.startswith(("--- ", "@@")):
                in_headers = False
            for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
                if line.startswith(prefix):
                    key = prefix.rstrip()
                    if not in_headers or header_paths is None or key in metadata:
                        return "unsupported", []
                    metadata[key] = line[len(prefix):]
        if not finish_header():
            return "unsupported", []
        for path in sources:
            rows.setdefault(path, {"path": path, "added_lines": None,
                                   "removed_lines": None, "source_only": True})
        return ("parsed" if rows else "unsupported"), [rows[p] for p in sorted(rows)]
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        # Neither attacker-controlled patch bytes nor Git diagnostics escape.
        return "unavailable", []


def preview_intent(intent: Mapping[str, Any], *, disclose_paths: bool = False,
                   duplicate_candidates: list[Mapping[str, Any]] | None = None,
                   candidates_complete: bool = False) -> dict[str, Any]:
    """Bind a static report to owner bytes; supplied candidates are authorized reads."""
    if canonical_intent_digest(intent) != intent["canonical_intent_digest"]:
        raise PreviewBindingError("effect preview content digest mismatch")
    status, paths = _declarations(intent["unified_diff"])
    duplicates = []
    if duplicate_candidates is not None:
        for other in duplicate_candidates:
            if (other["intent_id"] != intent["intent_id"] and
                    all(other.get(k) == intent.get(k) for k in
                        ("item_id", "repository", "release_digest", "canonical_intent_digest"))):
                if canonical_intent_digest(other) != other["canonical_intent_digest"]:
                    raise PreviewBindingError("effect preview candidate digest mismatch")
                duplicates.append(other["intent_id"])
    return {
        "schema": SCHEMA,
        "basis": {k: intent.get(k) for k in ("intent_id", "revision", "canonical_intent_digest",
                    "state", "item_id", "repository", "base_commit", "release_digest")},
        "unified_diff_sha256": hashlib.sha256(intent["unified_diff"].encode("utf-8")).hexdigest(),
        "declared_changes": {"status": status, "path_count": len(paths) if status == "parsed" else None,
            "paths": paths[:MAX_PATHS] if disclose_paths else [],
            "redacted": not disclose_paths, "truncated": disclose_paths and len(paths) > MAX_PATHS},
        "resources": {"status": "not-declared"},
        "protected_policy": {"status": "unknown"},
        "external_consequences": {"status": "unknown"},
        "duplicate_relation": {"status": "observed" if duplicate_candidates is not None else "unknown",
            "candidate_intent_ids": sorted(set(duplicates)), "complete": candidates_complete,
            "scope": "authorized proposed intents, same repository/item/release/content"},
        "receipt_source": {"operation": "work.effect.get-v1", "acceptance": "observed" if intent.get("acceptance") else "missing",
                           "application": "observed" if intent.get("application") else "missing"},
        "authorization": "none; owner acceptance and execution checks remain required",
    }
