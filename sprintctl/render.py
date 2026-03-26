def render_sprint_doc(
    sprint: dict,
    tracks: list[dict],
    items_by_track: dict[int, list[dict]],
    rendered_at: str,
) -> str:
    lines: list[str] = []

    lines.append(f"SPRINT: {sprint['name']}  [{sprint['status']}]")
    lines.append(f"Goal:   {sprint['goal']}")
    lines.append(f"Dates:  {sprint['start_date']} to {sprint['end_date']}")
    lines.append(f"ID:     {sprint['id']}")
    lines.append("")

    for track in tracks:
        lines.append(f"--- Track: {track['name']} ---")
        items = items_by_track.get(track["id"], [])
        if items:
            for item in items:
                assignee = item.get("assignee") or "-"
                lines.append(
                    f"  [{item['status']:8}] #{item['id']}  {item['title']}  (assignee: {assignee})"
                )
        else:
            lines.append("  (no items)")
        lines.append("")

    lines.append(f"Rendered: {rendered_at}")

    return "\n".join(lines)
