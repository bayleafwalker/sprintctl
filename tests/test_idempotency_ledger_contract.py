"""Oracle (agentops#2542, H1-9 / FU-4): the shared idempotency ledger protocol.

R4 lease semantics 6 on agentops#253 (normative) makes the write-tool ledger
one protocol for every record owner:

    begin(workspace_id, principal_id, tool, key, request_digest) -> LedgerEntry

keyed by ``(workspace_id, principal_id, tool, key)``.  The same tuple with the
same request digest is the same logical operation and result; the same tuple
with another digest is ``IDEMPOTENCY_KEY_REUSED``, which sprintctl keeps
publishing on the wire as ``idempotency-conflict`` (409).

This module pins, at the level that always imports (no PostgreSQL), the
typed shape sprintctl's ledger exposes in ``sprintctl.pg``:

* ``LedgerEntry`` -- an immutable value record (frozen dataclass) with at
  least ``workspace_id``, ``principal_id``, ``tool``, ``key``,
  ``request_digest``, ``result`` and ``replayed``; two entries with the same
  fields are equal (a durable ledger returns a fresh object per read, so
  callers compare by value, never by identity -- the #2530 nit);
* ``PgIdempotencyLedger(store)`` -- the sprintctl PostgreSQL ledger wrapped
  to that shape: ``begin(workspace_id, principal_id, tool, key,
  request_digest)`` and ``complete(entry, result)``;
* ``IDEMPOTENCY_KEY_REUSED == "idempotency-conflict"`` and
  ``IdempotencyConflict`` (still a ``ValueError``) carries it as ``code``.

The behaviour, including the one behaviour suite over run registration,
claim, outcome report and effect propose/accept, is pinned against
PostgreSQL in ``tests/pg/test_idempotency_ledger.py``.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from sprintctl import pg

REQUIRED_FIELDS = (
    "workspace_id", "principal_id", "tool", "key", "request_digest", "result", "replayed",
)


def _entry(**overrides) -> "pg.LedgerEntry":
    values = {
        "workspace_id": "ws-1",
        "principal_id": "github:1:0",
        "tool": "register_run",
        "key": "key-00000001",
        "request_digest": "a" * 64,
        "result": {"value": 1},
        "replayed": True,
    }
    values.update(overrides)
    return pg.LedgerEntry(**values)


class TestLedgerEntry:
    def test_ledger_entry_is_a_dataclass_with_the_protocol_fields(self):
        assert dataclasses.is_dataclass(pg.LedgerEntry)
        names = {field.name for field in dataclasses.fields(pg.LedgerEntry)}
        missing = [name for name in REQUIRED_FIELDS if name not in names]
        assert not missing, f"LedgerEntry lacks {missing}"

    def test_ledger_entry_is_immutable(self):
        entry = _entry()
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.result = {"value": 2}  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.request_digest = "b" * 64  # type: ignore[misc]

    def test_ledger_entries_compare_by_value_not_identity(self):
        first = _entry(result={"value": 1, "nested": {"a": [1, 2]}})
        second = _entry(result={"value": 1, "nested": {"a": [1, 2]}})
        assert first is not second
        assert first == second
        assert first != _entry(request_digest="b" * 64)
        assert first != _entry(replayed=False)
        assert first != _entry(principal_id="github:2:0")

    def test_the_entry_keeps_the_key_tuple_it_was_given(self):
        entry = _entry(workspace_id="ws-9", principal_id="p-9", tool="claim_work", key="k-99999999")
        assert (entry.workspace_id, entry.principal_id, entry.tool, entry.key) == (
            "ws-9", "p-9", "claim_work", "k-99999999",
        )


class TestLedgerProtocolShape:
    def test_pg_ledger_begin_takes_the_contract_arguments_in_order(self):
        begin = pg.PgIdempotencyLedger.begin
        params = [name for name in inspect.signature(begin).parameters if name != "self"]
        assert params[:5] == ["workspace_id", "principal_id", "tool", "key", "request_digest"], params
        # Nothing beyond the contract's five arguments is required.
        extra_required = [
            p.name for name, p in list(inspect.signature(begin).parameters.items())[6:]
            if p.default is inspect.Parameter.empty
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        assert not extra_required, extra_required

    def test_pg_ledger_complete_takes_an_entry_and_a_result(self):
        complete = pg.PgIdempotencyLedger.complete
        params = [name for name in inspect.signature(complete).parameters if name != "self"]
        assert params[:2] == ["entry", "result"], params

    def test_pg_ledger_wraps_a_store(self):
        store = pg.PgStore(conn=None, repo_id="repo-shape")
        ledger = pg.PgIdempotencyLedger(store)
        assert callable(ledger.begin) and callable(ledger.complete)


class TestIdempotencyKeyReused:
    def test_the_contract_name_is_published_as_idempotency_conflict(self):
        assert pg.IDEMPOTENCY_KEY_REUSED == "idempotency-conflict"

    def test_the_conflict_carries_the_published_code(self):
        exc = pg.IdempotencyConflict("this idempotency_key was already used with different arguments")
        assert isinstance(exc, ValueError)
        assert exc.code == pg.IDEMPOTENCY_KEY_REUSED == "idempotency-conflict"
