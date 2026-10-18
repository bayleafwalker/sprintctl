"""Offline read artifacts: integrity is separate from owner authenticity/authority."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64
SCHEMA = 'sprintctl-result-read-packet/v1'
OPERATIONS = {'acceptance': ('work.effect.acceptance-snapshot-v1', 'snapshot', 'sprintctl-effect-acceptance-snapshot/v1'),
              'continuation': ('work.run.continuation-delta-v1', 'delta', 'sprintctl-work-continuation-delta/v1')}
LIMITATIONS = [
    'Supplied owner responses and source references are unverified; no authority is conferred.',
    'Cross-response atomicity and currentness are not established.',
    'Terminal Decision binding, actors, attempt history, target acknowledgment and observation are not included.',
    'No owner import, execution replay, general historical transfer or external-byte custody is provided.',
    'Owner snapshot digests do not seal whole responses; packet integrity has a separate domain.',
]


class PacketError(ValueError):
    """Unsupported or malformed read artifact."""


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PacketError('packet requires finite, valid Unicode JSON') from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PacketError('duplicate JSON keys are not supported')
        result[key] = value
    return result


def _depth(value, depth=0):
    if depth > MAX_DEPTH:
        raise PacketError('JSON nesting exceeds 64 levels')
    if isinstance(value, dict):
        for key, child in value.items():
            key.encode('utf-8')
            _depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, depth + 1)
    elif isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, float) and not math.isfinite(value):
        raise PacketError('nonfinite JSON numbers are not supported')


def decode(raw: bytes) -> Any:
    if len(raw) > MAX_BYTES:
        raise PacketError('JSON input exceeds 2 MiB')
    def nonfinite(_value):
        raise PacketError('nonfinite JSON numbers are not supported')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=nonfinite)
        _depth(result)
        return result
    except PacketError:
        raise
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise PacketError('invalid UTF-8 JSON or excessive nesting') from exc


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PacketError('unsupported packet fields or missing required fields')


def _digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value)).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise PacketError('expected SHA256 must use sha256: followed by 64 lowercase hex digits')


def _timestamp(value):
    if not isinstance(value, str):
        raise PacketError('packaged_at requires a caller UTC timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise PacketError('invalid packaged_at timestamp') from exc
    if parsed.utcoffset() != timedelta(0):
        raise PacketError('packaged_at requires a caller UTC timestamp')


def _response(kind, response):
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
    except ImportError as exc:
        raise PacketError('offline packet validation requires sprintctl[inspection]') from exc
    from .vuoro_adapter import WORK_OPERATION_CONTRACTS
    operation, _key, _schema = OPERATIONS[kind]
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == operation)
    try:
        Draft202012Validator(contract.result_schema).validate(response)
    except ValidationError as exc:
        raise PacketError(f'{kind} response does not match its supported owner schema') from exc


def _source(kind, response, source_ref):
    _response(kind, response)
    operation, key, schema = OPERATIONS[kind]
    if response[key]['schema'] != schema:
        raise PacketError('unsupported owner response version')
    return {'operation': operation, 'schema': schema, 'source_ref': source_ref,
            'source_ref_verification': 'caller-declared-unverified', 'response': response}


def _validate(packet):
    _fields(packet, {'schema', 'serializer_version', 'packaged_at', 'sources', 'limitations', 'packet_sha256'})
    if packet['schema'] != SCHEMA or type(packet['serializer_version']) is not int or packet['serializer_version'] != 1:
        raise PacketError('unsupported packet version')
    _timestamp(packet['packaged_at'])
    from .contracts import reject_credential_shaped_values
    try:
        reject_credential_shaped_values(packet, 'read packet')
    except ValueError as exc:
        raise PacketError('credential-shaped content is not supported in read packets') from exc
    if packet['limitations'] != LIMITATIONS:
        raise PacketError('unsupported packet limitations')
    _sha(packet['packet_sha256'])
    sources = packet['sources']
    _fields(sources, {'acceptance', 'continuation'})
    for kind, source in sources.items():
        if source is None and kind == 'continuation':
            continue
        _fields(source, {'operation', 'schema', 'source_ref', 'source_ref_verification', 'response'})
        ref = source['source_ref']
        if ref is not None and (not isinstance(ref, str) or not ref.strip() or len(ref) > 512):
            raise PacketError('source_ref must be null or nonempty text of at most 512 characters')
        if source != _source(kind, source['response'], ref):
            raise PacketError('unsupported source operation, schema or provenance class')
    acceptance = sources['acceptance']['response']
    continuation = sources['continuation']
    if continuation is not None:
        continuation = continuation['response']
        if (continuation['repo_id'] != acceptance['repo_id'] or
            continuation['delta']['work']['item_id'] != acceptance['snapshot']['intent']['item_id']):
            raise PacketError('source repository and item identities must match')


def create_packet(acceptance: dict, continuation: dict | None = None, *,
                  source_ref: str | None = None, packaged_at: str | None = None) -> dict:
    # Round-trip through the exact JSON boundary; do not retain caller aliases.
    sources = {'acceptance': _source('acceptance', decode(canonical(acceptance)), source_ref),
               'continuation': None if continuation is None else _source('continuation', decode(canonical(continuation)), source_ref)}
    body = {'schema': SCHEMA, 'serializer_version': 1,
            'packaged_at': packaged_at or datetime.now(timezone.utc).isoformat(),
            'sources': sources, 'limitations': list(LIMITATIONS)}
    packet = {**body, 'packet_sha256': _digest(body)}
    _validate(packet)
    decode(canonical(packet))  # Combined packet, not merely each input, is bounded.
    return packet


def inspect_packet(packet: dict, *, expected_sha256: str | None = None) -> dict:
    packet = decode(canonical(packet))
    _validate(packet)
    if expected_sha256 is not None:
        _sha(expected_sha256)
    body = {key: value for key, value in packet.items() if key != 'packet_sha256'}
    computed = _digest(body)
    sources = packet['sources']
    acceptance = sources['acceptance']['response']
    snapshot = acceptance['snapshot']
    continuation = sources['continuation']['response'] if sources['continuation'] else None
    release = snapshot['current_release']
    left = release['release_digest'] if release else None
    right = continuation['delta']['current_release']['release_digest'] if continuation and continuation['delta']['current_release'] else None
    from .effect_explanation import render_effect_explanation
    from .briefing import render_run_brief
    return {'schema': 'sprintctl-result-read-inspection/v1',
            'integrity': {'self_consistent': computed == packet['packet_sha256'],
                          'computed_sha256': computed, 'recorded_sha256': packet['packet_sha256'],
                          'expected_sha256': expected_sha256,
                          'external_expectation': 'unavailable' if expected_sha256 is None else 'matches' if computed == expected_sha256 else 'mismatch',
                          'expectation_provenance': 'caller-supplied; independence is not verified'},
            'source_authenticity': 'unverified', 'cross_response_atomicity': 'not-established',
            'cross_response_currentness': 'not-established', 'authorizes_action': False,
            'repo_id': acceptance['repo_id'], 'item_id': snapshot['intent']['item_id'],
            'release_correspondence': 'unknown' if left is None or right is None else 'matches' if left == right else 'different',
            'owner_snapshot_identities': {'acceptance': snapshot['snapshot_digest'],
                                          'continuation': continuation['delta']['snapshot_digest'] if continuation else None},
            'effect_explanation': render_effect_explanation(acceptance),
            'continuation_brief': render_run_brief(continuation) if continuation else None,
            'packet': deepcopy(packet)}


def render_inspection(report: dict) -> str:
    integrity = report['integrity']
    lines = [f"READ PACKET: {report['repo_id']}#{report['item_id']}",
             f"Packet self-consistent: {integrity['self_consistent']}",
             f"Expected digest (caller-supplied; independence unverified): {integrity['external_expectation']}",
             f"Computed packet digest: {integrity['computed_sha256']}",
             'Supplied-source authenticity: unverified',
             'Cross-response atomicity/currentness: not established',
             f"Release record correspondence: {report['release_correspondence']}"]
    for kind, source in report['packet']['sources'].items():
        if source:
            ref = source['source_ref']
            excerpt = ref[:256] if ref is not None else None
            lines.append(f'{kind} caller-declared source reference: ' + json.dumps(excerpt, ensure_ascii=True) +
                         (' [excerpt; complete value retained in JSON]' if ref is not None and len(ref) > 256 else ''))
    lines.extend([report['effect_explanation']])
    if report['continuation_brief']:
        lines.append(report['continuation_brief'])
    lines.extend(report['packet']['limitations'])
    lines.append('authorizes_action: false')
    return '\n'.join(lines)
