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

