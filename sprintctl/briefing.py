"""Presentation of bounded owner continuation facts, without reads or inference."""

from __future__ import annotations

import json
from typing import Any


_COMPARISONS = {
    "work_edit_revision": "Work definition",
    "status_revision": "Work status",
    "current_release_digest": "Release",
    "current_decision_digest": "Decision records",
    "current_reference_set_digest": "Reference records",
}


def _quoted(value: Any) -> str:
    """Keep record content distinct from instructions and escape terminal controls."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def render_handoff_position(bundle: dict[str, Any]) -> list[str]:
    """Expose recorded position separately from the caller's current observations."""
    lines = ["CONTINUATION POSITION:"]
    checkpoint = bundle.get("last_checkpoint")
    if checkpoint:
        for field in ("sha", "branch", "worktree", "recorded_at", "dirty_file_count"):
            lines.append(f"  Predecessor {field}: {_quoted(checkpoint.get(field))}")
        if checkpoint.get("dirty_file_count"):
            lines.append("  The predecessor had uncommitted work; its commit alone does not identify all contents.")
    else:
        lines.append("  Predecessor checkpoint: unavailable")
    current = bundle.get("git_context")
    if current is None:
        lines.append("  Current Git context: unavailable; current worktree cleanliness is unknown")
    else:
        lines.append(f"  Current commit: {_quoted(current.get('sha'))}")
        dirty = current.get("dirty_files")
        lines.append(f"  Current dirty files: {len(dirty) if dirty is not None else 'unknown'}")
    delta = bundle.get("delta_since_last_handoff") or {}
    lines.append(f"  Previous handoff: {_quoted(delta.get('previous_handoff_at'))}")
    if delta.get("previous_handoff_at") is None:
        lines.append("  Changes since previous handoff: unknown (no comparison baseline)")
    else:
        lines.append(f"  Touched item IDs: {_quoted(delta.get('item_ids_touched', []))}")
        lines.append(f"  Touched reservation IDs: {_quoted(delta.get('reservation_ids_touched', []))}")
        lines.append(f"  New events: {delta.get('event_count', 'unknown')}")
    freshness = bundle.get("freshness") or {}
    lines.append(f"  Stale items: {freshness.get('stale_item_count', 'unknown')}")
    return lines


def render_run_brief(response: dict[str, Any]) -> str:
    """Render one owner page. Unknown facts never become unchanged or accepted."""
    delta = response["delta"]
    work = delta["work"]
    lines = [
        f"RESUME: {response['repo_id']}#{work['item_id']}",
        f"Work: {_quoted(work['title'])}",
        f"Objective excerpt: {_quoted(work['description_excerpt'])}",
        f"Status: {_quoted(work['status'])}",
        f"Owner snapshot: {delta['snapshot_digest']}",
        "This briefing records context; it grants no authority to act.",
    ]
    if work["title_truncated"] or work["description_truncated"]:
        lines.append("Work text is truncated; inspect the full item before continuing.")
    for state in ("changed", "unchanged", "unknown"):
        labels = [label for key, label in _COMPARISONS.items()
                  if delta["comparison"].get(key, "unknown") == state]
        lines.append(f"{state.upper()}: {', '.join(labels) if labels else '(none)'}")
    lines.extend([
        "Unchanged compares owner records only; it does not verify artifact bytes.",
        f"Current release: {_quoted(delta['current_release'])}",
        "Latest decision record: " + _quoted(
            None if delta["latest_decision"] is None else {
                key: delta["latest_decision"][key] for key in ("id", "kind", "release_digest")
            }),
        f"Continuation point: {_quoted(delta['source_checkpoint'])}",
        f"Predecessor: {_quoted(delta['predecessor_run_id'])}",
        f"Predecessor context on this page: {len(delta['predecessor_notes'])} note references, "
        f"{len(delta['predecessor_evidence'])} evidence references",
        "REFERENCES (text excerpt of one owner page):",
    ])
    for ref in delta["reference_page"][:5]:
        url = ref["url"][:200]
        suffix = " [truncated; inspect full reference]" if not ref["url_is_complete"] or len(ref["url"]) > 200 else ""
        lines.append(f"  {ref['reference_id']} [{ref['ref_type']}] {_quoted(url)}{suffix}")
    if len(delta["reference_page"]) > 5:
        lines.append(f"  {len(delta['reference_page']) - 5} more references on this page; inspect JSON.")
    if not delta["reference_page"]:
        lines.append("  (none on this page)")
    lines.extend([
        "CHECK BEFORE CONTINUING:",
        f"  Source bytes: {delta['source_bytes']}",
        f"  Reference/Git correspondence: {delta['reference_git_correspondence']}",
        f"  External content availability: {delta['external_missing_content']}",
        "  Inspect changed and unknown records, verify artifact digests and current authority.",
        "  Use 'sprintctl next-work --explain' for the current action recommendation.",
        f"Reference page complete: {delta['reference_page_complete']}",
        f"Decision page complete: {delta['decision_page_complete']}",
    ])
    if (not delta["reference_page_complete"] or not delta["decision_page_complete"]
            or delta["next_after_note_id"] is not None or delta["next_after_chain_seq"] is not None):
        lines.append("More context may remain; JSON retains cursors and completeness flags. "
                     "Continue reference/note/evidence pages explicitly; decision history may be truncated.")
    lines.append("Save the JSON response's delta.current as the checkpoint for your next comparison.")
    return "\n".join(lines)
