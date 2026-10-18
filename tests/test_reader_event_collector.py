"""Synthetic streams only: no provider, reader, credential or owner operations."""
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/collect_reader_events.py'
spec = importlib.util.spec_from_file_location('reader_event_collector', SCRIPT)
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)


def encoded(*events):
    return b''.join(json.dumps(event).encode() + b'\n' for event in events)


def record(tmp_path, raw, **kwargs):
    return collector.capture(io.BytesIO(raw), tmp_path / 'capture',
                             mode='codex-documentation-v1', **kwargs)


def tool(event='item.started', id='item_1', type='command_execution', **extra):
    return {'type': event, 'item': {'id': id, 'type': type, **extra}}


# Adapted synthetic semantics from OpenAI's documented JSONL example, checked
# 2026-10-18: https://learn.chatgpt.com/docs/non-interactive-mode . These do not
# establish the installed CLI or any OpenCode transport's actual event schema.
USAGE = {'input_tokens': 5, 'cached_input_tokens': 2, 'output_tokens': 3,
         'reasoning_output_tokens': 0}


def test_raw_hash_receive_clocks_permissions_and_observed_metrics(tmp_path):
    raw = encoded({'type': 'turn.started'}, tool(), tool('item.completed'),
                  tool('item.completed', id='answer', type='agent_message', text='{}'),
                  {'type': 'turn.completed', 'usage': USAGE})
    ticks = iter(range(100, 200))
    report = record(tmp_path, raw, monotonic=lambda: next(ticks), utc=lambda: 'frozen-utc')
    folder = tmp_path / 'capture'
    assert (folder / 'raw.bin').read_bytes() == raw
    assert report['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert report['observed_tool_items']['command_execution'] == 1
    assert report['single_terminal_reported_usage'] == USAGE
    assert report['answer_text'] == '{}'
    assert report['utility_success'] and report['capture_complete']
    assert report['process_exit'] is None and report['active_effort'] is None
    assert report['complete_attempt_metrics'] is None
    assert report['reader_attempt_success'] is None
    assert not report['no_extra_reads_certified']
    assert json.loads((folder / 'summary.json').read_text()) == report
    assert json.loads((folder / 'receive-records.jsonl').read_text())['monotonic_ns'] == 101
    assert folder.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())


class TinyStream:
    def __init__(self, raw):
        self.raw = io.BytesIO(raw)

    def read(self, size):
        return self.raw.read(min(1, size))


def test_chunk_splitting_utf8_and_bounded_receive_metadata(tmp_path):
    raw = b'{"type":"thread.started","label":"\xc3\xa9"}\n'
    report = collector.capture(TinyStream(raw), tmp_path / 'capture',
                               limits=replace(collector.Limits(), receive_records=2))
    assert report['receive_reads'] == len(raw)
    assert report['receive_records'] == 2
    assert report['receive_records_omitted'] == len(raw) - 2
    assert (tmp_path / 'capture/raw.bin').read_bytes() == raw
    assert report['capture_complete']


@pytest.mark.parametrize('raw', [b'\xff\n', b'{broken}\n', b'[]\n', b'null\n',
    b'{"type":"a","type":"b"}\n', b'{"value":NaN}\n', b'{"value":Infinity}\n'])
def test_invalid_events_preserve_exact_bytes_and_fail_interpretation(tmp_path, raw):
    report = record(tmp_path, raw)
    assert report['diagnostic_count'] == 1
    assert not report['utility_success']
    assert report['capture_complete']
    assert (tmp_path / 'capture/raw.bin').read_bytes() == raw


@pytest.mark.parametrize('number', ['1e999', '-1e999'])
def test_numeric_overflow_is_invalid_even_without_nonfinite_literal(tmp_path, number):
    raw = ('{"type":"thread.started","nested":[{"value":'+number+'}]}\n').encode()
    report = record(tmp_path, raw)
    assert not report['utility_success']
    assert report['diagnostic_count'] == 1
    assert (tmp_path / 'capture/raw.bin').read_bytes() == raw


