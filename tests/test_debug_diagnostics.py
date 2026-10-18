"""Observe real CLI outcomes; diagnostics must not acquire owner effects."""
import importlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from sprintctl import db
from sprintctl import cli as cli_module
from sprintctl.cli import cli

PREFIX = 'debug local:'
SCHEMA_READ = 'SELECT version FROM schema_version ORDER BY rowid LIMIT 1'


def debug_module():
    return importlib.import_module('sprintctl.debug_diagnostics')


def summary(result):
    lines = [line for line in result.stderr.splitlines() if line.startswith(PREFIX)]
    assert len(lines) == 1, result.stderr
    return lines[0]


def without_summary(result):
    return ''.join(line for line in result.stderr.splitlines(keepends=True) if not line.startswith(PREFIX))


@pytest.fixture
def item_id(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint['id'], 'debug')
    return db.create_work_item(conn, active_sprint['id'], track, 'PRIVATE_TITLE_918', description='PRIVATE_DESCRIPTION_918')


def invoke_pair(runner, args):
    off = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '0'})
    on = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '1'})
    assert on.exit_code == off.exit_code
    assert on.stdout == off.stdout
    assert without_summary(on) == off.stderr
    return off, on


def test_actual_local_read_keeps_json_and_adds_only_stderr(runner, item_id, db_path):
    off, on = invoke_pair(runner, ['item', 'show', '--id', str(item_id), '--json'])
    assert on.exit_code == 0
    assert json.loads(on.stdout)['item']['title'] == 'PRIVATE_TITLE_918'
    line = summary(on)
    assert f'schema={db.CURRENT_SCHEMA_VERSION}' in line
    assert json.dumps(str(db_path.resolve())) in line
    assert 'elapsed=' in line
    assert 'PRIVATE_TITLE_918' not in line


def test_actual_refusal_keeps_error_and_exit(runner, conn):
    off, on = invoke_pair(runner, ['item', 'show', '--id', '987654'])
    assert off.exit_code == on.exit_code == 1
    assert 'not found' in off.stderr
    summary(on)


@pytest.mark.parametrize('value', [None, '0', 'true', '2', ' 1'])
def test_disabled_mode_never_reads_clock_or_metadata(runner, item_id, monkeypatch, value):
    mod = debug_module()
    calls = []
    def forbidden(*args):
        calls.append(args)
        raise AssertionError('disabled instrumentation ran')
    monkeypatch.setattr(mod, '_clock_ns', forbidden)
    monkeypatch.setattr(mod, '_read_schema', forbidden)
    env = {'SPRINTCTL_DEBUG': value}
    result = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], env=env)
    assert result.exit_code == 0
    assert result.stderr == ''
    assert calls == []


def test_clock_has_exact_start_and_finalization_scope(runner, item_id, monkeypatch):
    mod = debug_module()
    ticks = iter([1_000_000, 14_250_000])
    calls = []
    def clock():
        value = next(ticks); calls.append(value); return value
    monkeypatch.setattr(mod, '_clock_ns', clock)
    result = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert 'elapsed=13.250ms' in summary(result)
    assert calls == [1_000_000, 14_250_000]


@pytest.mark.parametrize('ticks', [[RuntimeError('CLOCK_SECRET'), 100], [OSError('CLOCK_SECRET'), 100], [0, RuntimeError('CLOCK_SECRET')], [0, OSError('CLOCK_SECRET')], [100, 99]])
def test_failed_or_negative_clock_is_unknown_not_an_error(runner, item_id, monkeypatch, ticks):
    mod = debug_module(); values = iter(ticks)
    def clock():
        value = next(values)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(mod, '_clock_ns', clock)
    result = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert 'elapsed=unknown' in summary(result)
    assert 'CLOCK_SECRET' not in result.stderr


