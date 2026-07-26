"""Read-only installation provenance and backend capability diagnostics."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping

from . import CLI_CAPABILITIES, __version__
from . import backend as _backend
from . import served as _served
from .db import CURRENT_SCHEMA_VERSION as SQLITE_SCHEMA_VERSION
from .pg_migrations import CURRENT_SCHEMA_VERSION as REMOTE_SCHEMA_VERSION

_SERVED_EXPECTED_OPERATIONS = _served.EXPECTED_OPERATIONS
_VERSION_RE = re.compile(r"\bversion\s+([^\s]+)")
_POSTGRES_CREDENTIAL_RE = re.compile(r"(postgres(?:ql)?://)[^\s@]+@", re.IGNORECASE)

REINSTALL_GUIDANCE = {
    "pipx": "pipx upgrade sprintctl",
    "uv": "uv tool upgrade sprintctl",
    "source": "python -m pip install -e .",
    "remote_pipx": "pipx install --force 'sprintctl[remote]'",
    "remote_uv": "uv tool install --force 'sprintctl[remote]'",
}


def _source_metadata(cwd: Path) -> dict[str, Any]:
    for directory in (cwd.resolve(), *cwd.resolve().parents):
        path = directory / "pyproject.toml"
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                payload = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = payload.get("project", {})
        if project.get("name") != "sprintctl":
            continue
        settings = payload.get("tool", {}).get("sprintctl", {})
        return {
            "present": True,
            "root": str(directory),
            "version": project.get("version"),
            "capabilities": sorted(settings.get("capabilities", [])),
            "schema": {
                "local": settings.get("sqlite-schema-version"),
                "remote": settings.get("remote-schema-version"),
            },
        }
    return {
        "present": False,
        "root": None,
        "version": None,
        "capabilities": [],
        "schema": {"local": None, "remote": None},
    }


def _package_metadata() -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution("sprintctl")
    except importlib.metadata.PackageNotFoundError:
        metadata_version = None
        location = None
    else:
        metadata_version = distribution.version
        location = str(Path(distribution.locate_file("")).resolve())
    return {
        "code_version": __version__,
        "metadata_version": metadata_version,
        "location": location,
        "module_path": str(Path(__file__).resolve().parent),
        "capabilities": sorted(CLI_CAPABILITIES),
    }


def _probe_path_executable() -> dict[str, Any]:
    path = shutil.which("sprintctl")
    if path is None:
        return {"path": None, "version": None, "probe_error": "not found on PATH"}
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"path": path, "version": None, "probe_error": str(exc)}
    output = (result.stdout or result.stderr).strip()
    match = _VERSION_RE.search(output)
    error = None
    if result.returncode != 0:
        error = f"exit {result.returncode}: {output}"
    elif match is None:
        error = f"unrecognized version output: {output}"
    return {
        "path": str(Path(path).resolve()),
        "version": match.group(1) if match else None,
        "probe_error": error,
    }


def _backend_facts(cwd: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    mode = environ.get("SPRINTCTL_BACKEND") or "local"
    facts: dict[str, Any] = {
        "environment_mode": mode,
        "url_configured": bool(environ.get("SPRINTCTL_URL")),
        "database_path": environ.get("SPRINTCTL_DB"),
        "valid": False,
        "resolved_mode": None,
        "repo_root": None,
        "repo_id": None,
        "repo_source": None,
        "marker": None,
        "error": None,
    }
    try:
        config = _backend.load_backend_config(cwd=cwd, environ=environ)
    except _backend.BackendConfigError as exc:
        facts["error"] = str(exc)
        try:
            repo_root, repo_id, marker = _backend.resolve_repo_identity(cwd)
        except _backend.BackendConfigError:
            return facts
        facts["repo_root"] = str(repo_root) if repo_root else None
        env_repo_id = environ.get("SPRINTCTL_REPO_ID")
        facts["repo_id"] = env_repo_id or repo_id
        facts["repo_source"] = (
            "env" if env_repo_id else "marker" if marker and marker.repo_id else "cwd" if repo_id else None
        )
        if marker:
            facts["marker"] = {
                "path": str(marker.path),
                "backend": marker.backend,
                "repo_id": marker.repo_id,
            }
        return facts

    facts.update(
        {
            "valid": True,
            "resolved_mode": config.mode,
            "repo_root": str(config.repo_root) if config.repo_root else None,
            "repo_id": config.repo_id,
            "repo_source": config.repo_source,
            "marker": (
                {
                    "path": str(config.marker.path),
                    "backend": config.marker.backend,
                    "repo_id": config.marker.repo_id,
                }
                if config.marker
                else None
            ),
        }
    )
    if config.mode == "local" and not facts["database_path"]:
        facts["database_path"] = str(Path.home() / ".sprintctl" / "sprintctl.db")
    return facts


def _probe_local_schema(environ: Mapping[str, str]) -> dict[str, Any]:
    configured = environ.get("SPRINTCTL_DB")
    path = Path(configured).expanduser() if configured else Path.home() / ".sprintctl" / "sprintctl.db"
    result: dict[str, Any] = {
        "backend": "local",
        "expected_version": SQLITE_SCHEMA_VERSION,
        "actual_version": None,
        "compatible": None,
        "status": "absent",
        "error": None,
    }
    if not path.is_file():
        return result
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        row = conn.execute("SELECT version FROM schema_version ORDER BY rowid LIMIT 1").fetchone()
        version = int(row[0]) if row is not None else 0
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        result.update({"status": "unavailable", "error": str(exc)})
        return result
    finally:
        if conn is not None:
            conn.close()
    result.update(
        {
            "actual_version": version,
            "compatible": version == SQLITE_SCHEMA_VERSION,
            "status": "current" if version == SQLITE_SCHEMA_VERSION else "mismatch",
        }
    )
    return result


def _probe_remote_schema(environ: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "backend": "remote",
        "expected_version": REMOTE_SCHEMA_VERSION,
        "actual_version": None,
        "compatible": None,
        "status": "unavailable",
        "error": None,
        "has_active_sprint": None,
        "superseded_marker": None,
    }
    if importlib.util.find_spec("psycopg") is None:
        result["error"] = "psycopg is not installed"
        return result
    url = environ.get("SPRINTCTL_URL")
    if not url:
        result["error"] = "SPRINTCTL_URL is not configured"
        return result
    conn = None
    try:
        import psycopg

        conn = psycopg.connect(
            url,
            connect_timeout=3,
            options="-c default_transaction_read_only=on",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cur.fetchone()
            version = int(row[0]) if row is not None else 0
            if version == REMOTE_SCHEMA_VERSION:
                cur.execute("SELECT EXISTS (SELECT 1 FROM sprint WHERE status = 'active')")
                active_row = cur.fetchone()
                result["has_active_sprint"] = bool(active_row[0]) if active_row is not None else False
                cur.execute("SELECT to_regclass('superseded_marker')")
                marker_row = cur.fetchone()
                if marker_row is not None and marker_row[0] is not None:
                    cur.execute("SELECT message FROM superseded_marker LIMIT 1")
                    message_row = cur.fetchone()
                    if message_row is not None and message_row[0] is not None:
                        result["superseded_marker"] = str(message_row[0])
    except Exception as exc:  # driver exceptions vary by psycopg implementation
        message = str(exc).replace(url, "<redacted SPRINTCTL_URL>")
        result["error"] = _POSTGRES_CREDENTIAL_RE.sub(r"\1<redacted>@", message)
        return result
    finally:
        if conn is not None:
            conn.close()
    result.update(
        {
            "actual_version": version,
            "compatible": version == REMOTE_SCHEMA_VERSION,
            "status": "current" if version == REMOTE_SCHEMA_VERSION else "mismatch",
        }
    )
    return result


def _probe_served_backend(
    environ: Mapping[str, str], served_profile: Any | None
) -> dict[str, Any]:
    """Verify the three things served mode needs before any command runs:
    the credential file resolves, the profile parsed (already implied by the
    caller having a ``served_profile``), and the catalog the profile points
    at exposes the operations ``sprintctl.served`` invokes.

    Read-only: never invokes a served operation, only unauthenticated
    catalog discovery.
    """
    result: dict[str, Any] = {
        "backend": "served",
        "expected_version": sorted(_SERVED_EXPECTED_OPERATIONS),
        "actual_version": None,
        "compatible": None,
        "status": "unavailable",
        "error": None,
        "credential_resolved": None,
        "profile": None,
    }
    if served_profile is None:
        result["error"] = "served profile did not parse"
        return result
    result["profile"] = {
        "name": served_profile.name,
        "endpoint": served_profile.endpoint,
        "expected_environment": served_profile.expected_environment,
    }
    if importlib.util.find_spec("vuoro_client") is None:
        result["error"] = "vuoro-client is not installed"
        return result

    from . import vuoro_credentials

    try:
        vuoro_credentials.resolve_file_credential(served_profile.credential_ref)
    except vuoro_credentials.CredentialResolutionError as exc:
        result["credential_resolved"] = False
        result["error"] = str(exc)
        return result
    result["credential_resolved"] = True

    try:
        operations = _served.catalog_operation_names(served_profile)
    except Exception as exc:  # transport, handshake, and schema failures vary by client
        result["error"] = str(exc)
        return result

    actual = sorted(operations)
    result["actual_version"] = actual
    missing = sorted(_SERVED_EXPECTED_OPERATIONS - operations)
    result["compatible"] = not missing
    result["status"] = "current" if not missing else "mismatch"
    if missing:
        result["error"] = "catalog is missing expected operations: " + ", ".join(missing)
    return result


def _finding(code: str, severity: str, message: str, guidance: list[str]) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "guidance": guidance,
    }


def evaluate_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Add deterministic findings to collected facts.

    Kept separate from collection so stale/current installation fixtures can
    exercise capability negotiation without depending on the host machine.
    """
    report = dict(facts)
    findings: list[dict[str, Any]] = []
    provenance = facts["provenance"]
    executable = provenance["executable"]
    package = provenance["package"]
    source = provenance["source"]
    backend = facts["backend"]
    extras = facts["extras"]
    schema = facts["schema"]

    if source["present"] and executable["version"] and executable["version"] != source["version"]:
        findings.append(
            _finding(
                "executable-source-version-mismatch",
                "warning",
                f"PATH sprintctl {executable['version']} differs from source {source['version']}.",
                [REINSTALL_GUIDANCE["pipx"], REINSTALL_GUIDANCE["uv"]],
            )
        )
    if package["metadata_version"] and package["metadata_version"] != package["code_version"]:
        findings.append(
            _finding(
                "package-metadata-version-mismatch",
                "warning",
                f"Imported code {package['code_version']} differs from package metadata {package['metadata_version']}.",
                [REINSTALL_GUIDANCE["source"]],
            )
        )
    if source["present"] and package["code_version"] != source["version"]:
        findings.append(
            _finding(
                "package-source-version-mismatch",
                "warning",
                f"Imported sprintctl {package['code_version']} differs from source {source['version']}.",
                [REINSTALL_GUIDANCE["source"], REINSTALL_GUIDANCE["pipx"], REINSTALL_GUIDANCE["uv"]],
            )
        )

    missing_capabilities = sorted(set(source["capabilities"]) - set(package["capabilities"]))
    if missing_capabilities:
        findings.append(
            _finding(
                "source-capability-mismatch",
                "error",
                "Imported package lacks source capabilities: " + ", ".join(missing_capabilities) + ".",
                [REINSTALL_GUIDANCE["source"], REINSTALL_GUIDANCE["pipx"], REINSTALL_GUIDANCE["uv"]],
            )
        )

    if not backend["valid"]:
        error = backend["error"] or "Backend configuration is invalid."
        if "backend-uncorroborated" in error:
            findings.append(
                _finding(
                    "backend-uncorroborated",
                    "error",
                    error,
                    [
                        "Run from a repository with .sprintctl/backend.json, or pass "
                        "--repo-id together with --allow-markerless-nonlocal for one invocation.",
                    ],
                )
            )
        else:
            findings.append(
                _finding(
                    "backend-config-invalid",
                    "error",
                    error,
                    ["Align SPRINTCTL_BACKEND, SPRINTCTL_URL, and .sprintctl/backend.json."],
                )
            )
    if backend["environment_mode"] == "remote" and not extras["remote"]["enabled"]:
        findings.append(
            _finding(
                "remote-extra-missing",
                "error",
                "Remote mode is configured but psycopg is unavailable.",
                [REINSTALL_GUIDANCE["remote_pipx"], REINSTALL_GUIDANCE["remote_uv"]],
            )
        )
    if backend["environment_mode"] == "served" and not extras.get("served", {}).get("enabled"):
        findings.append(
            _finding(
                "served-extra-missing",
                "error",
                "Served mode is configured but vuoro-client is unavailable.",
                ["Install the 'served' extra: pip install 'sprintctl[served]'."],
            )
        )
    if backend["valid"] and backend["resolved_mode"] == "remote" and schema["status"] == "current":
        if schema.get("has_active_sprint") is False:
            findings.append(
                _finding(
                    "backend-reachable-but-empty",
                    "warning",
                    "Remote backend is reachable but holds no active sprint data (SF2-b).",
                    [
                        "Confirm SPRINTCTL_URL targets the current shared authority; "
                        "doctor performed read-only checks only.",
                    ],
                )
            )
        if schema.get("superseded_marker"):
            findings.append(
                _finding(
                    "backend-superseded",
                    "error",
                    f"Remote backend is superseded: {schema['superseded_marker']} (SF3).",
                    [
                        "Stop using this remote target and switch to the current served authority; "
                        "doctor performed read-only checks only.",
                    ],
                )
            )
    if schema["status"] == "mismatch":
        findings.append(
            _finding(
                "schema-version-mismatch",
                "error",
                f"{schema['backend']} schema {schema['actual_version']} is incompatible with expected {schema['expected_version']}.",
                ["Upgrade the CLI or run the documented migration with an authorized operator; doctor never migrates."],
            )
        )
    elif schema["status"] == "unavailable" and backend["valid"]:
        findings.append(
            _finding(
                "schema-unavailable",
                "error",
                f"Could not read {schema['backend']} schema capability: {schema['error']}.",
                ["Restore read access and rerun sprintctl doctor; no schema changes were attempted."],
            )
        )

    report["findings"] = findings
    severities = {finding["severity"] for finding in findings}
    report["status"] = "error" if "error" in severities else "warning" if findings else "ok"
    report["reinstall_guidance"] = dict(REINSTALL_GUIDANCE)
    return report


