"""Served failures must never render an empty detail (dogfood 2026-09-16)."""

from __future__ import annotations

import pytest

from sprintctl import cli_runtime


def _run(exc: BaseException, capsys) -> str:
    def _boom():
        raise exc

    with pytest.raises(SystemExit) as raised:
        cli_runtime._run_served("item list", _boom)
    assert raised.value.code == 1
    return capsys.readouterr().err.strip()


def test_message_detail_is_kept(capsys):
    assert _run(RuntimeError("item-not-found: Item #1 not found"), capsys) == (
        "Error: served item list failed: item-not-found: Item #1 not found"
    )


def test_empty_exception_names_its_type(capsys):
    err = _run(TimeoutError(), capsys)
    assert err == "Error: served item list failed: TimeoutError (no detail from the served client)"


def test_empty_exception_falls_back_to_its_cause(capsys):
    class TransportError(Exception):
        pass

    try:
        try:
            raise ConnectionResetError("connection reset by peer")
        except ConnectionResetError as cause:
            raise TransportError() from cause
    except TransportError as exc:
        wrapped = exc
    err = _run(wrapped, capsys)
    assert err == (
        "Error: served item list failed: TransportError: "
        "ConnectionResetError: connection reset by peer"
    )
