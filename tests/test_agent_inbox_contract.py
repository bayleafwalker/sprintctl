"""Agent directory and inbox v1: argument rules, catalog entries, SQLite refusal.

The hand-written validators in sprintctl.agent_inbox must agree with the
contract's JSON schemas (vendored in tests/fixtures/agent-inbox-v1) on every
case below.  Storage behaviour is covered by tests/pg/test_agent_inbox.py.
"""
from __future__ import annotations

import copy
import sqlite3
from types import SimpleNamespace

import pytest

from sprintctl import agent_inbox as inbox
from sprintctl import db
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.agent_inbox_schemas import is_valid

INS = "ins_" + "A" * 26
MSG = "msg_" + "B" * 26
RUN = "run_" + "C" * 26


def declaration(**overrides):
    value = {"schema": "agent-declaration/v1", "harness": {"name": "claude-code", "version": "1.0"},
             "adapter": None, "host_class": "workstation", "interactivity": "interactive",
             "accepted_kinds": ["continue"],
             "channels": [{"channel": "mcp", "wake_modes": ["read-while-running"]}]}
    value.update(overrides)
    return value


def envelope(**overrides):
    value = {"schema": "agent-message/v1", "message_id": MSG, "recipient": {"principal_id": "github:2:0"},
             "kind": "continue", "mode": "request", "context": {"item_id": 1},
             "body": {"text": "please continue"}, "created_at": "2026-10-14T10:00:00Z"}
    value.update(overrides)
    return value


def _accepts(check, value) -> bool:
    try:
        check(copy.deepcopy(value))
    except inbox.InboxRefused as exc:
        assert exc.code == "invalid-arguments", exc.code
        return False
    return True


DECLARATIONS = [
    declaration(),
    declaration(adapter={"name": "opencode", "version": "0.1"}),
    declaration(interactivity="non-interactive", channels=[], accepted_kinds=[]),
    declaration(host_class="mainframe"),
    declaration(adapter={"name": "x"}),
    declaration(accepted_kinds=["continue", "continue"]),
    declaration(accepted_kinds=["delegate"]),
    declaration(channels=[{"channel": "Bad", "wake_modes": ["read-while-running"]}]),
    declaration(channels=[{"channel": "mcp", "wake_modes": []}]),
    declaration(channels=[{"channel": "mcp", "wake_modes": ["sometimes"]}]),
    declaration(harness={"name": "", "version": "1"}),
    declaration(schema="agent-declaration/v2"),
    {**declaration(), "extra": 1},
    {k: v for k, v in declaration().items() if k != "adapter"},
]

ENVELOPES = [
    envelope(),
    envelope(context={"predecessor_run_id": RUN, "correlation_id": "corr-0001",
                      "work_link": "vuoro-work://link/v1/abc_-9"}),
    envelope(recipient={"principal_id": "github:2:0", "instance_id": INS}),
    envelope(artifacts=[{"uri": "s3://x", "sha256": "0" * 64, "size_bytes": 0}], not_after="2026-10-14T10:00:00.5+03:00"),
    envelope(mode="delegation"),
    envelope(context={"correlation_id": "abcdefgh"}),
    envelope(context={"item_id": 0}),
    envelope(context={"item_id": True}),
    envelope(context={"predecessor_run_id": "run_x"}),
    envelope(context={"item_id": 1, "work_link": "https://example.com"}),
    envelope(recipient={"principal_id": ""}),
    envelope(recipient={"principal_id": "p", "instance_id": "ins_short"}),
    envelope(kind="delegate"),
    envelope(mode="push"),
    envelope(body={"text": "x" * 16385}),
    envelope(body={"text": 1}),
    envelope(artifacts=[{"uri": "u", "sha256": "0" * 64, "size_bytes": 1}] * 17),
    envelope(artifacts=[{"uri": "u", "sha256": "G" * 64, "size_bytes": 1}]),
    envelope(created_at="yesterday"),
    envelope(message_id="msg_lower" + "a" * 17),
    {**envelope(), "extra": 1},
    {k: v for k, v in envelope().items() if k != "created_at"},
]


