"""Agent, message, run and effect commands over existing served operations.

Each command sends one catalog operation through the served client. JSON
preserves its result; briefing/explanation text presents the same owner facts.
The agent directory and inbox (vuoro contract v1) and the
run continuation reads exist only on the PostgreSQL owner, so every verb here
requires ``SPRINTCTL_BACKEND=served``; no owner state is inferred locally. Records
that the contract defines as JSON objects (a declaration, an envelope, a
receipt's own fields) are read from ``--json FILE`` (``-`` for stdin).
A refusal prints ``{"error": {"code", "message"}}`` and exits 1.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from time import monotonic
from typing import Any

import click

from .. import served as _served
from ..cli_runtime import _served_config_or_none


def _json_input(stream: Any, what: str) -> Any:
    try:
        return json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"{what} is not valid JSON: {exc}") from exc


class _OwnerReadRefused(click.ClickException):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_.-]{1,80}", code) else "unknown"
        super().__init__(f"owner refused the read ({self.code})")


def _call(operation: str, arguments: dict[str, Any], *, raise_refusal: bool = False) -> Any:
    obj = click.get_current_context().find_root().obj
    config = _served_config_or_none(obj)
    if config is None:
        raise click.ClickException(
            f"{operation} is served by the PostgreSQL owner only; set SPRINTCTL_BACKEND=served"
        )
    try:  # served extra, lazy; absent only where nothing can be served
        from vuoro_client.errors import InvocationRejectedError
    except ModuleNotFoundError:
        InvocationRejectedError = None  # type: ignore[assignment,misc]

    try:
        result = _served.invoke_agent_operation(
            config.served_profile, operation, arguments, repo_id=config.repo_id
        )
    except Exception as exc:
        if InvocationRejectedError is None or not isinstance(exc, InvocationRejectedError):
            raise
        if raise_refusal:
            raise _OwnerReadRefused(exc.code) from None
        message = str(exc).partition(": ")[2] or str(exc)
        click.echo(json.dumps({"error": {"code": exc.code, "message": message}}, indent=2))
        sys.exit(1)
    return result


def _invoke(operation: str, arguments: dict[str, Any]) -> None:
    click.echo(json.dumps(_call(operation, arguments), indent=2, ensure_ascii=False))


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _continuation_arguments(run_id, item_id, limit, checkpoint, cursor, after_note_id, after_chain_seq):
    arguments = _present(run_id=run_id, item_id=item_id, limit=limit,
                         after_note_id=after_note_id, after_chain_seq=after_chain_seq)
    for name, stream in (("checkpoint", checkpoint), ("cursor", cursor)):
        if stream is not None:
            value = _json_input(stream, name)
            if not isinstance(value, dict):
                raise click.ClickException(f"{name} must be a JSON object")
            arguments[name] = value
    return arguments


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


@run.command("brief")
@click.option("--run-id", required=True, help="The caller's own run; predecessor credentials are never reused")
@click.option("--item-id", required=True, type=click.IntRange(min=1), help="Item in the current repository")
@click.option("--checkpoint", type=click.File("r"), help="Complete owner checkpoint JSON (previous response's delta.current)")
@click.option("--cursor", type=click.File("r"), help="Previous response's delta.next_reference_cursor JSON")
@click.option("--after-note-id", type=click.IntRange(min=0), default=None)
@click.option("--after-chain-seq", type=click.IntRange(min=0), default=None)
@click.option("--limit", type=click.IntRange(1, 64), default=32, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def run_brief(run_id, item_id, checkpoint, cursor, after_note_id, after_chain_seq, limit, fmt) -> None:
    """Brief one item from a single bounded owner read; JSON preserves its response."""
    from ..briefing import render_run_brief

    arguments = _continuation_arguments(run_id, item_id, limit, checkpoint, cursor,
                                        after_note_id, after_chain_seq)
    result = _call("work.run.continuation-delta-v1", arguments)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False) if fmt == "json"
               else render_run_brief(result))


@run.command("collect")
@click.option("--json", "pages", type=click.File("rb"), required=True,
              help="Offline envelope array: run_id, item_id, limit, cursor, checkpoint, after_note_id, after_chain_seq, observed_at, response")
@click.option("--page-budget", type=click.IntRange(1, 64), default=64, show_default=True,
              help="Finite collection budget, at most 64 pages")
@click.option("--output", type=click.Path(path_type=str), default="-", show_default=True,
              help="Collection JSON output path, - for stdout")
def run_collect(pages, page_budget, output) -> None:
    """Package supplied continuation pages offline; no owner call is made."""
    from ..continuation_collection import MAX_COLLECTION_BYTES, create_collection

    raw = pages.read(MAX_COLLECTION_BYTES + 1)
    if len(raw) > MAX_COLLECTION_BYTES:
        raise click.ClickException("captured page input exceeds 2 MiB")
    try:
        collection = create_collection(raw, page_budget=page_budget)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise click.ClickException(str(error)) from error
    from ..result_packet import canonical
    rendered = canonical(collection).decode("utf-8")
    if output == "-":
        click.echo(rendered, nl=False)
    else:
        absolute_output = os.path.abspath(output)
        output_dir = os.path.dirname(absolute_output)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_dir,
                                             prefix=".sprintctl-collection-", delete=False) as stream:
                temp_path = stream.name
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_path, absolute_output)
        except FileExistsError as error:
            raise click.ClickException("output exists; choose a new path") from error
        except OSError as error:
            raise click.ClickException(f"could not write collection atomically: {error}") from error
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass


@run.command("collect-fetch")
@click.option("--run-id", required=True, help="The caller's own authorized run")
@click.option("--item-id", required=True, type=click.IntRange(min=1))
@click.option("--limit", type=click.IntRange(1, 64), default=32, show_default=True)
@click.option("--page-budget", type=click.IntRange(1, 64), default=64, show_default=True)
@click.option("--deadline-seconds", type=click.FloatRange(min=1.0, max=300.0),
              default=30.0, show_default=True,
              help="Stop issuing page reads after this caller-side budget")
@click.option("--output", type=click.Path(path_type=str), default="-", show_default=True,
              help="Collection JSON output path, - for stdout")
def run_collect_fetch(run_id, item_id, limit, page_budget, deadline_seconds, output) -> None:
    """Read bounded owner pages and save them in the offline collection format."""
    from ..continuation_collection import (
        MAX_COLLECTION_BYTES,
        CollectionError,
        _validate_response,
        create_collection,
    )
    from ..result_packet import PacketError, canonical

    if output != "-" and os.path.lexists(os.path.abspath(output)):
        raise click.ClickException("output exists; choose a new path")

    started = monotonic()
    deadline = started + deadline_seconds
    pages: list[dict[str, Any]] = []
    request_cursor = checkpoint = None
    after_note_id = after_chain_seq = None
    last_current = None
    seen_reference_cursors: set[bytes] = set()
    seen_note_cursors: set[int] = set()
    seen_evidence_cursors: set[int] = set()
    deadline_exhausted = False
    read_stop_reason = None
    extra_warnings: list[str] = []

    for _ordinal in range(page_budget):
        if monotonic() >= deadline:
            deadline_exhausted = True
            break

        arguments = _present(run_id=run_id, item_id=item_id, limit=limit,
                             cursor=request_cursor, checkpoint=checkpoint,
                             after_note_id=after_note_id,
                             after_chain_seq=after_chain_seq)
        try:
            response = _call("work.run.continuation-delta-v1", arguments, raise_refusal=True)
        except _OwnerReadRefused as error:
            read_stop_reason = "owner-refused"
            extra_warnings.append(f"owner read refused ({error.code})")
            break
        except click.ClickException:
            raise
        except Exception as error:
            read_stop_reason = "owner-read-failed"
            extra_warnings.append(f"owner read failed ({type(error).__name__})")
            break
        try:
            _validate_response(response)
        except (CollectionError, RecursionError) as error:
            read_stop_reason = "owner-read-failed"
            extra_warnings.append("owner returned an unusable page; response omitted")
            if not pages:
                raise click.ClickException(f"owner returned an unusable page: {error}") from error
            break

        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        pages.append({
            "run_id": run_id,
            "item_id": item_id,
            "limit": limit,
            "cursor": request_cursor,
            "checkpoint": checkpoint,
            "after_note_id": after_note_id,
            "after_chain_seq": after_chain_seq,
            "observed_at": observed_at,
            "response": response,
        })
        try:
            candidate = create_collection(pages, page_budget=page_budget)
            if len(canonical(candidate)) > MAX_COLLECTION_BYTES:
                raise CollectionError("collection exceeds 2 MiB")
        except (CollectionError, PacketError, RecursionError, ValueError) as error:
            raise click.ClickException(f"could not archive owner page: {error}") from error

        delta = response["delta"]
        next_reference = delta["next_reference_cursor"]
        next_note_id = delta["next_after_note_id"]
        next_chain_seq = delta["next_after_chain_seq"]
        pending = any(value is not None for value in
                      (next_reference, next_note_id, next_chain_seq))
        snapshot_changed = (
            request_cursor is not None
            and request_cursor["expected_snapshot_digest"] != delta["snapshot_digest"]
        )
        checkpoint_changed = (
            last_current is not None and canonical(last_current) != canonical(delta["current"])
        )
        if snapshot_changed or checkpoint_changed:
            read_stop_reason = "source-changed"
            extra_warnings.append(
                "owner current checkpoint changed during paged read"
                if checkpoint_changed
                else "owner snapshot changed during paged read"
            )
            break
        if not pending:
            break

        if next_reference is not None:
            key = canonical(next_reference)
            if key in seen_reference_cursors:
                read_stop_reason = "invalid-cursor"
                extra_warnings.append("owner returned a repeated reference cursor")
                break
            last_requested = request_cursor["last_reference_id"] if request_cursor else 0
            if next_reference["last_reference_id"] <= last_requested:
                read_stop_reason = "invalid-cursor"
                extra_warnings.append("owner returned a non-advancing reference cursor")
                break
            seen_reference_cursors.add(key)
        if next_note_id is not None:
            if next_note_id in seen_note_cursors or (
                after_note_id is not None and next_note_id <= after_note_id
            ):
                read_stop_reason = "invalid-cursor"
                extra_warnings.append("owner returned a repeated or non-advancing note cursor")
                break
            seen_note_cursors.add(next_note_id)
        if next_chain_seq is not None:
            if next_chain_seq in seen_evidence_cursors or (
                after_chain_seq is not None and next_chain_seq <= after_chain_seq
            ):
                read_stop_reason = "invalid-cursor"
                extra_warnings.append("owner returned a repeated or non-advancing evidence cursor")
                break
            seen_evidence_cursors.add(next_chain_seq)

        request_cursor = next_reference
        last_current = delta["current"]
        after_note_id = next_note_id
        after_chain_seq = next_chain_seq
        if monotonic() >= deadline and len(pages) < page_budget:
            deadline_exhausted = True
            break

    if not pages:
        if read_stop_reason is not None:
            raise click.ClickException(extra_warnings[0])
        raise click.ClickException("caller deadline elapsed before the first owner page")
    try:
        collection = create_collection(
            pages,
            page_budget=page_budget,
            stop_reason_override=(read_stop_reason or
                                  ("deadline-exhausted" if deadline_exhausted else None)),
            additional_warnings=extra_warnings,
        )
        rendered = canonical(collection).decode("utf-8")
    except (CollectionError, PacketError, RecursionError, ValueError) as error:
        raise click.ClickException(f"could not archive owner pages: {error}") from error
    if len(rendered.encode("utf-8")) > MAX_COLLECTION_BYTES:
        raise click.ClickException("fetched collection exceeds 2 MiB")

    if output == "-":
        click.echo(rendered, nl=False)
        return

    absolute_output = os.path.abspath(output)
    output_dir = os.path.dirname(absolute_output)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_dir,
                                         prefix=".sprintctl-collection-", delete=False) as stream:
            temp_path = stream.name
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, absolute_output)
    except FileExistsError as error:
        raise click.ClickException("output exists; choose a new path") from error
    except OSError as error:
        raise click.ClickException(f"could not write collection atomically: {error}") from error
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


@run.command("collect-inspect")
@click.option("--json", "collection_file", type=click.File("rb"), required=True,
              help="Offline continuation collection JSON file")
def run_collect_inspect(collection_file) -> None:
    """Verify collection/page digests offline; authenticity stays unknown."""
    from ..continuation_collection import MAX_COLLECTION_BYTES, inspect_collection

    raw = collection_file.read(MAX_COLLECTION_BYTES + 1)
    if len(raw) > MAX_COLLECTION_BYTES:
        raise click.ClickException("continuation collection exceeds 2 MiB")
    try:
        report = inspect_collection(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise click.ClickException(str(error)) from error
    from ..result_packet import canonical
    click.echo(canonical(report).decode("utf-8"))


@run.command("diagnose")
@click.option("--run-id", required=True)
@click.option("--item-id", required=True, type=click.IntRange(min=1))
@click.option("--checkpoint", type=click.File("r"), help="Complete saved owner delta.current JSON")
@click.option("--observations", type=click.File("rb"), help="Optional caller-reported checks, at most 64 KiB")
@click.option("--cursor", type=click.File("r"))
@click.option("--after-note-id", type=click.IntRange(min=0), default=None)
@click.option("--after-chain-seq", type=click.IntRange(min=0), default=None)
@click.option("--limit", type=click.IntRange(1, 64), default=32, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def run_diagnose(run_id, item_id, checkpoint, observations, cursor, after_note_id, after_chain_seq, limit, fmt):
    """Find continuation gaps in one owner page; do not perform artifact checks."""
    from ..resume_diagnostics import MAX_OBSERVATION_BYTES, diagnose_resume, render_resume_diagnostic

    arguments = _continuation_arguments(run_id, item_id, limit, checkpoint, cursor,
                                        after_note_id, after_chain_seq)
    observed = None
    if observations is not None:
        raw = observations.read(MAX_OBSERVATION_BYTES + 1)
        if len(raw) > MAX_OBSERVATION_BYTES:
            raise click.ClickException("observations exceed 64 KiB")
        try:
            observed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise click.ClickException("observations are not valid JSON") from error
        if not isinstance(observed, dict):
            raise click.ClickException("observations must be a JSON object")
    result = _call("work.run.continuation-delta-v1", arguments)
    try:
        report = diagnose_resume(result, checkpoint_supplied=checkpoint is not None, observations=observed)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(report, indent=2, ensure_ascii=False) if fmt == "json"
               else render_resume_diagnostic(report))


@click.group("effect")
def effect() -> None:
    """Read effect acceptance facts and their evidence limits."""


@effect.command("explain")
@click.option("--intent-id", required=True)
@click.option("--revision", required=True, type=click.IntRange(min=1))
@click.option("--canonical-intent-digest", required=True, help="Exact lower-case 64-character SHA256")
@click.option("--artifact-sha256", required=True, help="sha256:<64 lower-case hex characters>")
@click.option("--verification-ref", type=click.File("r"), help="JSON object containing run_id and item_id")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
def effect_explain(intent_id, revision, canonical_intent_digest, artifact_sha256, verification_ref, fmt) -> None:
    """Explain one exact effect basis; mismatches are facts, not acceptance writes."""
    import re
    from ..effect_explanation import render_effect_explanation

    if (re.fullmatch(r"[0-9a-f]{64}", canonical_intent_digest) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_sha256) is None):
        raise click.ClickException("give exact lower-case SHA256 digests for the intent and artifact")
    arguments = {"intent_id": intent_id, "revision": revision,
                 "canonical_intent_digest": canonical_intent_digest, "artifact_sha256": artifact_sha256}
    if verification_ref is not None:
        ref = _json_input(verification_ref, "verification reference")
        if (not isinstance(ref, dict) or set(ref) != {"run_id", "item_id"}
                or any(not isinstance(ref[key], str) or not ref[key] for key in ref)):
            raise click.ClickException("verification reference must contain exactly run_id and item_id strings")
        arguments["verification_ref"] = ref
    result = _call("work.effect.acceptance-snapshot-v1", arguments)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False) if fmt == "json"
               else render_effect_explanation(result))


def register(root: click.Group) -> None:
    root.add_command(agent)
    root.add_command(message)
    root.add_command(run)
    root.add_command(effect)
