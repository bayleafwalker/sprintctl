"""Offline integrity never upgrades supplied records to owner authority."""
from copy import deepcopy
import hashlib
import json
import socket
import sys

import pytest

from sprintctl import result_packet as packets
from sprintctl.cli import cli
from tests.test_effect_explanation_cli import owner_response as acceptance_response
from tests.test_run_brief import owner_response as continuation_response

STAMP = '2026-10-18T12:00:00+00:00'


def create(acceptance, continuation=None, **kwargs):
    return packets.create_packet(acceptance, continuation, packaged_at=STAMP, **kwargs)


def rehash(packet):
    body = {key: value for key, value in packet.items() if key != 'packet_sha256'}
    packet['packet_sha256'] = 'sha256:' + hashlib.sha256(packets.canonical(body)).hexdigest()


def test_roundtrip_preserves_complete_sources_and_separates_digest_domains(acceptance_response, continuation_response):
    original = deepcopy(acceptance_response)
    packet = create(acceptance_response, continuation_response)
    assert packet == create(acceptance_response, continuation_response)
    restored = packets.decode(packets.canonical(packet))
    report = packets.inspect_packet(restored)
    assert report['packet']['sources']['acceptance']['response'] == original == acceptance_response
    assert report['packet']['sources']['continuation']['response'] == continuation_response
    assert report['integrity']['self_consistent'] is True
    assert report['integrity']['external_expectation'] == 'unavailable'
    assert report['source_authenticity'] == 'unverified'
    assert report['cross_response_atomicity'] == 'not-established'
    assert report['release_correspondence'] == 'different'
    assert report['authorizes_action'] is False
    assert 'terminal work acceptance is not established' in report['effect_explanation']
    assert 'Source bytes: unknown' in report['continuation_brief']
    acceptance_response['snapshot']['receipt']['status'] = 'not-found'
    assert packet['sources']['acceptance']['response'] == original


def test_tamper_and_rehash_does_not_match_independent_expectation(acceptance_response):
    packet = create(acceptance_response)
    expected = packet['packet_sha256']
    packet['sources']['acceptance']['response']['snapshot']['receipt']['status'] = 'not-found'
    report = packets.inspect_packet(packet, expected_sha256=expected)
    assert report['integrity']['self_consistent'] is False
    assert report['integrity']['external_expectation'] == 'mismatch'
    rehash(packet)
    report = packets.inspect_packet(packet, expected_sha256=expected)
    assert report['integrity']['self_consistent'] is True
    assert report['integrity']['external_expectation'] == 'mismatch'
    assert report['source_authenticity'] == 'unverified'
    assert 'not found/visible for this caller' in report['effect_explanation']


def test_valid_conflicts_and_partial_pages_remain_visible(acceptance_response, continuation_response):
    acceptance_response['snapshot']['artifact_digest_matches'] = False
    acceptance_response['snapshot']['decision_facts_complete'] = False
    acceptance_response['snapshot']['receipt']['comparison'] = 'matches'
    continuation_response['delta']['reference_page_complete'] = False
    packet = create(acceptance_response, continuation_response)
    report = packets.inspect_packet(packet, expected_sha256=packet['packet_sha256'])
    assert report['integrity']['external_expectation'] == 'matches'
    assert report['release_correspondence'] == 'different'
    assert 'REQUESTED ARTIFACT: MISMATCH' in report['effect_explanation']
    assert 'artifact-check comparison: matches' in report['effect_explanation']
    assert report['packet']['sources']['continuation']['response']['delta']['reference_page_complete'] is False


@pytest.mark.parametrize('source,field,value', [('acceptance', 'repo_id', 'other'),
    ('continuation', 'repo_id', 'other')])
def test_mixed_repository_refused(acceptance_response, continuation_response, source, field, value):
    selected = acceptance_response if source == 'acceptance' else continuation_response
    selected[field] = value
    with pytest.raises(packets.PacketError, match='identities'):
        create(acceptance_response, continuation_response)


def test_mixed_item_refused(acceptance_response, continuation_response):
    continuation_response['delta']['work']['item_id'] = 8
    with pytest.raises(packets.PacketError, match='identities'):
        create(acceptance_response, continuation_response)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(schema='unsupported/v2'),
    lambda p: p.update(serializer_version=True),
    lambda p: p.update(extra='payload'),
    lambda p: p.pop('sources'),
    lambda p: p['sources']['acceptance'].update(operation='work.read.item'),
    lambda p: p['sources']['acceptance'].update(source_ref_verification='authenticated'),
    lambda p: p['sources']['acceptance']['response']['snapshot'].pop('receipt'),
    lambda p: p['sources']['acceptance']['response']['snapshot'].update(schema='unsupported/v2'),
    lambda p: p.update(limitations=[]),
    lambda p: p.update(packaged_at='2026-10-18T12:00:00'),
])
def test_malformed_or_unsupported_packets_fail(acceptance_response, mutation):
    packet = create(acceptance_response)
    mutation(packet)
    with pytest.raises(packets.PacketError):
        packets.inspect_packet(packet)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}',
    b'NaN', b'Infinity', b'1e400', b'"\\ud800"', b'\xff',
    b'{"x":' + b'1' * 5000 + b'}',
    b'[' * 100 + b'0' + b']' * 100, b'[' * 2000 + b'0' + b']' * 2000,
    b' ' * (packets.MAX_BYTES + 1)])