def test_depth_is_checked_before_json_decoder(tmp_path, monkeypatch):
    raw = b'{"a":' + b'[' * 65 + b'0' + b']' * 65 + b'}\n'
    original = collector.json.loads
    called = []
    def spy(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(collector.json, 'loads', spy)
    report = record(tmp_path, raw)
    assert report['diagnostic_count'] == 1
    assert not called


def test_depth_scanner_ignores_escaped_quotes_and_brackets():
    raw = json.dumps({'x': '[{' * 100 + '\\"'}).encode()
    assert collector.strict_object(raw, 2)['x'].startswith('[{')


@pytest.mark.parametrize('size', [7, 8, 9])
def test_raw_cap_does_not_probe_or_infer_remainder(tmp_path, size):
    stream = io.BytesIO(b' ' * size)
    report = collector.capture(stream, tmp_path / 'capture',
                               limits=replace(collector.Limits(), raw_bytes=8))
    assert stream.tell() == min(size, 8)
    assert report['termination'] == ('observed-eof' if size < 8 else 'raw-cap-reached')
    assert report['remainder'] == ('none-observed-eof' if size < 8 else 'unknown')
    assert not report['utility_success']


def test_unterminated_final_line_is_not_silently_complete(tmp_path):
    report = record(tmp_path, b'{"type":"thread.started"}')
    assert report['unterminated_final_line']
    assert report['capture_complete']  # EOF of stream, not valid complete JSONL.
    assert not report['utility_success']


def test_event_line_count_and_diagnostic_bounds(tmp_path):
    raw = b'x' * 50 + b'\n' + b'{}\n' * 20
    report = record(tmp_path, raw, limits=replace(collector.Limits(), line_bytes=8,
                                                events=2, diagnostics=2))
    assert len(report['diagnostics']) == 2
    assert report['diagnostic_count'] > 2
    assert report['event_lines_considered'] == 20
    assert (tmp_path / 'capture/raw.bin').read_bytes() == raw


def test_duplicate_lifecycle_is_one_item_and_retry_is_distinct(tmp_path):
    raw = encoded({'type': 'turn.started'}, tool(), tool('item.updated'),
                  tool('item.completed', status='failed'), tool('item.completed', status='failed'),
                  tool(id='retry'), tool('item.completed', id='retry'),
                  {'type': 'turn.completed', 'usage': USAGE},
                  {'type': 'turn.completed', 'usage': USAGE})
    report = record(tmp_path, raw)
    assert report['observed_tool_items']['command_execution'] == 2
    assert report['observed_terminal_events'] == 2
    assert report['single_terminal_reported_usage'] is None
    assert report['observed_tool_lifecycle'][0]['failed']
    assert not report['utility_success']


def test_item_identity_is_scoped_to_observed_turn(tmp_path):
    raw = encoded({'type':'turn.started'}, tool(),
                  {'type':'turn.completed','usage':USAGE}, {'type':'turn.started'}, tool())
    report = record(tmp_path, raw)
    assert report['observed_tool_items']['command_execution'] == 2


@pytest.mark.parametrize('event', [tool(id=None), tool(type=None), tool(id='x'*257),
    {'type':3}, {'type':'error'}, {'type':'turn.failed'},
    {'type':'turn.completed','usage':{'input_tokens':True}},
    {'type':'turn.completed','usage':{'input_tokens':-1}},
    {'type':'turn.completed','usage':{'input_tokens':1.0}}])
def test_malformed_or_failed_lifecycle_is_not_hidden(tmp_path,event):
    assert not record(tmp_path,encoded(event))['utility_success']


def test_tool_without_turn_has_unknown_attribution(tmp_path):
    report = record(tmp_path, encoded(tool()))
    assert 'tool item has no observed turn attribution' in report['diagnostics']


def test_item_limit_and_conflicting_type(tmp_path):
    raw = encoded({'type':'turn.started'}, tool(), tool(type='web_search'), tool(id='extra'))
    report = record(tmp_path, raw, limits=replace(collector.Limits(), items=1))
    assert len(report['observed_tool_lifecycle']) == 1
    assert report['diagnostics'] == ['conflicting item type', 'item tracking limit']


@pytest.mark.parametrize('messages', [ ['too big'], ['one','two'] ])
def test_answer_text_bound_and_multiple_messages(tmp_path, messages):
    raw = encoded(*(tool('item.completed',id=str(i),type='agent_message',text=m)
                    for i,m in enumerate(messages)))
    report = record(tmp_path,raw,limits=replace(collector.Limits(),answer_bytes=4))
    assert report['answer_text'] is None
    assert not report['utility_success']


def test_opaque_transport_never_invents_codex_metrics(tmp_path):
    raw = encoded(tool(), {'type':'turn.completed','usage':USAGE})
    report = collector.capture(io.BytesIO(raw),tmp_path/'capture',mode='opaque')
    assert report['observed_tool_items'] is None
    assert report['observed_terminal_events'] is None
    assert report['single_terminal_reported_usage'] is None
    assert report['capture_complete'] and report['reader_attempt_success'] is None


def test_empty_stream_is_not_successful_zero_call_reader(tmp_path):
    report = record(tmp_path,b'')
    assert not report['utility_success'] and report['reader_attempt_success'] is None


def test_read_error_retains_partial_raw_and_unknown_remainder(tmp_path):
    class Broken:
        def __init__(self): self.calls=0
        def read(self,size):
            self.calls+=1
            if self.calls==1: return b'{}\n'
            raise OSError('private source details must not be copied')
    report = collector.capture(Broken(),tmp_path/'capture')
    assert report['termination']=='capture-error' and report['remainder']=='unknown'
    assert (tmp_path/'capture/raw.bin').read_bytes()==b'{}\n'
    assert 'private' not in json.dumps(report)


@pytest.mark.parametrize('symlink',[False,True])
def test_existing_output_is_never_overwritten(tmp_path,symlink):
    target=tmp_path/'capture'
    if symlink: target.symlink_to(tmp_path,target_is_directory=True)
    else: target.mkdir()
    with pytest.raises(FileExistsError): collector.capture(io.BytesIO(b'{}\n'),target)
    assert not (tmp_path/'raw.bin').exists()


def test_summary_storage_failure_propagates_with_partial_evidence(tmp_path,monkeypatch):
    original=collector.os.open
    def fail_summary(path,*args,**kwargs):
        if Path(path).name=='summary.json': raise OSError('disk full')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(collector.os,'open',fail_summary)
    with pytest.raises(OSError): collector.capture(io.BytesIO(b'{}\n'),tmp_path/'capture')
    assert (tmp_path/'capture/raw.bin').read_bytes()==b'{}\n'
    assert not (tmp_path/'capture/summary.json').exists()


def test_completed_tool_without_start_is_retained_as_anomaly(tmp_path):
    report=record(tmp_path,encoded({'type':'turn.started'},tool('item.completed')))
    assert report['observed_tool_lifecycle'][0]['turn']==1
    assert report['observed_tool_lifecycle'][0]['item_id']=='item_1'
    assert 'tool completed without observed start' in report['diagnostics']


@pytest.mark.parametrize('events', [
    [{'type':'turn.started'},{'type':'turn.completed','usage':USAGE}],
    [tool('item.completed',type='agent_message',text='{}')],
    [{'type':'new_provider_event','secret_field':'not reinterpreted'}],
])
def test_missing_answer_or_terminal_and_unknown_event_do_not_create_attempt_success(tmp_path,events):
    report=record(tmp_path,encoded(*events))
    assert report['reader_attempt_success'] is None
    assert report['complete_attempt_metrics'] is None
    if events[0]['type']=='new_provider_event':
        assert report['unknown_event_types_or_items']==1


@pytest.mark.parametrize('limits',[collector.Limits(raw_bytes=0),collector.Limits(depth=True)])
def test_invalid_bounds_fail_before_creating_output(tmp_path,limits):
    with pytest.raises(ValueError):
        record(tmp_path,b'',limits=limits)
    assert not (tmp_path/'capture').exists()


def test_write_and_close_failures_propagate(tmp_path,monkeypatch):
    original=collector.os.fdopen
    class BrokenWriter:
        def __init__(self,file): self.file=file
        def __enter__(self): return self
        def write(self,data): raise OSError('write failed')
        def __exit__(self,*args): self.file.close()
    monkeypatch.setattr(collector.os,'fdopen',lambda *a,**k:BrokenWriter(original(*a,**k)))
    with pytest.raises(OSError): record(tmp_path,b'{}\n')
    assert not (tmp_path/'capture/summary.json').exists()


def test_main_reports_storage_failure_without_private_exception_details(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',[str(SCRIPT),'--output-dir',str(tmp_path/'capture')])
    monkeypatch.setattr(sys,'stdin',type('Input',(),{'buffer':io.BytesIO(b'{}\n')})())
    def failure(*args,**kwargs): raise OSError('private disk details')
    monkeypatch.setattr(collector,'capture',failure)
    assert collector.main()==1
    assert 'private disk details' not in capsys.readouterr().err
