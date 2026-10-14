"""``sprintctl agent``, ``sprintctl message`` and ``sprintctl run``: thin served verbs.

Each command sends one catalog operation through the served client and prints
its JSON result.  The agent directory and inbox (vuoro contract v1) and the
run continuation reads exist only on the PostgreSQL owner, so every verb here
requires ``SPRINTCTL_BACKEND=served``; nothing is computed locally.  Records
that the contract defines as JSON objects (a declaration, an envelope, a
receipt's own fields) are read from ``--json FILE`` (``-`` for stdin).
A refusal prints ``{"error": {"code", "message"}}`` and exits 1.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from .. import served as _served
from ..cli_runtime import _served_config_or_none


def _json_input(stream: Any, what: str) -> Any:
    try:
        return json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"{what} is not valid JSON: {exc}") from exc


def _invoke(operation: str, arguments: dict[str, Any]) -> None:
    obj = click.get_current_context().find_root().obj
    config = _served_config_or_none(obj)
    if config is None:
        raise click.ClickException(
            f"{operation} is served by the PostgreSQL owner only; set SPRINTCTL_BACKEND=served"
        )
    from vuoro_client.errors import InvocationRejectedError  # served extra, lazy

    try:
        result = _served.invoke_agent_operation(
            config.served_profile, operation, arguments, repo_id=config.repo_id
        )
    except InvocationRejectedError as exc:
        message = str(exc).partition(": ")[2] or str(exc)
        click.echo(json.dumps({"error": {"code": exc.code, "message": message}}, indent=2))
        sys.exit(1)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


# -- agent ------------------------------------------------------------------

@click.group("agent")
def agent() -> None:
    """Agent directory: register, observe and retire instances; list facts."""


@agent.command("register")
@click.option("--instance-id", required=True, help="ins_<ULID> of this runtime installation")
@click.option("--json", "declaration", type=click.File("r"), default="-", show_default=True,
              help="agent-declaration/v1 JSON file, - for stdin")
@click.option("--idempotency-key", required=True)
def agent_register(instance_id: str, declaration, idempotency_key: str) -> None:
    """Register an instance or append a declaration revision."""
    _invoke("work.agent.register-v1", {
        "instance_id": instance_id, "declaration": _json_input(declaration, "declaration"),
        "idempotency_key": idempotency_key,
    })


@agent.command("observe")
@click.option("--instance-id", required=True)
@click.option("--fact", required=True, type=click.Choice(["observed", "reachable", "executing", "attended"]))
@click.option("--channel", default=None, help="Required for reachable")
@click.option("--run-id", default=None, help="Required for executing")
@click.option("--valid-for-seconds", required=True, type=int)
@click.option("--idempotency-key", required=True)
def agent_observe(instance_id, fact, channel, run_id, valid_for_seconds, idempotency_key) -> None:
    """Record one self-reported observation; the owner stamps its time."""
    _invoke("work.agent.observe-v1", _present(
        instance_id=instance_id, fact=fact, channel=channel, run_id=run_id,
        valid_for_seconds=valid_for_seconds, idempotency_key=idempotency_key,
    ))


@agent.command("retire")
@click.option("--instance-id", required=True)
@click.option("--reason", required=True)
@click.option("--idempotency-key", required=True)
def agent_retire(instance_id: str, reason: str, idempotency_key: str) -> None:
    """Retire an instance; it stays listed with its facts."""
    _invoke("work.agent.retire-v1", {
        "instance_id": instance_id, "reason": reason, "idempotency_key": idempotency_key,
    })


@agent.command("list")
@click.option("--active-only", is_flag=True, default=False, help="Leave retired instances out")
def agent_list(active_only: bool) -> None:
    """Directory entries of this repository, facts evaluated now."""
    _invoke("work.agent.list-v1", {"include_retired": not active_only})


@agent.command("describe")
@click.option("--principal-id", required=True)
def agent_describe(principal_id: str) -> None:
    """One principal's directory entry."""
    _invoke("work.agent.describe-v1", {"principal_id": principal_id})


# -- message ----------------------------------------------------------------

@click.group("message")
def message() -> None:
    """Durable inbox: send, read, receipts and cancellation."""


@message.command("send")
@click.option("--json", "envelope", type=click.File("r"), default="-", show_default=True,
              help="agent-message/v1 envelope JSON file, - for stdin")
def message_send(envelope) -> None:
    """Send an envelope; resending the same message_id replays its stamps."""
    _invoke("work.message.send-v1", {"message": _json_input(envelope, "envelope")})


@message.command("read")
@click.option("--after-seq", default=0, show_default=True, type=int)
@click.option("--limit", default=100, show_default=True, type=int)
@click.option("--instance-id", default=None, help="Include messages targeted at this instance")
def message_read(after_seq: int, limit: int, instance_id: str | None) -> None:
    """Messages addressed to the caller after a cursor; keep next_after_seq."""
    _invoke("work.message.read-v1", _present(after_seq=after_seq, limit=limit, instance_id=instance_id))


@message.command("status")
@click.option("--message-id", required=True)
def message_status(message_id: str) -> None:
    """An envelope and every receipt (sender or recipient only)."""
    _invoke("work.message.status-v1", {"message_id": message_id})


@message.command("receipt")
@click.option("--message-id", required=True)
@click.option("--instance-id", required=True)
@click.option("--kind", required=True)
@click.option("--json", "fields", type=click.File("r"), default=None,
              help="JSON object of the kind's own fields (reason, run_id, detail, outcome, ...)")
def message_receipt(message_id: str, instance_id: str, kind: str, fields) -> None:
    """Record a receipt; the same receipt again replays."""
    extra = _json_input(fields, "receipt fields") if fields is not None else {}
    if not isinstance(extra, dict):
        raise click.ClickException("receipt fields must be a JSON object")
    _invoke("work.message.receipt-v1", {
        **extra, "message_id": message_id, "instance_id": instance_id, "kind": kind,
    })


@message.command("cancel")
@click.option("--message-id", required=True)
@click.option("--reason", required=True)
def message_cancel(message_id: str, reason: str) -> None:
    """Request cancellation (sender only); a repeat returns the first."""
    _invoke("work.message.cancel-v1", {"message_id": message_id, "reason": reason})


# -- run --------------------------------------------------------------------

@click.group("run")
def run() -> None:
    """Run continuation: read a predecessor's context, write a session note."""


@run.command("context")
@click.option("--run-id", required=True, help="The caller's own run")
@click.option("--limit", default=None, type=int)
@click.option("--after-note-id", default=None, type=int)
@click.option("--after-chain-seq", default=None, type=int)
def run_context(run_id: str, limit, after_note_id, after_chain_seq) -> None:
    """The predecessor's session notes and evidence (work.run.predecessor-context-v1)."""
    _invoke("work.run.predecessor-context-v1", _present(
        run_id=run_id, limit=limit, after_note_id=after_note_id, after_chain_seq=after_chain_seq,
    ))


@run.command("note")
@click.option("--run-id", required=True)
@click.option("--note", required=True)
@click.option("--idempotency-key", required=True)
def run_note(run_id: str, note: str, idempotency_key: str) -> None:
    """Write one session note (work.session-note.write-v1)."""
    _invoke("work.session-note.write-v1", {"run_id": run_id, "note": note, "idempotency_key": idempotency_key})


def register(root: click.Group) -> None:
    root.add_command(agent)
    root.add_command(message)
    root.add_command(run)