@pytest.mark.parametrize('failure', [RuntimeError('SCHEMA_SECRET'), OSError('SCHEMA_SECRET')])
def test_metadata_failure_is_unknown_and_preserves_actual_primary_refusal(runner, conn, monkeypatch, failure):
    mod = debug_module()
    def fail(*args): raise failure
    monkeypatch.setattr(mod, '_read_schema', fail)
    result = runner.invoke(cli, ['item', 'show', '--id', '987654'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 1
    assert 'not found' in without_summary(result)
    assert 'schema=unknown' in summary(result)
    assert 'SCHEMA_SECRET' not in result.stderr


@pytest.mark.parametrize('hook', ['_format_summary', '_write_summary'])
@pytest.mark.parametrize('failure', [RuntimeError('DIAGNOSTIC_SECRET'), OSError('DIAGNOSTIC_SECRET'), BrokenPipeError('DIAGNOSTIC_SECRET')])
@pytest.mark.parametrize('missing', [False, True])
def test_format_and_output_failures_never_mask_primary_outcome(runner, item_id, monkeypatch, hook, failure, missing):
    mod = debug_module()
    args = ['item', 'show', '--id', str(987654 if missing else item_id), '--json']
    before = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '0'})
    def fail(*args): raise failure
    monkeypatch.setattr(mod, hook, fail)
    after = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '1'})
    assert (after.exit_code, after.stdout, after.stderr) == (before.exit_code, before.stdout, before.stderr)
    assert 'DIAGNOSTIC_SECRET' not in after.stderr


def test_only_one_fixed_extra_read_and_no_extra_owner_effects(runner, item_id, db_path, monkeypatch):
    original = db.get_connection
    traces = []
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(traces[-1].append)
        return connection
    monkeypatch.setattr(db, 'get_connection', connect)
    for enabled in ['0', '1']:
        traces.append([])
        result = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], env={'SPRINTCTL_DEBUG': enabled})
        assert result.exit_code == 0
    off, on = traces
    assert len(on) == len(off) + 1
    assert on.count(SCHEMA_READ) == off.count(SCHEMA_READ) + 1
    assert any(on[:i] + on[i + 1:] == off for i, sql in enumerate(on) if sql == SCHEMA_READ)
    # A real primary mutation commits once in each mode; debug must not add an event.
    with sqlite3.connect(db_path) as c:
        before = c.execute('select count(*) from event').fetchone()[0]
    for enabled in ['0', '1']:
        traces.append([])
        result = runner.invoke(cli, ['item', 'note', '--id', str(item_id), '--type', 'update', '--summary', 'PRIVATE_NOTE_918', '--actor', 'synthetic', '--json'], env={'SPRINTCTL_DEBUG': enabled})
        assert result.exit_code == 0
        if enabled == '1': assert 'PRIVATE_NOTE_918' not in summary(result)
    with sqlite3.connect(db_path) as c:
        rows = c.execute('select payload from event order by id').fetchall()
    assert len(rows) == before + 2
    assert sum('PRIVATE_NOTE_918' in row[0] for row in rows) == 2
    assert traces[-1].count('COMMIT') == traces[-2].count('COMMIT')
    assert sorted(p.name for p in db_path.parent.iterdir() if p.name.startswith('sync-')) == []


