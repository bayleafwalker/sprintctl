from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", track="eng", assignee=None):
    tid = db.get_or_create_track(conn, sprint_id, track)
    return db.create_work_item(conn, sprint_id, tid, title, assignee=assignee)


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
                "Summary: 1 pending total, 1 ready, 0 waiting on dependencies, 0 active claims",
                "",
                "Ready items (1):",
                "  ID  TRACK  ASSIGNEE  TITLE     ",
                "  --  -----  --------  ----------",
                f"  #{item_id}  eng    -         Ready task",
                "",
                "Dependency waiting items (0):",
                "  (none)",
                "",
                "Active claims (0):",
                "  (none)",
                "",
                "Conflicts (0):",
                "  (none)",
                "",
                "Next action:",
                f"  [start-ready-item]  Start ready item #{item_id} because it is unblocked and no active claims are open.",
            ]
        )
        assert result.output == f"{expected}\n"

    def test_next_work_explain_text_output_snapshot_dependency_waiting(
        self, runner, conn, active_sprint
    ):
        blocker_id = _item(conn, active_sprint["id"], "Blocker", track="eng")
        blocked_id = _item(conn, active_sprint["id"], "Blocked task", track="eng")
        db.add_dep(conn, blocker_id, blocked_id)
        conn.execute("UPDATE work_item SET status = 'active' WHERE id = ?", (blocker_id,))
        conn.commit()

        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--explain"])
        assert result.exit_code == 0, result.output

        expected = "\n".join(
            [
                f"Sprint #{active_sprint['id']}: {active_sprint['name']}",
                "Summary: 1 pending total, 0 ready, 1 waiting on dependencies, 0 active claims",
                "",
                "Ready items (0):",
                "  (none)",
                "",
                "Dependency waiting items (1):",
                "  ID  TRACK  ASSIGNEE  BLOCKERS  TITLE       ",
                "  --  -----  --------  --------  ------------",
                f"  #{blocked_id}  eng    -         #{blocker_id}        Blocked task",
                "",
                "Active claims (0):",
                "  (none)",
                "",
                "Conflicts (1):",
                "  [dependency-blocked]  1 pending item(s) are waiting on unresolved blockers.",
                "",
                "Next action:",
                f"  [unblock-dependent-work]  Resolve blocker #{blocker_id} to unblock item #{blocked_id}.",
            ]
        )
        assert result.output == f"{expected}\n"


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
        assert lines == [f"#{item_id}\tpending\tdocs\talice\tWrite docs"]

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
        ]
