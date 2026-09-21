import json

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", track="eng", assignee=None):
    tid = db.get_or_create_track(conn, sprint_id, track)
    return db.create_work_item(conn, sprint_id, tid, title, assignee=assignee)


def _checkpoint_note(conn, sprint_id, item_id, *, detail="checkpoint", **payload_extra):
    payload = {"summary": "checkpoint", "detail": detail, **payload_extra}
    return db.create_event(
        conn, sprint_id, "predecessor-session", "lane.checkpoint",
        work_item_id=item_id, payload=payload,
    )


class TestCliTableFormatting:
    def test_item_list_renders_table_headers(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Write docs", track="docs", assignee="alice")
        result = runner.invoke(cli, ["item", "list", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        header = result.output.splitlines()[0]
        assert "ID" in header
        assert "STATUS" in header
        assert "TRACK" in header
        assert "ASSIGNEE" in header
        assert "TITLE" in header

    def test_sprint_list_renders_table_headers(self, runner, active_sprint):
        result = runner.invoke(cli, ["sprint", "list"])
        assert result.exit_code == 0, result.output
        header = result.output.splitlines()[0]
        assert "ID" in header
        assert "STATUS" in header
        assert "KIND" in header
        assert "NAME" in header
        assert "DATES" in header

    def test_next_work_renders_table_headers(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Ready task", track="eng")
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Ready to start in sprint" in result.output
        assert "ID" in result.output
        assert "TRACK" in result.output
        assert "ASSIGNEE" in result.output
        assert "TITLE" in result.output


class TestNextWorkExplainTextFormatting:
    def test_next_work_explain_text_output_snapshot_ready_item(self, runner, conn, active_sprint):
        item_id = _item(conn, active_sprint["id"], "Ready task", track="eng")
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--explain"])
        assert result.exit_code == 0, result.output

        expected = "\n".join(
            [
                f"Sprint #{active_sprint['id']}: {active_sprint['name']}",
                "Summary: 1 pending total, 1 ready, 0 waiting on dependencies, 0 active reservations, 0 active unreserved",
                "",
                "Ready items (1):",
                "  ID  TRACK  ASSIGNEE  TITLE     ",
                "  --  -----  --------  ----------",
                f"  #{item_id}  eng    -         Ready task",
                "  Refs:",
                "    (none — ready items carry no doc refs; see 'item ref add --type doc')",
                "",
                "Dependency waiting items (0):",
                "  (none)",
                "",
                "Active reservations (0):",
                "  (none)",
                "",
                "Active items without reservations (0):",
                "  (none)",
                "",
                "Conflicts (0):",
                "  (none)",
                "",
                "Next action:",
                f"  [start-ready-item]  Start ready item #{item_id} because it is unblocked and no active reservations are open.",
                "",
                "Recommended commands:",
                f"  - sprintctl reservation reserve --item-id {item_id} --actor <name> --session-id <session-id> --json",
                f"  - sprintctl item show --id {item_id}",
            ]
        )
        assert result.output == f"{expected}\n"

    def test_next_work_explain_text_output_snapshot_dependency_waiting(
        self, runner, conn, active_sprint
    ):
        blocker_id = _item(conn, active_sprint["id"], "Blocker", track="eng")
        blocked_id = _item(conn, active_sprint["id"], "Blocked task", track="eng")
        db.add_dep(conn, blocker_id, blocked_id)
        conn.execute("UPDATE work_item SET status = 'blocked' WHERE id = ?", (blocker_id,))
        conn.commit()

        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--explain"])
        assert result.exit_code == 0, result.output

        expected = "\n".join(
            [
                f"Sprint #{active_sprint['id']}: {active_sprint['name']}",
                "Summary: 1 pending total, 0 ready, 1 waiting on dependencies, 0 active reservations, 0 active unreserved",
                "",
                "Ready items (0):",
                "  (none)",
                "",
                "Dependency waiting items (1):",
                "  ID  TRACK  ASSIGNEE  BLOCKERS  TITLE       ",
                "  --  -----  --------  --------  ------------",
                f"  #{blocked_id}  eng    -         #{blocker_id}        Blocked task",
                "",
                "Active reservations (0):",
                "  (none)",
                "",
                "Active items without reservations (0):",
                "  (none)",
                "",
                "Conflicts (1):",
                "  [dependency-blocked]  1 pending item(s) are waiting on unresolved blockers.",
                "",
                "Next action:",
                f"  [unblock-dependent-work]  Resolve blocker #{blocker_id} to unblock item #{blocked_id}.",
                "",
                "Recommended commands:",
                f"  - sprintctl item show --id {blocker_id}",
                f"  - sprintctl item show --id {blocked_id}",
                f"  - sprintctl next-work --sprint-id {active_sprint['id']} --json --explain",
            ]
        )
        assert result.output == f"{expected}\n"


class TestNextWorkCheckpointedUnacked:
    """agentops#2475 (2450-S6c): plain `next-work` must show an unacked
    lane.checkpoint's item, branch, sha, and next_action without --explain."""

    def test_plain_output_shows_unacked_checkpoint(self, runner, conn, active_sprint):
        item_id = _item(conn, active_sprint["id"], "Interrupted task", track="eng")
        _checkpoint_note(
            conn, active_sprint["id"], item_id,
            detail="validated: it builds; rejected: approach A; next_action: run the migration",
            git_branch="wt/2475-render-checkpoints", git_sha="a" * 40,
        )

        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])

        assert result.exit_code == 0, result.output
        assert "Checkpointed unacked items (1):" in result.output
        assert f"#{item_id}" in result.output
        assert "wt/2475-render-checkpoints" in result.output
        assert "a" * 40 in result.output
        assert "next_action: run the migration" in result.output

    def test_include_checkpoints_false_suppresses_the_section(self, runner, conn, active_sprint):
        item_id = _item(conn, active_sprint["id"], "Interrupted task", track="eng")
        _checkpoint_note(
            conn, active_sprint["id"], item_id,
            detail="next_action: run the migration",
            git_branch="wt/2475-render-checkpoints", git_sha="a" * 40,
        )

        result = runner.invoke(
            cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--no-include-checkpoints"]
        )

        assert result.exit_code == 0, result.output
        assert "Checkpointed unacked items" not in result.output
        assert "next_action" not in result.output

    def test_explain_json_is_unaffected_by_include_checkpoints_flag(self, runner, conn, active_sprint):
        # agentops#2475 acceptance: the explain JSON contract does not change
        # because of this flag -- the --explain --json fixture output must be
        # byte-identical whether or not checkpoints are included in plain
        # output, and must not gain any new top-level key.
        item_id = _item(conn, active_sprint["id"], "Interrupted task", track="eng")
        _checkpoint_note(
            conn, active_sprint["id"], item_id,
            detail="next_action: run the migration",
            git_branch="wt/2475-render-checkpoints", git_sha="a" * 40,
        )

        result_included = runner.invoke(
            cli,
            [
                "next-work", "--sprint-id", str(active_sprint["id"]),
                "--explain", "--json", "--include-checkpoints",
            ],
        )
        result_excluded = runner.invoke(
            cli,
            [
                "next-work", "--sprint-id", str(active_sprint["id"]),
                "--explain", "--json", "--no-include-checkpoints",
            ],
        )

        assert result_included.exit_code == 0, result_included.output
        assert result_excluded.exit_code == 0, result_excluded.output
        assert result_included.output == result_excluded.output

        payload = json.loads(result_included.output)
        # "checkpointed_unacked" is deliberately absent: this flag governs
        # only the plain-output section, not the --explain --json contract,
        # which does not carry a top-level checkpointed_unacked key here.
        assert "checkpointed_unacked" not in payload
        assert set(payload.keys()) == {
            "contract_version", "sprint", "summary", "ready_items",
            "dependency_waiting_items", "active_reservations",
            "active_unreserved_items", "conflicts",
            "next_action", "recommended_commands", "recommended_command_bundle",
            "projection",
        }

    def test_plain_output_bounds_table_width_for_a_realistic_long_detail(self, runner, conn, active_sprint):
        # A realistic checkpoint detail: multi-line, well over 1000 chars,
        # with a next_action segment buried in the middle. Before the fix,
        # this text landed in a DETAIL table column, which `_render_table`
        # pads to the widest cell without truncation -- the header and
        # separator lines ballooned to the detail's width and the embedded
        # newlines spilled outside the table, destroying alignment.
        detail_lines = [
            "validated: `uv run --extra dev pytest -q tests/test_cli_output_format.py` "
            "passes; the new option threads through next_work_cmd cleanly.",
            "rejected: putting the detail inline as a single joined line -- it reads "
            "as an unreadable wall of text once payload notes exceed a paragraph.",
            "context: " + ("the checkpoint payload carries prior session notes here. " * 15),
            "next_action: rebase onto origin/main and rerun the targeted test file "
            "before touching the full suite.",
            "trailing notes: " + ("see the design doc for background. " * 10),
        ]
        long_detail = "\n".join(detail_lines)
        assert len(long_detail) > 1000

        item_id = _item(conn, active_sprint["id"], "Interrupted task", track="eng")
        _checkpoint_note(
            conn, active_sprint["id"], item_id,
            detail=long_detail,
            git_branch="wt/2475-render-checkpoints", git_sha="b" * 40,
        )

        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        header_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("ID") and "SHA" in line
        )
        header = lines[header_idx]
        separator = lines[header_idx + 1]
        # The header/separator's width is driven only by ID/TITLE/BRANCH/SHA
        # -- not by the 1000+ char detail -- so it stays well under the width
        # a DETAIL column would have forced (a real detail like this one
        # blew the line to 477 chars before the fix).
        assert len(header) < 200, header
        assert len(separator) < 200, separator

        assert "wt/2475-render-checkpoints" in result.output
        assert "b" * 40 in result.output
        assert "rebase onto origin/main and rerun the targeted test file" in result.output
        for detail_line in detail_lines:
            assert f"    #{item_id}  {detail_line}" in lines


class TestCliStatusColor:
    def test_item_list_uses_ansi_color_when_enabled(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Pending task")
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"])],
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\x1b[" in result.output
        assert "pending" in result.output


class TestCliFzfOutput:
    def test_item_list_fzf_outputs_parseable_rows(self, runner, conn, active_sprint):
        item_id = _item(conn, active_sprint["id"], "Write docs", track="docs", assignee="alice")
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"]), "--fzf"],
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert lines == [f"#{item_id}\tpending\tdocs\talice\tWrite docs\t-"]

    def test_item_list_fzf_disables_colorized_table_output(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Task", track="eng", assignee="alice")
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"]), "--fzf"],
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\x1b[" not in result.output

    def test_item_list_fzf_empty_outputs_no_lines(self, runner, active_sprint):
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"]), "--fzf"],
        )
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_item_list_fzf_cannot_combine_with_json(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Task")
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"]), "--fzf", "--json"],
        )
        assert result.exit_code == 1
        assert "--fzf cannot be combined with --json" in result.output

    def test_item_list_fzf_escapes_tabs_newlines_and_backslashes(self, runner, conn, active_sprint):
        item_id = _item(
            conn,
            active_sprint["id"],
            title="Fix\tbad\nline\\path",
            track="eng\tops\ncore",
            assignee="al\nice\t\\",
        )
        result = runner.invoke(
            cli,
            ["item", "list", "--sprint-id", str(active_sprint["id"]), "--fzf"],
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0].split("\t") == [
            f"#{item_id}",
            "pending",
            "eng\\tops\\ncore",
            "al\\nice\\t\\\\",
            "Fix\\tbad\\nline\\\\path",
            "-",
        ]