def test_reused_local_store_captures_once_and_caller_write_stays_uncommitted(runner, conn, monkeypatch):
    statements = []; conn.set_trace_callback(statements.append)
    conn.execute("insert into sprint(name,status,aggregate_uuid) values ('caller provisional','planned','11111111-1111-4111-8111-111111111111')")
    @click.command('debug-test-reuse')
    @click.pass_obj
    def command(obj):
        cli_module._get_store(obj); cli_module._get_store(obj)
        click.echo('primary')
    monkeypatch.setitem(cli.commands, command.name, command)
    result = runner.invoke(cli, [command.name], obj={'conn': conn}, env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert result.stdout == 'primary\n'
    summary(result)
    assert statements.count(SCHEMA_READ) == 1
    assert conn.in_transaction
    assert 'COMMIT' not in statements and 'ROLLBACK' not in statements
    conn.rollback()
    assert conn.execute("select count(*) from sprint where name='caller provisional'").fetchone()[0] == 0


def test_private_values_never_enter_diagnostic_stderr(runner, item_id, monkeypatch):
    for name in ['FJ_TOKEN', 'SPRINTCTL_URL', 'SPRINTCTL_VUORO_PROFILE', 'PRIVATE_ENV']:
        monkeypatch.setenv(name, 'SYNTHETIC_SECRET_' + name)
    result = runner.invoke(cli, ['item', 'note', '--id', str(item_id), '--type', 'update', '--summary', 'PRIVATE_ARG_AND_BOUND_NOTE_918', '--actor', 'PRIVATE_ACTOR_918', '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert 'PRIVATE_ARG_AND_BOUND_NOTE_918' in result.stdout
    assert 'PRIVATE_' not in result.stderr and 'SYNTHETIC_SECRET_' not in result.stderr
    summary(result)


def test_hostile_database_path_is_escaped_and_bounded(runner, tmp_path, monkeypatch):
    path = tmp_path / ('d' * 200) / ('e' * 120) / 'line\n\x1b[31m"\u202e.db'
    monkeypatch.setenv('SPRINTCTL_DB', str(path))
    result = runner.invoke(cli, ['sprint', 'create', '--name', 'test', '--status', 'active'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    line = summary(result)
    assert len(line) < 1800
    assert 'truncated' in line
    assert '\x1b' not in line and '\u202e' not in line
    short = tmp_path / 'line\n\x1b[31m"\u202e.db'
    monkeypatch.setenv('SPRINTCTL_DB', str(short))
    result = runner.invoke(cli, ['sprint', 'create', '--name', 'test', '--status', 'active'], env={'SPRINTCTL_DEBUG': '1'})
    line = summary(result)
    assert '\\n' in line and '\\u001b' in line and '\\u202e' in line and '\\"' in line


@pytest.mark.parametrize('args', [['--help'], ['--version'], ['git-context', '--json'], ['event', 'list', '--sprint-id', '1', '--unknown']])
def test_non_store_paths_open_nothing_extra(runner, monkeypatch, args):
    before = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '0'})
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError('diagnostic attempted a store or owner call')
    monkeypatch.setattr(db, 'get_connection', forbidden)
    monkeypatch.setattr(cli_module._served, 'read_item', forbidden)
    after = runner.invoke(cli, args, env={'SPRINTCTL_DEBUG': '1'})
    assert (after.exit_code, after.stdout, after.stderr) == (before.exit_code, before.stdout, before.stderr)
    assert attempts == []


def test_served_path_has_only_the_existing_read_and_no_debug_output(runner, monkeypatch):
    config = SimpleNamespace(mode='served', repo_id='synthetic', repo_source='flag', served_profile=SimpleNamespace(endpoint='https://synthetic.invalid'))
    loads = []
    def load(**kwargs): loads.append(kwargs); return config
    monkeypatch.setattr(cli_module._backend, 'load_backend_config', load)
    calls = []
    payload = {'item': {'id': 1, 'status': 'pending'}, 'events': [], 'active_reservations': [], 'refs': [], 'deps': {'blocked_by': [], 'blocks': []}}
    def read(*args, **kwargs): calls.append(kwargs); return payload
    monkeypatch.setattr(cli_module._served, 'read_item', read)
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError('diagnostic local connection or metadata read in served mode')
    monkeypatch.setattr(db, 'get_connection', forbidden)
    mod = debug_module(); monkeypatch.setattr(mod, '_read_schema', forbidden)
    monkeypatch.setattr(cli_module._served, '_invoke_operation', forbidden)
    monkeypatch.setattr(cli_module._served, 'resolve_file_credential', forbidden)
    before = runner.invoke(cli, ['item', 'show', '--id', '1', '--json'], env={'SPRINTCTL_DEBUG': '0'})
    after = runner.invoke(cli, ['item', 'show', '--id', '1', '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert before.exit_code == after.exit_code == 0
    assert (before.stdout, before.stderr) == (after.stdout, after.stderr)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(loads) == 2 and loads[0] == loads[1]
    assert attempts == []


def test_project_single_member_view_is_not_instrumented(runner, conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINTCTL_REPO_ID", "synthetic")
    path = tmp_path / 'project.toml'
    path.write_text('schema_version=1\nproject_id="981b2073-d7af-4c28-bff3-3cf807495fba"\ndisplay_name="test"\nhome_repo="synthetic"\n[[members]]\nrepo_id="synthetic"\nbacklog=true\nrender="none"\n')
    off, on = invoke_pair(runner, ['item', 'list', '--project', str(path), '--json'])
    assert off.exit_code == on.exit_code == 0
    assert on.stderr == ''


def test_timing_begins_before_open_and_ends_after_connection_close(runner, item_id, monkeypatch):
    mod = debug_module(); phases = []
    original = db.get_connection
    class ObservedConnection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        def close(self):
            phases.append('close')
            self.connection.close()
    def connect(*args, **kwargs):
        phases.append('open')
        return ObservedConnection(original(*args, **kwargs))
    def clock():
        phases.append('clock')
        return len(phases) * 1_000_000
    monkeypatch.setattr(db, 'get_connection', connect)
    monkeypatch.setattr(mod, '_clock_ns', clock)
    result = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert phases == ['clock', 'open', 'close', 'clock']
    assert 'elapsed=3.000ms' in summary(result)


def test_debug_output_failure_does_not_undo_primary_write(runner, item_id, conn, monkeypatch):
    mod = debug_module()
    before = conn.execute('select count(*) from event').fetchone()[0]
    def fail(*args): raise RuntimeError('OUTPUT_SECRET')
    monkeypatch.setattr(mod, '_write_summary', fail)
    result = runner.invoke(cli, ['item', 'note', '--id', str(item_id), '--type', 'update', '--summary', 'committed-primary', '--actor', 'synthetic', '--json'], env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert result.stderr == ''
    assert json.loads(result.stdout)['summary'] == 'committed-primary'
    assert conn.execute('select count(*) from event').fetchone()[0] == before + 1


def test_reusing_an_object_with_debug_off_does_not_reuse_diagnostic_state(runner, item_id, conn, monkeypatch):
    mod = debug_module(); obj = {'conn': conn}
    first = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], obj=obj, env={'SPRINTCTL_DEBUG': '1'})
    assert first.exit_code == 0
    summary(first)
    calls = []
    def forbidden(*args):
        calls.append(args)
        raise AssertionError('stale metadata collector ran')
    monkeypatch.setattr(mod, '_read_schema', forbidden)
    monkeypatch.setattr(mod, '_clock_ns', forbidden)
    second = runner.invoke(cli, ['item', 'show', '--id', str(item_id), '--json'], obj=obj, env={'SPRINTCTL_DEBUG': '0'})
    assert second.exit_code == 0
    assert second.stdout == first.stdout and second.stderr == ''
    assert calls == []


def test_real_closed_stderr_pipe_cannot_change_success_exit(runner, item_id, db_path, tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith('SPRINTCTL_')}
    env.update(PYTHONPATH=str(root), SPRINTCTL_BACKEND='local', SPRINTCTL_DB=str(db_path))
    results = []
    for setting in ['0', '1']:
        child = subprocess.Popen([sys.executable, '-m', 'sprintctl', 'item', 'show', '--id', str(item_id), '--json'], cwd=tmp_path, env=dict(env, SPRINTCTL_DEBUG=setting), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stderr.close()
            child.stderr = None
            output, _ = child.communicate(timeout=20)
            code = child.returncode
        finally:
            if child.poll() is None:
                child.kill(); child.communicate(timeout=5)
        results.append((code, output))
    assert results[0][0] == 0
    assert results[1] == results[0]


def test_root_timer_includes_later_subcommand_argument_parsing(runner, conn, monkeypatch):
    mod = debug_module(); phases = []
    def clock():
        phases.append('clock')
        return len(phases) * 1_000_000
    def parse(ctx, param, value):
        phases.append('subcommand-parse')
        return value
    @click.command('debug-test-parse')
    @click.option('--synthetic-value', callback=parse)
    @click.pass_obj
    def command(obj, synthetic_value):
        phases.append('primary')
        cli_module._get_store(obj)
        click.echo('primary')
    monkeypatch.setitem(cli.commands, command.name, command)
    monkeypatch.setattr(mod, '_clock_ns', clock)
    result = runner.invoke(cli, [command.name, '--synthetic-value', 'PRIVATE_ARG'], obj={'conn': conn}, env={'SPRINTCTL_DEBUG': '1'})
    assert result.exit_code == 0
    assert phases == ['clock', 'subcommand-parse', 'primary', 'clock']
    assert 'elapsed=3.000ms' in summary(result)
    assert 'PRIVATE_ARG' not in result.stderr