def collect_report(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    cwd = (cwd or Path.cwd()).resolve()
    environ = environ if environ is not None else os.environ
    backend = _backend_facts(cwd, environ)
    extras = {
        "remote": {
            "enabled": importlib.util.find_spec("psycopg") is not None,
            "requirement": "psycopg[binary]>=3.1",
        },
        "served": {
            "enabled": importlib.util.find_spec("vuoro_client") is not None,
            "requirement": "vuoro-client (the 'served' extra)",
        },
    }
    if backend["valid"] and backend["resolved_mode"] == "remote":
        schema = _probe_remote_schema(environ)
    elif backend["valid"] and backend["resolved_mode"] == "served":
        served_profile = None
        try:
            served_profile = _backend.load_backend_config(cwd=cwd, environ=environ).served_profile
        except _backend.BackendConfigError:
            served_profile = None
        schema = _probe_served_backend(environ, served_profile)
    elif backend["valid"]:
        schema = _probe_local_schema(environ)
    else:
        schema = {
            "backend": backend["environment_mode"],
            "expected_version": (
                REMOTE_SCHEMA_VERSION
                if backend["environment_mode"] == "remote"
                else sorted(_SERVED_EXPECTED_OPERATIONS)
                if backend["environment_mode"] == "served"
                else SQLITE_SCHEMA_VERSION
            ),
            "actual_version": None,
            "compatible": None,
            "status": "not-probed",
            "error": "backend configuration is invalid",
        }
    facts = {
        "schema_version": "sprintctl-doctor/v1",
        "provenance": {
            "invocation": {
                "argv0": sys.argv[0],
                "python": str(Path(sys.executable).resolve()),
                "code_version": __version__,
            },
            "executable": _probe_path_executable(),
            "package": _package_metadata(),
            "source": _source_metadata(cwd),
        },
        "extras": extras,
        "backend": backend,
        "schema": schema,
    }
    return evaluate_facts(facts)


def render_text(report: Mapping[str, Any]) -> str:
    provenance = report["provenance"]
    backend = report["backend"]
    schema = report["schema"]
    lines = [f"sprintctl doctor: {report['status']}"]
    lines.append(
        f"executable: {provenance['executable']['version'] or 'unknown'} "
        f"({provenance['executable']['path'] or 'not found'})"
    )
    lines.append(
        f"package: code={provenance['package']['code_version']} "
        f"metadata={provenance['package']['metadata_version'] or 'unknown'}"
    )
    source_version = provenance["source"]["version"] if provenance["source"]["present"] else "not detected"
    lines.append(f"source: {source_version}")
    served_extra = report["extras"].get("served", {})
    lines.append(
        f"extras: remote={'enabled' if report['extras']['remote']['enabled'] else 'missing'} "
        f"served={'enabled' if served_extra.get('enabled') else 'missing'}"
    )
    lines.append(
        f"backend: env={backend['environment_mode']} resolved={backend['resolved_mode'] or 'invalid'} "
        f"repo={backend.get('repo_id') or 'unresolved'} "
        f"source={backend.get('repo_source') or 'unresolved'} "
        f"marker={backend['marker']['backend'] if backend['marker'] else 'none'} "
        f"url={'configured' if backend['url_configured'] else 'unset'}"
    )
    lines.append(
        f"schema: backend={schema['backend']} expected={schema['expected_version']} "
        f"actual={schema['actual_version'] if schema['actual_version'] is not None else schema['status']}"
    )
    if report["findings"]:
        lines.append("findings:")
        for finding in report["findings"]:
            lines.append(f"- [{finding['severity']}] {finding['code']}: {finding['message']}")
            for guidance in finding["guidance"]:
                lines.append(f"  fix: {guidance}")
    return "\n".join(lines)


def dumps(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
