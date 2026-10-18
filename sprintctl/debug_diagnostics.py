"""Best-effort human diagnostics for an invocation's ordinary local store.

No SQL trace, owner calls, repair, or machine-readable diagnostic contract.
Elapsed scope is the root callback through cleanup, including later parsing.
Every diagnostic boundary is isolated from the primary command's outcome.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import click

_STATE_KEY = '_sprintctl_local_debug'
_SCHEMA_READ = 'SELECT version FROM schema_version ORDER BY rowid LIMIT 1'


def _clock_ns() -> int:
    return time.perf_counter_ns()


def _safe_clock() -> int | None:
    try:
        value = _clock_ns()
        return value if type(value) is int and value >= 0 else None
    except Exception:
        return None


def _read_schema(connection) -> int | None:
    cursor = connection.execute(_SCHEMA_READ)
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    value = row[0] if row is not None else None
    return value if type(value) is int and value >= 0 else None


def _format_summary(elapsed_ns: int | None, path: str | None, schema: int | None) -> str:
    elapsed = f'{elapsed_ns / 1_000_000:.3f}ms' if elapsed_ns is not None else 'unknown'
    if path is None:
        location = 'unknown'
    else:
        location = json.dumps(path[:256], ensure_ascii=True)
        if len(path) > 256:
            location += ' [truncated]'
    return f'debug local: elapsed={elapsed}; db={location}; schema={schema if schema is not None else "unknown"}'


def _write_summary(text: str) -> None:
    stream = click.get_text_stream('stderr')
    try:
        descriptor = stream.fileno()
    except (AttributeError, io.UnsupportedOperation):
        # In-memory Click/test streams have no descriptor or shutdown flush.
        click.echo(text, file=stream)
    else:
        # Do not leave buffered text for interpreter shutdown to flush again:
        # a closed pipe must not turn a successful command into exit 120.
        os.write(descriptor, (text + '\n').encode('ascii'))


class _Invocation:
    def __init__(self) -> None:
        self.started = _safe_clock()
        self.captured = False
        self.excluded = False
        self.path: str | None = None
        self.schema: int | None = None

    def capture(self, connection, path: Path | None) -> None:
        context = click.get_current_context(silent=True)
        while context is not None:
            if context.params.get('project_path') is not None:
                self.excluded = True
                return
            context = context.parent
        if self.captured:
            return
        self.captured = True
        try:
            self.path = str(path.resolve())
        except Exception:
            pass
        try:
            self.schema = _read_schema(connection)
        except Exception:
            pass

    def finish(self) -> None:
        if not self.captured or self.excluded:
            return
        ended = _safe_clock()
        elapsed = None
        if self.started is not None and ended is not None and ended >= self.started:
            elapsed = ended - self.started
        try:
            _write_summary(_format_summary(elapsed, self.path, self.schema))
        except Exception:
            # A broken diagnostic stream/formatter must not replace an exit,
            # exception, or successful result from the primary command.
            pass


def install(context: click.Context) -> None:
    """Start only when opted in; do not resolve a backend or open storage."""
    try:
        context.obj.pop(_STATE_KEY, None)
        if os.environ.get('SPRINTCTL_DEBUG') != '1':
            return
        state = _Invocation()
        context.obj[_STATE_KEY] = state
        context.call_on_close(state.finish)
    except Exception:
        pass


def capture_local(obj: dict, connection, path: Path | None) -> None:
    """Use only the initialized connection already opened by _get_store."""
    try:
        state = obj.get(_STATE_KEY)
        if isinstance(state, _Invocation):
            state.capture(connection, path)
    except Exception:
        pass
