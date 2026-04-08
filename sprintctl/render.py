from datetime import datetime

from . import calc as _calc


def _fmt_idle(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def render_sprint_doc(
    sprint: dict,
    tracks: list[dict],
    items_by_track: dict[int, list[dict]],
    rendered_at: str,
    refs_by_item: dict[int, list[dict]] | None = None,
) -> str:
    # Parse as naive UTC — SQLite timestamps are also naive UTC strings
    now = datetime.strptime(rendered_at, "%Y-%m-%dT%H:%M:%SZ")
    all_items = [it for items in items_by_track.values() for it in items]
    active_count = sum(1 for it in all_items if it["status"] == "active")
    risk = _calc.sprint_overrun_risk(sprint, active_count, now)
    refs_by_item = refs_by_item or {}

    lines: list[str] = []

    header = f"SPRINT: {sprint['name']}  [{sprint['status']}]"
    if risk["overdue"]:
        header += "  [OVERDUE]"
    elif risk["at_risk"]:
        header += "  [AT RISK]"
    lines.append(header)
    lines.append(f"Goal:   {sprint['goal']}")
    if sprint.get("start_date") and sprint.get("end_date"):
        lines.append(f"Dates:  {sprint['start_date']} to {sprint['end_date']}")
    lines.append(f"ID:     {sprint['id']}")
    if all_items:
        counts = _calc.track_health(all_items)["counts"]
        lines.append(
            f"Items:  {len(all_items)} total — "
            f"{counts['done']} done, {counts['active']} active, "
            f"{counts['pending']} pending, {counts['blocked']} blocked"
        )
    lines.append("")

    for track in tracks:
        lines.append(f"--- Track: {track['name']} ---")
        items = items_by_track.get(track["id"], [])
        if items:
            health = _calc.track_health(items)
            done_pct = int(health["done_ratio"] * 100)
            blocked_pct = int(health["blocked_ratio"] * 100)
            lines.append(
                f"  health: {health['total']} items — "
                f"{health['counts']['done']} done ({done_pct}%), "
                f"{health['counts']['active']} active, "
                f"{health['counts']['pending']} pending, "
                f"{health['counts']['blocked']} blocked ({blocked_pct}%)"
            )
            for item in items:
                assignee = item.get("assignee") or "-"
                staleness = _calc.item_staleness(item, now)
                stale_tag = f"  [stale {_fmt_idle(staleness['idle_seconds'])}]" if staleness["is_stale"] else ""
                lines.append(
                    f"  [{item['status']:8}] #{item['id']}  {item['title']}  "
                    f"(assignee: {assignee}){stale_tag}"
                )
                for ref in refs_by_item.get(item["id"], []):
                    label = f" {ref['label']}" if ref["label"] else ""
                    lines.append(f"    ref [{ref['ref_type']}]{label}: {ref['url']}")
        else:
            lines.append("  (no items)")
        lines.append("")

    lines.append(f"Rendered: {rendered_at}")

    return "\n".join(lines)
