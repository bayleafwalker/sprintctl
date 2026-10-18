"""Local file-only inspection of supplied owner read artifacts."""
import json
import os
from pathlib import Path
import tempfile

import click

from ..result_packet import MAX_BYTES, PacketError, create_packet, decode, inspect_packet, render_inspection


def _read(stream):
    return decode(stream.read(MAX_BYTES + 1))


@click.group('result')
def result():
    """Inspect offline owner read artifacts without importing owner state."""


@result.group('packet')
def packet():
    """Package supplied responses; integrity does not authenticate their source."""


@packet.command('create')
@click.option('--acceptance', type=click.File('rb'), required=True)
@click.option('--continuation', type=click.File('rb'))
@click.option('--source-ref', help='Optional caller-declared, unverified source reference')
@click.option('--output', type=click.Path(dir_okay=False), required=True)
def create(acceptance, continuation, source_ref, output):
    """Create a bounded local read packet; perform no owner or artifact calls."""
    try:
        value = create_packet(_read(acceptance), _read(continuation) if continuation else None,
                              source_ref=source_ref)
        # Publish only complete bytes and refuse overwriting existing evidence.
        target = Path(output)
        descriptor, temporary = tempfile.mkstemp(prefix='.sprintctl-packet-', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                from ..result_packet import canonical
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            os.unlink(temporary)
    except (PacketError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(value['packet_sha256'])


@packet.command('inspect')
@click.argument('input_file', type=click.File('rb'))
@click.option('--expected-sha256', help='Independent expectation; source/independence remains caller responsibility')
@click.option('--format', 'output_format', type=click.Choice(['text', 'json']), default='text')
def inspect(input_file, expected_sha256, output_format):
    """Inspect local packet content and optional expected canonical digest."""
    try:
        report = inspect_packet(_read(input_file), expected_sha256=expected_sha256)
    except PacketError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=True, sort_keys=True) if output_format == 'json' else render_inspection(report))
    if not report['integrity']['self_consistent'] or report['integrity']['external_expectation'] == 'mismatch':
        raise click.exceptions.Exit(1)
