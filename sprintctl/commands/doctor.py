"""Diagnostic command registration extracted from :mod:`sprintctl.cli`."""

import click

from .. import doctor as _doctor


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit deterministic JSON diagnostics")
def doctor_cmd(as_json: bool) -> None:
    """Diagnose install provenance, extras, backend config, and schema compatibility."""
    report = _doctor.collect_report()
    click.echo(_doctor.dumps(report) if as_json else _doctor.render_text(report))
    if report["status"] == "error":
        raise click.exceptions.Exit(1)


def register(root: click.Group) -> None:
    root.add_command(doctor_cmd)
