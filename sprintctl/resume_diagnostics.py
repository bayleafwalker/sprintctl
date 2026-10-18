"""Read-only continuation checklist. Caller observations remain report-class."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from typing import Any

MAX_OBSERVATION_BYTES = 65_536
OBSERVATION_SCHEMA = "sprintctl-resume-observations/v1"
REPORT_SCHEMA = "sprintctl-resume-diagnostic/v1"
OBSERVATION_MAX_AGE = timedelta(hours=1)


def _observations(value, response, now):
    if value is None:
        return []
    fields = {"schema", "repo_id", "item_id", "snapshot_digest", "checks"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != OBSERVATION_SCHEMA:
        raise ValueError("observations must use the complete resume-observations/v1 object")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ValueError("observations exceed 64 KiB")
    delta = response["delta"]
    if (value["repo_id"] != response["repo_id"] or type(value["item_id"]) is not int
            or value["item_id"] != delta["work"]["item_id"]
            or value["snapshot_digest"] != delta["snapshot_digest"]):
        raise ValueError("observations must match this repository, item and owner snapshot")
    checks = value["checks"]
    if not isinstance(checks, list) or len(checks) > 64:
        raise ValueError("observations require at most 64 checks")
    visible = {ref["reference_id"] for ref in delta["reference_page"]}
    seen = set()
    selected = []
    for check in checks:
        required = {"target", "observed_at", "observer", "method", "status"}
        if (not isinstance(check, dict) or not required <= set(check)
                or set(check) - required - {"detail"}):
            raise ValueError("each observation requires target, observed_at, observer, method and status")
        target = check["target"]
        if isinstance(target, dict) and target == {"kind": "source-bytes"}:
            key = "source-bytes"
        elif (isinstance(target, dict) and set(target) == {"kind", "reference_id"}
                and target["kind"] == "reference" and type(target["reference_id"]) is int
                and target["reference_id"] in visible):
            key = f"reference:{target['reference_id']}"
        else:
            raise ValueError("observation targets must name source-bytes or a reference visible on this page")
        if key in seen:
            raise ValueError("duplicate observation target")
        seen.add(key)
        if check["status"] not in ("available", "inaccessible", "unchecked"):
            raise ValueError("observation status must be available, inaccessible or unchecked")
        for name, maximum in (("observer", 200), ("method", 200), ("detail", 500)):
            if name not in check:
                continue
            text = check[name]
            if not isinstance(text, str) or len(text) > maximum or (name != "detail" and not text.strip()):
                raise ValueError(f"observation {name} is empty or exceeds its bound")
        try:
            stamp = datetime.fromisoformat(check["observed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
                raise ValueError("not UTC")
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("observed_at must be a UTC timestamp") from error
        exclusion = "future" if stamp > now else "stale" if now - stamp > OBSERVATION_MAX_AGE else None
        selected.append({"check": deepcopy(check), "subject": key, "evidence_class": "report",
                         "eligible": exclusion is None, "exclusion_reason": exclusion})
    return selected


def diagnose_resume(response: dict[str, Any], *, checkpoint_supplied: bool,
                    observations=None, now: datetime | None = None) -> dict[str, Any]:
    """Derive a review checklist, not measured effort, authority or artifact assurance."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("diagnostic generation requires an aware clock")
    now = now.astimezone(timezone.utc)
    delta = response["delta"]
    observed = _observations(observations, response, now)
    findings = []

    def add(code, subject, order, provenance, next_check):
        findings.append({"reason_code": code, "subject": subject, "review_order": order,
                         "provenance": provenance, "next_check": next_check})

    for entry in observed:
        if entry["eligible"] and entry["check"]["status"] == "inaccessible":
            add("artifact-inaccessible-reported", entry["subject"], 1, "caller-report",
                "Recheck caller-reported access; this does not establish globally missing content.")
    if not checkpoint_supplied:
        add("comparison-baseline-absent", "comparison", 3, "caller-input",
            "Supply a saved owner delta.current checkpoint for a comparison.")
    if delta["source_checkpoint"] is None:
        add("source-checkpoint-absent", "continuation", 3, "owner-read",
            "Inspect or record a current continuation point; historical checkpoints may still exist.")
    for field, state in delta["comparison"].items():
        if state == "changed":
            add("owner-record-changed", field, 2, "owner-comparison", "Inspect the changed owner record before continuing.")
        elif state == "unknown":
            add("owner-record-comparison-unknown", field, 4, "owner-comparison",
                "Resolve the missing baseline or unavailable collection digest; do not infer equality.")
    if (not delta["reference_page_complete"] or not delta["decision_page_complete"]
            or delta["next_after_note_id"] is not None or delta["next_after_chain_seq"] is not None):
        add("owner-context-incomplete", "context-pages", 3, "owner-read",
            "Continue explicit cursors; Decision history may remain truncated.")
    for key, code in (("source_bytes", "source-content-unchecked"),
                      ("reference_git_correspondence", "reference-correspondence-unchecked"),
                      ("external_missing_content", "external-content-unchecked")):
        if delta[key] == "unknown":
            add(code, key, 4, "owner-unknown", "Perform and record the required content check separately.")
    add("outcome-not-assessed", "effect-outcome", 5, "read-scope",
        "Use effect evidence and independent target observations to assess outcomes.")
    findings.sort(key=lambda row: (row["review_order"], row["reason_code"], row["subject"]))
    return {"schema": REPORT_SCHEMA, "generated_at": now.isoformat(), "clock_source": "caller-UTC",
            "owner_response": deepcopy(response), "findings": findings, "observations": observed,
            "coverage": {"owner_read_count": 1, "artifact_checks_performed": 0,
                         "reference_page_complete": delta["reference_page_complete"],
                         "decision_page_complete": delta["decision_page_complete"],
                         "caller_observation_count": len(observed),
                         "eligible_caller_observations": sum(row["eligible"] for row in observed)},
            "ranking_basis": "declared-checklist-order", "recovery_effort": None,
            "authorizes_action": False}


def render_resume_diagnostic(report: dict[str, Any]) -> str:
    owner = report["owner_response"]
    lines = [f"RESUME CHECKLIST: {owner['repo_id']}#{owner['delta']['work']['item_id']}",
             "Order is a declared checklist, not measured recovery burden or cross-item priority."]
    for finding in report["findings"]:
        lines.append(f"  [{finding['reason_code']}] {finding['subject']} ({finding['provenance']})")
        lines.append(f"    {finding['next_check']}")
    for observation in report["observations"][:5]:
        check = observation["check"]
        lines.append("  Caller report: " + json.dumps({"subject": observation["subject"],
            "status": check["status"], "observer": check["observer"], "method": check["method"],
            "eligible": observation["eligible"], "exclusion_reason": observation["exclusion_reason"]}, ensure_ascii=True))
    if len(report["observations"]) > 5:
        lines.append("  Further caller reports are retained in JSON.")
    lines.extend(["No artifacts were checked by this command; caller reports do not upgrade owner unknowns.",
                  "Recovery effort: unmeasured", "authorizes_action: false"])
    return "\n".join(lines)