@pytest.mark.parametrize("value", DECLARATIONS)
def test_declaration_validation_agrees_with_the_contract_schema(value):
    assert _accepts(inbox.validate_declaration, value) == is_valid("declaration", value)


@pytest.mark.parametrize("value", ENVELOPES)
def test_envelope_validation_agrees_with_the_contract_schema(value):
    assert _accepts(inbox.validate_envelope, value) == is_valid("message", value)


def _sized(target: int) -> dict:
    value = envelope(artifacts=[{"uri": "a", "sha256": "0" * 64, "size_bytes": 1} for _ in range(16)])
    remaining = target - len(inbox.canonical_bytes(value))
    for artifact in value["artifacts"]:
        extra = min(2040, remaining // 4)
        artifact["uri"] += "\U0001F600" * extra
        remaining -= 4 * extra
    value["artifacts"][0]["uri"] += "b" * remaining
    assert len(inbox.canonical_bytes(value)) == target
    return value


def test_envelope_admission_limits_and_mode():
    inbox.admit_envelope(_sized(64 * 1024))
    with pytest.raises(inbox.InboxRefused) as caught:
        inbox.admit_envelope(_sized(64 * 1024 + 1))
    assert caught.value.code == "payload-too-large"
    inbox.admit_envelope(envelope(body={"text": "x" * 16384}))
    with pytest.raises(inbox.InboxRefused) as caught:
        inbox.admit_envelope(envelope(body={"text": "é" * 8193}))  # 16386 UTF-8 bytes
    assert caught.value.code == "payload-too-large"
    with pytest.raises(inbox.InboxRefused) as caught:
        inbox.admit_envelope(envelope(mode="delegation"))
    assert caught.value.code == "mode-not-supported"
    admitted, encoded = inbox.admit_envelope(envelope())
    assert encoded == inbox.canonical_bytes(admitted)
    assert b" " not in encoded.replace(b"please continue", b"")  # no insignificant whitespace


@pytest.mark.parametrize("arguments,fields", [
    ({"kind": "observed"}, {}),
    ({"kind": "refused", "reason": {"code": "busy", "text": "no"}}, {"reason": {"code": "busy", "text": "no"}}),
    ({"kind": "run-linked", "run_id": RUN}, {"run_id": RUN}),
    ({"kind": "launch-ambiguous", "detail": "spawn timed out"}, {"detail": "spawn timed out"}),
    ({"kind": "launch-ambiguous", "detail": "d", "run_id": RUN}, {"detail": "d", "run_id": RUN}),
    ({"kind": "result-linked", "outcome": "completed", "evidence_refs": [{"kind": "commit", "ref": "abc"}],
      "summary": "ok"}, {"outcome": "completed", "evidence_refs": [{"kind": "commit", "ref": "abc"}], "summary": "ok"}),
])
def test_receipt_arguments_separate_the_kinds_own_fields(arguments, fields):
    assert inbox.receipt_arguments({"message_id": MSG, "instance_id": INS, **arguments}) == (
        MSG, INS, arguments["kind"], fields)


@pytest.mark.parametrize("arguments", [
    {"kind": "mystery"},
    {"kind": "cancel-requested", "reason": "x"},
    {"kind": "observed", "run_id": RUN},
    {"kind": "refused"},
    {"kind": "refused", "reason": {"code": "Bad", "text": "x"}},
    {"kind": "run-linked"},
    {"kind": "launch-ambiguous", "detail": "x" * 2049},
    {"kind": "launch-ambiguous", "detail": "é" * 1025},
    {"kind": "result-linked", "outcome": "not-started", "evidence_refs": [], "summary": ""},
    {"kind": "result-linked", "outcome": "won", "evidence_refs": [], "summary": ""},
    {"kind": "result-linked", "outcome": "failed", "evidence_refs": [{"kind": "x"}], "summary": ""},
    {"kind": "result-linked", "outcome": "failed", "evidence_refs": [{"kind": "c", "ref": "r"}] * 33, "summary": ""},
])
def test_receipt_arguments_refuse_malformed_kinds_and_fields(arguments):
    with pytest.raises(inbox.InboxRefused) as caught:
        inbox.receipt_arguments({"message_id": MSG, "instance_id": INS, **arguments})
    assert caught.value.code == "invalid-arguments"


@pytest.mark.parametrize("arguments", [
    {"instance_id": INS, "fact": "observed", "valid_for_seconds": 0, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "observed", "valid_for_seconds": 3601, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "observed", "valid_for_seconds": True, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "reachable", "valid_for_seconds": 5, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "observed", "channel": "mcp", "valid_for_seconds": 5, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "executing", "valid_for_seconds": 5, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "attended", "run_id": RUN, "valid_for_seconds": 5, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "present", "valid_for_seconds": 5, "idempotency_key": "key-00001"},
    {"instance_id": INS, "fact": "observed", "valid_for_seconds": 5, "idempotency_key": "short"},
    {"instance_id": INS, "fact": "observed", "valid_for_seconds": 5, "idempotency_key": "key-00001",
     "principal_id": "someone-else"},
])
def test_observe_arguments_bounds(arguments):
    with pytest.raises(inbox.InboxRefused) as caught:
        inbox.observe_arguments(arguments)
    assert caught.value.code == "invalid-arguments"


def test_identity_and_scope_are_never_arguments():
    """principal_id, workspace_id and repo_id are the authenticated call's."""
    for name in ("principal_id", "workspace_id", "repo_id"):
        with pytest.raises(inbox.InboxRefused):
            inbox.register_arguments({"instance_id": INS, "declaration": declaration(),
                                      "idempotency_key": "key-00001", name: "x"})
        with pytest.raises(inbox.InboxRefused):
            inbox.read_arguments({"after_seq": 0, "limit": 1, name: "x"})


def test_catalog_publishes_the_ten_operations():
    contracts = {c.name: c for c in WORK_OPERATION_CONTRACTS}
    expected = {
        "work.agent.register-v1": ("work:evidence", "write"),
        "work.agent.observe-v1": ("work:evidence", "write"),
        "work.agent.retire-v1": ("work:evidence", "write"),
        "work.agent.list-v1": ("work:read", "read"),
        "work.agent.describe-v1": ("work:read", "read"),
        "work.message.send-v1": ("work:evidence", "write"),
        "work.message.read-v1": ("work:read", "read"),
        "work.message.status-v1": ("work:read", "read"),
        "work.message.receipt-v1": ("work:evidence", "write"),
        "work.message.cancel-v1": ("work:evidence", "write"),
    }
    assert set(expected) == inbox.OPERATIONS
    for name, (authority, semantics) in expected.items():
        contract = contracts[name]
        assert (contract.required_authority, contract.execution_semantics) == (authority, semantics)
        assert contract.idempotency == "not-allowed"
        assert contract.input_schema["additionalProperties"] is False
        for field in ("principal_id", "workspace_id", "repo_id"):
            if name != "work.agent.describe-v1" or field != "principal_id":
                assert field not in contract.input_schema["properties"], (name, field)


def test_standalone_sqlite_refuses_every_operation_clearly(tmp_path):
    conn = sqlite3.connect(tmp_path / "local.db")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    app = WorkApplication(
        repo_id="local", store=conn, backend=db, ingest_records=lambda r: [],
        arbitrate_command=lambda *a, **k: None, list_records=lambda a, l: [], list_decisions=lambda a, l: [],
    )
    context = SimpleNamespace(
        identity=SimpleNamespace(actor="a", environment="t", authorities=frozenset({"work:evidence", "work:read"}),
                                 principal_id="p", workspace_id="w", client_id=None, grant_id=None),
        request_id="r", basis_revision=None, catalog_revision="c", idempotency_requirement="not-allowed",
        idempotency_key=None, repo_id=None,
    )
    for operation in sorted(inbox.OPERATIONS):
        with pytest.raises(ApplicationRejection) as caught:
            app.invoke(operation, {}, context)
        assert caught.value.code == "unsupported-backend", operation
        assert "PostgreSQL" in caught.value.message