def test_strict_json_boundary_rejects_duplicates_nonfinite_unicode_depth_and_size(raw):
    with pytest.raises(packets.PacketError):
        packets.decode(raw)


@pytest.mark.parametrize('expected', ['x', 'a' * 64, 'sha256:' + 'A' * 64, True])
def test_expected_hash_grammar_is_not_silently_ignored(acceptance_response, expected):
    with pytest.raises(packets.PacketError):
        packets.inspect_packet(create(acceptance_response), expected_sha256=expected)


def test_metadata_is_quoted_bounded_and_never_executed(acceptance_response):
    packet = create(acceptance_response, source_ref='\x1b[31m\n' + 'x' * 400)
    text = packets.render_inspection(packets.inspect_packet(packet))
    assert '\x1b' not in text
    assert '\\u001b' in text
    assert 'excerpt; complete value retained in JSON' in text
    assert packet['sources']['acceptance']['source_ref'].endswith('x' * 400)


def test_optional_validator_has_actionable_error_without_import_side_effects(monkeypatch, acceptance_response):
    monkeypatch.setitem(sys.modules, 'jsonschema', None)
    with pytest.raises(packets.PacketError, match=r'sprintctl\[inspection\]'):
        create(acceptance_response)


def test_cli_local_utilities_do_not_resolve_backend_or_use_network(runner, tmp_path, monkeypatch, acceptance_response):
    from sprintctl import backend, db
    monkeypatch.setenv('SPRINTCTL_BACKEND', 'served')
    def forbidden(*args, **kwargs):
        pytest.fail('offline packet touched an owner, database or network')
    monkeypatch.setattr(backend, 'load_backend_config', forbidden)
    monkeypatch.setattr(db, 'get_connection', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    source = tmp_path / 'acceptance.json'
    source.write_text(json.dumps(acceptance_response))
    output = tmp_path / 'packet.json'
    result = runner.invoke(cli, ['result', 'packet', 'create', '--acceptance', str(source), '--output', str(output)])
    assert result.exit_code == 0, result.output
    digest = result.output.strip()
    inspected = runner.invoke(cli, ['result', 'packet', 'inspect', str(output), '--expected-sha256', digest, '--format', 'json'])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)['integrity']['external_expectation'] == 'matches'
    wrong = runner.invoke(cli, ['result', 'packet', 'inspect', str(output), '--expected-sha256', 'sha256:' + '0' * 64])
    assert wrong.exit_code == 1 and 'mismatch' in wrong.output
    again = runner.invoke(cli, ['result', 'packet', 'create', '--acceptance', str(source), '--output', str(output)])
    assert again.exit_code == 1
    assert output.read_text()  # Existing evidence was not truncated.


def test_bad_input_never_creates_output(runner, tmp_path):
    source = tmp_path / 'bad.json'
    source.write_bytes(b'{"x":1,"x":2}')
    output = tmp_path / 'packet.json'
    result = runner.invoke(cli, ['result', 'packet', 'create', '--acceptance', str(source), '--output', str(output)])
    assert result.exit_code == 1 and not output.exists()


def test_credential_shaped_content_is_refused_not_redacted(acceptance_response):
    acceptance_response['snapshot']['evidence_obligations'][0]['label'] = 'Bearer ' + 'a' * 40
    with pytest.raises(packets.PacketError, match='credential-shaped'):
        create(acceptance_response)


def test_failed_publication_removes_private_temporary_file(runner, tmp_path, monkeypatch, acceptance_response):
    from sprintctl.commands import result
    source = tmp_path / 'acceptance.json'
    source.write_text(json.dumps(acceptance_response))
    output = tmp_path / 'packet.json'
    def refuse(*args, **kwargs):
        raise OSError('fixture publication refused')
    monkeypatch.setattr(result.os, 'link', refuse)
    response = runner.invoke(cli, ['result', 'packet', 'create', '--acceptance', str(source), '--output', str(output)])
    assert response.exit_code == 1
    assert not output.exists()
    assert not list(tmp_path.glob('.sprintctl-packet-*'))
