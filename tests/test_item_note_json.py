"""Machine-readable notes reuse the existing single-write owner boundary."""
import json
import sys

import pytest

from sprintctl import db, served
from sprintctl.application import ApplicationRejection
import sprintctl.cli as cli_module
from sprintctl.cli import cli
from tests.test_served_routes import _configure_served_repo
from tests.test_work_application import _application, _context


def _arguments(item_id, *, as_json=True):
    return ["item", "note", "--id", str(item_id), "--type", "update",
            "--summary", "Recorded once"] + (["--json"] if as_json else [])


@pytest.fixture
def item_id(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "notes")
    return db.create_work_item(conn, active_sprint["id"], track, "Note target")


def _notes(conn, item_id):
    return conn.execute("SELECT * FROM event WHERE work_item_id=? AND event_type='update'",
                        (item_id,)).fetchall()


@pytest.mark.parametrize("as_json", [False, True])
def test_local_note_output_and_single_event(runner, conn, item_id, as_json):
    result = runner.invoke(cli, _arguments(item_id, as_json=as_json))
    assert result.exit_code == 0, result.output
    rows = _notes(conn, item_id)
    assert len(rows) == 1
    if as_json:
        assert json.loads(result.output) == dict(event_id=rows[0]["id"], item_id=item_id,
                                                note_type="update", summary="Recorded once")
    else:
        assert result.output == f"Recorded note #{rows[0]['id']} (update) on item #{item_id}: Recorded once\n"


def test_local_missing_item_does_not_print_success_json(runner, conn):
    result = runner.invoke(cli, _arguments(99999))
    assert result.exit_code == 1
    assert "not found" in result.output
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 0


@pytest.fixture
def served_note(conn, runner, tmp_path, monkeypatch):
    if sys.version_info < (3, 12):
        pytest.skip("served requires Python3.12+")
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _: pytest.fail("opened CLI/PG store"))
    owner = _application(store=conn, backend=db)
    calls = []
    state = {"failure": None, "after_write": False}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def invoke(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            if state["failure"] is not None and not state["after_write"]:
                raise state["failure"]
            try:
                result = owner.invoke(operation, arguments, _context(actor="authenticated-actor"))
                if state["failure"] is not None:
                    raise state["failure"]
                return result
            except ApplicationRejection as error:
                from vuoro_client import InvocationRejectedError
                raise InvocationRejectedError(error.code, error.message,
                                              status_code=error.http_status) from error

    monkeypatch.setattr(served, "_client", lambda _: Client())
    return calls, state


@pytest.mark.parametrize("as_json", [False, True])
def test_served_scoped_note_uses_owner_once(runner, conn, item_id, served_note, as_json):
    calls, _ = served_note
    args = _arguments(f"served-guard-test#{item_id}", as_json=as_json)
    args += ["--evidence-item-id", f"served-guard-test#{item_id}", "--actor", "ignored"]
    output = runner.invoke(cli, args)
    assert output.exit_code == 0, output.output
    rows = _notes(conn, item_id)
    assert len(rows) == len(calls) == 1
    assert rows[0]["actor"] == "authenticated-actor"
    assert json.loads(rows[0]["payload"])["evidence_item_id"] == item_id
    operation, arguments, kwargs = calls[0]
    assert operation == "work.item.note" and kwargs["repo_id"] == "served-guard-test"
    assert arguments["item_id"] == item_id and "actor" not in arguments
    if as_json:
        assert json.loads(output.output) == dict(event_id=rows[0]["id"], item_id=item_id,
                                                note_type="update", summary="Recorded once")
    else:
        assert output.output.startswith(f"Recorded note #{rows[0]['id']} (update) on item #{item_id}: Recorded once\n")
        assert "Context: repo=served-guard-test" in output.output


@pytest.mark.parametrize("mismatch", ["target", "evidence", "flag"])
def test_served_scope_mismatch_refuses_before_write(runner, conn, item_id, served_note, mismatch):
    calls, _ = served_note
    args = _arguments(f"other-repo#{item_id}" if mismatch == "target" else f"served-guard-test#{item_id}")
    if mismatch == "evidence":
        args += ["--evidence-item-id", f"other-repo#{item_id}"]
    if mismatch == "flag":
        args = ["--repo-id", "other-repo", *args]
    output = runner.invoke(cli, args)
    assert output.exit_code == 1
    assert "scope mismatch" in output.output
    assert calls == [] and _notes(conn, item_id) == []


def test_served_missing_item_does_not_retry_or_fallback(runner, conn, served_note):
    calls, _ = served_note
    output = runner.invoke(cli, _arguments("served-guard-test#99999"))
    assert output.exit_code == 1
    assert "item-not-found" in output.output
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 0


@pytest.mark.parametrize("after_write", [False, True])
def test_served_unknown_failure_does_not_retry_or_fallback(runner, conn, item_id, served_note, after_write):
    calls, state = served_note
    state["failure"] = RuntimeError("response lost")
    state["after_write"] = after_write
    output = runner.invoke(cli, _arguments(item_id))
    assert output.exit_code == 1
    assert "response lost" in output.output
    assert len(calls) == 1
    assert len(_notes(conn, item_id)) == (1 if after_write else 0)


@pytest.mark.parametrize("allow", [False, True])
def test_markerless_scope_requires_explicit_authorized_opt_in(
        runner, conn, item_id, served_note, tmp_path, monkeypatch, allow):
    calls, _ = served_note
    # A sibling of the marked repository has no inherited marker or git root.
    sibling = tmp_path.parent / f"{tmp_path.name}-markerless-sibling"
    sibling.mkdir()
    monkeypatch.chdir(sibling)
    args = ["--repo-id", "served-guard-test"]
    if allow:
        args += ["--allow-markerless-nonlocal"]
    output = runner.invoke(cli, [*args, *_arguments(f"served-guard-test#{item_id}")])
    if allow:
        assert output.exit_code == 0, output.output
        assert json.loads(output.output)["item_id"] == item_id
        assert len(calls) == len(_notes(conn, item_id)) == 1
    else:
        assert output.exit_code == 1
        assert "backend-uncorroborated" in output.output
        assert calls == [] and _notes(conn, item_id) == []
