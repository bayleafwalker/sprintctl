"""Private passive measurement utility; never starts a reader or proves an attempt.

Codex examples/types: https://learn.chatgpt.com/docs/non-interactive-mode
Documentation checked 2026-10-18. Fixtures are synthetic, not installed-runtime
qualification. EOF describes this stream, never process exit or active effort.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time


@dataclass(frozen=True)
class Limits:
    raw_bytes: int = 32 * 1024 * 1024
    line_bytes: int = 1024 * 1024
    events: int = 100000
    receive_records: int = 4096
    diagnostics: int = 64
    items: int = 4096
    answer_bytes: int = 64 * 1024
    depth: int = 64


def strict_object(raw, depth):
    # Bound nesting before the recursive decoder, ignoring brackets in strings.
    text = raw.decode('utf-8')
    level = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            level += 1
            if level > depth:
                raise ValueError('JSON depth limit')
        elif char in ']}':
            level -= 1

    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError('nonfinite JSON value')

    def finite_float(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError('JSON numeric overflow')
        return value

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite,
                       parse_float=finite_float)
    if not isinstance(value, dict):
        raise ValueError('event must be a JSON object')
    return value


def short_string(value, bound=256):
    return isinstance(value, str) and 0 < len(value) <= bound


class Interpretation:
    TOOL_TYPES = ('command_execution', 'mcp_tool_call', 'web_search')

    def __init__(self, mode, limits):
        self.mode, self.limits = mode, limits
        self.turn = None
        self.turns = 0
        self.items = {}
        self.terminal_count = 0
        self.usage = None
        self.answer = None
        self.answer_hash = None
        self.answer_count = 0
        self.issues = []
        self.issue_count = 0
        self.unknown_events = 0

    def issue(self, message):
        self.issue_count += 1
        if len(self.issues) < self.limits.diagnostics:
            self.issues.append(message[:256])

    def event(self, event):
        if self.mode == 'opaque':
            return
        kind = event.get('type')
        if not short_string(kind, 128):
            self.issue('missing or invalid event type')
        elif kind == 'turn.started':
            if self.turn is not None:
                self.issue('turn started before observed terminal boundary')
            self.turns += 1
            self.turn = self.turns
        elif kind in ('turn.completed', 'turn.failed'):
            self.terminal_count += 1
            if self.turn is None:
                self.issue('terminal event without observed active turn')
            if self.terminal_count > 1:
                self.issue('multiple terminal events; attempt usage unavailable')
                self.usage = None
            elif kind == 'turn.completed':
                usage = event.get('usage')
                fields = ('input_tokens', 'cached_input_tokens', 'output_tokens',
                          'reasoning_output_tokens')
                if (isinstance(usage, dict) and usage
                        and set(usage) <= set(fields)
                        and all(type(v) is int and 0 <= v <= 2**63-1
                                for v in usage.values())):
                    self.usage = usage
                else:
                    self.issue('missing or invalid terminal usage')
            else:
                self.issue('observed failed turn')
            self.turn = None
        elif kind in ('item.started', 'item.updated', 'item.completed'):
            item = event.get('item')
            if not isinstance(item, dict) or not short_string(item.get('id')):
                self.issue('missing or invalid item identity')
                return
            item_type = item.get('type')
            if not short_string(item_type, 128):
                self.issue('missing or invalid item type')
                return
            if item_type == 'agent_message' and kind == 'item.completed':
                text = item.get('text')
                if not isinstance(text, str):
                    self.issue('invalid agent message text')
                    return
                try:
                    raw = text.encode('utf-8')
                except UnicodeError:
                    self.issue('invalid agent message Unicode')
                    return
                self.answer_count += 1
                self.answer_hash = hashlib.sha256(raw).hexdigest()
                if len(raw) <= self.limits.answer_bytes and self.answer_count == 1:
                    self.answer = text
                else:
                    self.answer = None
                    self.issue('oversized or multiple agent messages; answer unavailable')
            elif item_type in self.TOOL_TYPES:
                key = (self.turn, item['id'])
                if key not in self.items:
                    if len(self.items) >= self.limits.items:
                        self.issue('item tracking limit')
                        return
                    self.items[key] = {'type': item_type, 'turn': self.turn,
                                       'item_id': item['id'], 'started': False,
                                       'completed': False, 'failed': False}
                row = self.items[key]
                if row['type'] != item_type:
                    self.issue('conflicting item type')
                    return
                row['started'] |= kind == 'item.started'
                row['completed'] |= kind == 'item.completed'
                row['failed'] |= item.get('status') == 'failed'
                if kind == 'item.completed' and not row['started']:
                    self.issue('tool completed without observed start')
                if self.turn is None:
                    self.issue('tool item has no observed turn attribution')
            elif item_type not in ('reasoning', 'file_change', 'plan', 'agent_message'):
                self.unknown_events += 1
        elif kind == 'error':
            self.issue('observed error event')
        elif kind != 'thread.started':
            self.unknown_events += 1

    def report(self):
        return {
            'observed_tool_items': ({t: sum(r['type'] == t for r in self.items.values())
                                     for t in self.TOOL_TYPES}
                                    if self.mode != 'opaque' else None),
            'observed_tool_lifecycle': list(self.items.values()),
            'observed_terminal_events': self.terminal_count if self.mode != 'opaque' else None,
            'single_terminal_reported_usage': self.usage,
            'observed_agent_messages': self.answer_count if self.mode != 'opaque' else None,
            'answer_text': self.answer, 'last_message_sha256': self.answer_hash,
            'unknown_event_types_or_items': self.unknown_events if self.mode != 'opaque' else None,
            'diagnostics': self.issues, 'diagnostic_count': self.issue_count,
            'complete_attempt_metrics': None, 'active_effort': None,
            'process_exit': None, 'startup_vs_reader_reads': None,
            'no_extra_reads_certified': False,
        }


def capture(stream, output, *, mode='opaque', limits=Limits(),
            monotonic=time.monotonic_ns,
            utc=lambda: datetime.now(timezone.utc).isoformat()):
    """Capture bounded bytes. Storage failures propagate; partial files may remain."""
    if mode not in ('opaque', 'codex-documentation-v1'):
        raise ValueError('unsupported interpretation')
    if any(type(v) is not int or v <= 0 for v in asdict(limits).values()):
        raise ValueError('limits must be positive integers')
    output = Path(output)
    output.mkdir(mode=0o700)  # Existing paths/symlinks are never reused.
    interpretation = Interpretation(mode, limits)
    total = reads = receive_count = lines = 0
    pending = bytearray()
    discarding = False
    termination = None
    raw_hash = hashlib.sha256()
    started = monotonic()

    def line(raw):
        nonlocal lines
        lines += 1
        if lines > limits.events:
            if lines == limits.events + 1:
                interpretation.issue('event interpretation count limit')
            return
        try:
            interpretation.event(strict_object(raw, limits.depth))
        except (ValueError, UnicodeError, RecursionError):
            interpretation.issue('invalid JSON event')

    def private_file(name):
        return os.fdopen(os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600), 'wb')

    with private_file('raw.bin') as raw_file, private_file('receive-records.jsonl') as records:
        while total < limits.raw_bytes:
            try:
                chunk = stream.read(min(8192, limits.raw_bytes - total))
                if not isinstance(chunk, bytes):
                    raise TypeError('stream must yield bytes')
                if len(chunk) > min(8192, limits.raw_bytes - total):
                    raise ValueError('stream exceeded requested read bound')
            except (OSError, ValueError, TypeError):
                termination = 'capture-error'
                interpretation.issue('input stream read failed')
                break
            if not chunk:
                termination = 'observed-eof'
                break
            received_ns, received_utc = monotonic(), utc()
            raw_file.write(chunk)
            raw_hash.update(chunk)
            total += len(chunk)
            reads += 1
            if receive_count < limits.receive_records:
                record = {'end_offset': total, 'monotonic_ns': received_ns,
                          'utc': received_utc}
                # Clocks are injected only for tests; production values are bounded.
                if type(received_ns) is not int or not isinstance(received_utc, str) or len(received_utc) > 128:
                    raise ValueError('invalid receive clock')
                records.write((json.dumps(record) + '\n').encode())
                receive_count += 1
            for byte in chunk:
                if byte == 10:
                    if not discarding:
                        line(bytes(pending))
                    pending.clear()
                    discarding = False
                elif not discarding:
                    if len(pending) == limits.line_bytes:
                        pending.clear()
                        discarding = True
                        interpretation.issue('event line byte limit')
                    else:
                        pending.append(byte)
        else:
            termination = 'raw-cap-reached'
        unterminated = bool(pending) or discarding
        if pending:
            line(bytes(pending))
        if unterminated:
            interpretation.issue('unterminated final event line')
    ended = monotonic()
    summary = {
        'interpretation': mode, 'limits': asdict(limits), 'termination': termination,
        'remainder': 'none-observed-eof' if termination == 'observed-eof' else 'unknown',
        'captured_bytes': total, 'raw_sha256': raw_hash.hexdigest(),
        'receive_reads': reads, 'receive_records': receive_count,
        'receive_records_omitted': reads - receive_count,
        'event_lines_considered': lines, 'unterminated_final_line': unterminated,
        'observed_stream_duration_ns': ended - started,
        'capture_complete': termination == 'observed-eof',
        'reader_attempt_success': None,
        **interpretation.report(),
    }
    summary['utility_success'] = (summary['capture_complete'] and total > 0
                                  and interpretation.issue_count == 0)
    with private_file('summary.json') as result:
        result.write((json.dumps(summary, indent=2, ensure_ascii=True) + '\n').encode())
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--interpretation', choices=('opaque', 'codex-documentation-v1'),
                        default='opaque')
    args = parser.parse_args()
    try:
        report = capture(sys.stdin.buffer, args.output_dir, mode=args.interpretation)
    except (OSError, ValueError) as error:
        print(f'Capture failed ({type(error).__name__}); partial evidence may remain.',
              file=sys.stderr)
        return 1
    return 0 if report['utility_success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
