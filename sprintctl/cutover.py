"""Per-repo authority + projection cutover dogfood evidence (item #1163).

Phase 28 (``sprintctl/pilot.py``, ``sprintctl/authority_config.py``,
``sprintctl/projection_reads.py``, ``sprintctl/authority.py``) built three
independent opt-in flags: observation shadowing, authority-command rollout
mode, and guarded projection reads. This module assembles them into one
per-repo **dogfood evidence packet** an operator reviews before deciding
whether this repository is ready to promote from shadow observation to an
authoritative cutover -- see ``docs/reference/cutover-dogfood.md``.

Scope (item #1163): opt-in sprintctl-repo pilot, parity histories,
watermark/reconciliation lag, stale-tool incidents, a rollback rehearsal, and
an explicit promotion gate. Non-scope: this module never performs a fleet
cutover or deletes a backend (see ``docs/plans/adr-outbox-sync-model.md``),
and it never decides to promote a repository itself -- ``promotable`` is
evidence for an operator-directed decision, the same posture
``docs/reference/capability-receipts.md`` documents for capability receipts.

This module is pure/read-mostly with one narrow, self-reversing exception:
``rehearse_rollback`` toggles the two small per-repo opt-in JSON files this
dogfood is about (never authoritative backend or projection data, which
those modules cannot write to by their own construction) and always restores
whatever was configured before it ran. Everything else here only reads
already-populated state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import authority_config as _authority_config
from . import doctor as _doctor
from . import pilot as _pilot
from . import projection as _projection
from . import projection_reads as _projection_reads

CUTOVER_EVIDENCE_CONTRACT_VERSION = "1"
DEFAULT_MAX_WATERMARK_AGE_SECONDS = _projection.DEFAULT_STALE_AFTER_SECONDS


class CutoverEvidenceError(ValueError):
    """Raised when cutover dogfood evidence cannot be assembled safely."""


def config_snapshot(
    *, cwd: Path | None = None, repo_root: Path | None = None
) -> dict[str, Any]:
    """Read-only snapshot of the three opt-in flags this dogfood exercises.

    Never mutates anything.
    """
    pilot_status = _pilot.shadow_pilot_status(cwd=cwd, repo_root=repo_root)
    authority_status = _authority_config.authority_command_status(cwd=cwd, repo_root=repo_root)
    reads_status = _projection_reads.projection_reads_status(cwd=cwd, repo_root=repo_root)
    return {
        "pilot_enabled": pilot_status.enabled,
        "authority_command_mode": authority_status.mode.value,
        "projection_reads_enabled": reads_status.enabled,
    }


def rehearse_rollback(
    *, cwd: Path | None = None, repo_root: Path | None = None
) -> dict[str, Any]:
    """Prove the rollback path round-trips without leaving a different config.

    Disables both the authority-command mode and guarded projection reads,
    confirms the disabled state took effect, then restores whatever was
    configured immediately before this rehearsal ran. Running this evidence
    collection never changes a repository's opted-in state by side effect --
    only ``sprintctl authority-config set`` / ``sprintctl projection-reads
    enable|disable`` (an operator's explicit action) does that.

    Only touches the two small per-repo JSON files these modules own
    (``.sprintctl/authority-command.json``, ``.sprintctl/projection-reads.json``)
    -- never authoritative backend or projection data, per those modules' own
    invariants (see their module docstrings).
    """
    before_authority = _authority_config.authority_command_status(cwd=cwd, repo_root=repo_root)
    before_reads = _projection_reads.projection_reads_status(cwd=cwd, repo_root=repo_root)

    disabled_authority = _authority_config.set_authority_command_mode(
        _authority_config.AuthorityCommandMode.OFF, cwd=cwd, repo_root=repo_root
    )
    disabled_reads = _projection_reads.set_projection_reads_enabled(
        False, cwd=cwd, repo_root=repo_root
    )

    restored_authority = _authority_config.set_authority_command_mode(
        before_authority.mode, cwd=cwd, repo_root=repo_root
    )
    restored_reads = _projection_reads.set_projection_reads_enabled(
        before_reads.enabled, cwd=cwd, repo_root=repo_root
    )

    authority_round_trip_ok = (
        disabled_authority.mode == _authority_config.AuthorityCommandMode.OFF
        and restored_authority.mode == before_authority.mode
    )
    reads_round_trip_ok = (
        disabled_reads.enabled is False and restored_reads.enabled == before_reads.enabled
    )

    return {
        "authority_command": {
            "before_mode": before_authority.mode.value,
            "disabled_mode": disabled_authority.mode.value,
            "restored_mode": restored_authority.mode.value,
            "round_trip_ok": authority_round_trip_ok,
        },
        "projection_reads": {
            "before_enabled": before_reads.enabled,
            "disabled_enabled": disabled_reads.enabled,
            "restored_enabled": restored_reads.enabled,
            "round_trip_ok": reads_round_trip_ok,
        },
        "rollback_ok": authority_round_trip_ok and reads_round_trip_ok,
    }


def evaluate_watermark_lag(
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
    max_age_seconds: int = DEFAULT_MAX_WATERMARK_AGE_SECONDS,
) -> dict[str, Any]:
    """Cached-projection watermark/reconciliation-lag evidence.

    Reuses ``sprintctl/projection.py``'s existing freshness assessment
    against the opt-in shadow-pilot cache; this only judges whether that
    assessment is within the dogfood's configured lag bound and never
    mutates the projection.
    """
    try:
        pilot_status = _pilot.shadow_pilot_status(cwd=cwd, repo_root=repo_root)
    except _pilot.ShadowPilotConfigError as exc:
        raise CutoverEvidenceError(str(exc)) from exc
    if not pilot_status.paths.projection_path.exists():
        return {
            "healthy": False,
            "fallback_reason": "missing",
            "watermark_offset": None,
            "watermark_advanced_at": None,
            "age_seconds": None,
            "schema_version": None,
            "stale_after_seconds": max_age_seconds,
            "max_age_seconds": max_age_seconds,
        }
    conn = _projection.open_cached_projection(pilot_status.paths.projection_path)
    try:
        freshness = _projection.assess_freshness(conn, stale_after_seconds=max_age_seconds)
    finally:
        conn.close()
    result = freshness.to_dict()
    result["max_age_seconds"] = max_age_seconds
    return result


def evaluate_stale_tool_incidents(*, cwd: Path | None = None) -> dict[str, Any]:
    """Reuse ``sprintctl doctor``'s read-only provenance/capability findings
    as this dogfood's stale-tool-incident evidence; no separate detector.
    """
    cwd = cwd or Path.cwd()
    report = _doctor.collect_report(cwd=cwd)
    incidents = [finding for finding in report["findings"] if finding["severity"] == "error"]
    return {
        "status": report["status"],
        "incidents": incidents,
        "findings": report["findings"],
    }


def build_cutover_evidence(
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
    parity: dict[str, Any] | None = None,
    max_watermark_age_seconds: int = DEFAULT_MAX_WATERMARK_AGE_SECONDS,
    rehearse: bool = True,
) -> dict[str, Any]:
    """Assemble the full per-repo cutover dogfood evidence packet.

    ``parity`` is a caller-computed parity report dict, typically
    ``sprintctl.shadow.compare_parity(...).to_dict()`` -- building it needs
    backend-specific event history, so it is intentionally not fetched here
    (keeps this module backend-agnostic and independently testable). Pass
    ``None`` when the pilot has never been enabled or synchronized; the
    promotion gate then blocks on ``"parity-not-evaluated"``.

    ``promotable`` is evidence for an operator-directed decision, never a
    self-executed promotion -- see the module docstring's non-scope note.
    """
    config = config_snapshot(cwd=cwd, repo_root=repo_root)
    watermark = evaluate_watermark_lag(
        cwd=cwd, repo_root=repo_root, max_age_seconds=max_watermark_age_seconds
    )
    stale_tools = evaluate_stale_tool_incidents(cwd=cwd or repo_root)
    rollback = rehearse_rollback(cwd=cwd, repo_root=repo_root) if rehearse else None

    blockers: list[str] = []
    if not config["pilot_enabled"]:
        blockers.append("pilot-not-enabled")
    if parity is None:
        blockers.append("parity-not-evaluated")
    elif not parity.get("is_equal", False):
        blockers.append("parity-diverged")
    if not watermark.get("healthy", False):
        blockers.append(f"watermark-{watermark.get('fallback_reason') or 'unhealthy'}")
    if stale_tools["incidents"]:
        blockers.append("stale-tool-incidents")
    if rehearse and not rollback["rollback_ok"]:
        blockers.append("rollback-rehearsal-failed")

    return {
        "contract_version": CUTOVER_EVIDENCE_CONTRACT_VERSION,
        "config": config,
        "parity": parity,
        "watermark": watermark,
        "stale_tools": stale_tools,
        "rollback_rehearsal": rollback,
        "promotable": len(blockers) == 0,
        "blockers": blockers,
    }


__all__ = [
    "CUTOVER_EVIDENCE_CONTRACT_VERSION",
    "CutoverEvidenceError",
    "DEFAULT_MAX_WATERMARK_AGE_SECONDS",
    "build_cutover_evidence",
    "config_snapshot",
    "evaluate_stale_tool_incidents",
    "evaluate_watermark_lag",
    "rehearse_rollback",
]
