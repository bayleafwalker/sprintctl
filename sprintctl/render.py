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
) -> str:
    # Parse as naive UTC — SQLite timestamps are also naive UTC strings
    now = datetime.strptime(rendered_at, "%Y-%m-%dT%H:%M:%SZ")
    all_items = [it for items in items_by_track.values() for it in items]
    active_count = sum(1 for it in all_items if it["status"] == "active")
    risk = _calc.sprint_overrun_risk(sprint, active_count, now)

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
        else:
            lines.append("  (no items)")
        lines.append("")

    lines.append(f"Rendered: {rendered_at}")

    return "\n".join(lines)
