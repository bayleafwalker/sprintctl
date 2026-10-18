"""``sprintctl agent|message|run``: thin JSON verbs over served operations."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from sprintctl import served, served_routes
from sprintctl.cli import cli
from sprintctl.commands import inbox

INS = "ins_" + "A" * 26
MSG = "msg_" + "B" * 26
RUN = "run_" + "C" * 26


@pytest.fixture
def calls(monkeypatch):
    recorded = []
    config = SimpleNamespace(served_profile=object(), repo_id="repo-1")
    monkeypatch.setattr(inbox, "_served_config_or_none", lambda obj: config)

    def invoke(profile, operation, arguments, *, repo_id):
        assert profile is config.served_profile and repo_id == "repo-1"
        recorded.append((operation, arguments))
        return {"operation": operation, "replayed": False}

    monkeypatch.setattr(served, "invoke_agent_operation", invoke)
    return recorded


@pytest.mark.parametrize("argv,stdin,operation,arguments", [
    (["agent", "register", "--instance-id", INS, "--idempotency-key", "key-00001"], '{"schema": "x"}',
     "work.agent.register-v1", {"instance_id": INS, "declaration": {"schema": "x"}, "idempotency_key": "key-00001"}),
    (["agent", "observe", "--instance-id", INS, "--fact", "reachable", "--channel", "mcp",
      "--valid-for-seconds", "60", "--idempotency-key", "key-00002"], None, "work.agent.observe-v1",
     {"instance_id": INS, "fact": "reachable", "channel": "mcp", "valid_for_seconds": 60,
      "idempotency_key": "key-00002"}),
    (["agent", "retire", "--instance-id", INS, "--reason", "gone", "--idempotency-key", "key-00003"], None,
     "work.agent.retire-v1", {"instance_id": INS, "reason": "gone", "idempotency_key": "key-00003"}),
    (["agent", "list"], None, "work.agent.list-v1", {"include_retired": True}),
    (["agent", "list", "--active-only"], None, "work.agent.list-v1", {"include_retired": False}),
    (["agent", "describe", "--principal-id", "github:2:0"], None, "work.agent.describe-v1",
     {"principal_id": "github:2:0"}),
    (["message", "send"], '{"message_id": "%s"}' % MSG, "work.message.send-v1", {"message": {"message_id": MSG}}),
    (["message", "read", "--after-seq", "7", "--instance-id", INS], None, "work.message.read-v1",
     {"after_seq": 7, "limit": 100, "instance_id": INS}),
    (["message", "status", "--message-id", MSG], None, "work.message.status-v1", {"message_id": MSG}),
    (["message", "receipt", "--message-id", MSG, "--instance-id", INS, "--kind", "run-linked", "--json", "-"],
     '{"run_id": "%s"}' % RUN, "work.message.receipt-v1",
     {"run_id": RUN, "message_id": MSG, "instance_id": INS, "kind": "run-linked"}),
    (["message", "receipt", "--message-id", MSG, "--instance-id", INS, "--kind", "observed"], None,
     "work.message.receipt-v1", {"message_id": MSG, "instance_id": INS, "kind": "observed"}),
    (["message", "cancel", "--message-id", MSG, "--reason", "stop"], None, "work.message.cancel-v1",
     {"message_id": MSG, "reason": "stop"}),
    (["run", "context", "--run-id", RUN], None, "work.run.predecessor-context-v1", {"run_id": RUN}),
    (["run", "note", "--run-id", RUN, "--note", "hi", "--idempotency-key", "inbox.%s.note" % MSG], None,
     "work.session-note.write-v1", {"run_id": RUN, "note": "hi", "idempotency_key": "inbox.%s.note" % MSG}),
])
def test_each_verb_sends_one_operation_and_prints_its_json(calls, argv, stdin, operation, arguments):
    result = CliRunner().invoke(cli, argv, input=stdin)
    assert result.exit_code == 0, result.output
    assert calls == [(operation, arguments)]
    assert json.loads(result.output) == {"operation": operation, "replayed": False}


def test_a_refusal_prints_the_error_code_as_json(calls, monkeypatch):
    errors = pytest.importorskip("vuoro_client.errors")  # the served extra
    InvocationRejectedError = errors.InvocationRejectedError

    def refuse(*_args, **_kwargs):
        raise InvocationRejectedError("already-decided", "this message already has a decision", status_code=409)

    monkeypatch.setattr(served, "invoke_agent_operation", refuse)
    result = CliRunner().invoke(cli, ["message", "status", "--message-id", MSG])
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": {"code": "already-decided", "message": "this message already has a decision"}}


def test_malformed_json_input_is_refused_before_any_call(calls):
    result = CliRunner().invoke(cli, ["message", "send"], input="{not json")
    assert result.exit_code != 0 and "not valid JSON" in result.output
    assert calls == []


def test_outside_served_mode_the_verbs_refuse_clearly(monkeypatch):
    monkeypatch.setattr(inbox, "_served_config_or_none", lambda obj: None)
    result = CliRunner().invoke(cli, ["agent", "list"])
    assert result.exit_code != 0 and "SPRINTCTL_BACKEND=served" in result.output


def test_routes_are_classified_and_bound_to_published_operations():
    paths = {"agent register", "agent observe", "agent retire", "agent list", "agent describe",
             "message send", "message read", "message status", "message receipt", "message cancel",
             "run context", "run note", "run brief", "effect explain"}
    assert {path: served_routes.disposition_for(path) for path in paths} == dict.fromkeys(paths, "catalog")
    routed = {spec.cli_path: spec.operation for spec in served_routes.OPERATION_SPECS if spec.cli_path in paths}
    assert set(routed.values()) == served.AGENT_OPERATIONS
    with pytest.raises(ValueError):
        served.invoke_agent_operation(object(), "work.item.edit", {}, repo_id="r")
