"""Remote arbitration for durable sprintctl authority commands.

The retained SQLite/PostgreSQL command functions remain the default path.  This
module is the feature-flagged migration path: it admits one producer-authored
command, validates and applies its effect under PostgreSQL row locks, and
appends one immutable remote decision in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import contracts, outbox, pg
from .db import (
    SPRINT_TRANSITIONS,
    VALID_TRANSITIONS,
    item_status_revision,
    sprint_status_revision,
)


AUTHORITY_COMMAND = contracts.RecordClass.AUTHORITY_COMMAND.value
REMOTE_DECISION = contracts.RecordClass.REMOTE_DECISION.value
_REMOTE_ACTOR = "sprintctl-remote-authority"
# PostgreSQL ``now()`` is transaction-scoped.  Authority workers can retain a
# read transaction between requests, so lease effects and liveness checks must
# use the database instant at the statement that admits the command.
_CLAIM_CLOCK_SQL = "statement_timestamp()"


class AuthorityCommandConflict(ValueError):
    """An immutable request identity was reused with different content."""


class AuthorityProtocolError(RuntimeError):
    """The durable journal is internally incomplete or contradictory."""


class _RejectedCommand(ValueError):
    def __init__(
        self,
        reason_code: str,
        detail: str,
        *,
        current_revision: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    request_event_id: str
    decision_event_id: str
    decision_ingest_offset: int
    decision_type: str
    outcome: str
    reason_code: str | None
    reason_detail: str | None
    effect: dict[str, Any]
    duplicate: bool = False

    @property
    def accepted(self) -> bool:
        return self.outcome == "accepted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_event_id": self.request_event_id,
            "decision_event_id": self.decision_event_id,
            "decision_ingest_offset": self.decision_ingest_offset,
            "decision_type": self.decision_type,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "effect": json.loads(json.dumps(self.effect)),
            "duplicate": self.duplicate,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso(value: Any) -> str:
    normalized = pg._iso(value)
    if normalized is None:
        raise ValueError("revision timestamp is missing")
    return normalized


def item_revision(item: Mapping[str, Any]) -> str:
    # Status is the authority field for transition validity. Descriptions and
    # assignees may change without invalidating an otherwise current command;
    # dependency and claim state are revalidated separately under locks.
    return item_status_revision(dict(item))


def sprint_revision(sprint: Mapping[str, Any]) -> str:
    return sprint_status_revision(dict(sprint))




def _required_ref(envelope: contracts.AuthorityCommand, name: str) -> Any:
    value = envelope.refs.get(name)
    if value is None or value == "":
        raise _RejectedCommand("invalid-command", f"command ref {name!r} is required")
    return value


def _required_payload(envelope: contracts.AuthorityCommand, name: str) -> Any:
    value = envelope.payload.get(name)
    if value is None or value == "":
        raise _RejectedCommand("invalid-command", f"command payload {name!r} is required")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise _RejectedCommand("invalid-command", f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _RejectedCommand("invalid-command", f"{field} must be a positive integer") from exc
    if result < 1:
        raise _RejectedCommand("invalid-command", f"{field} must be a positive integer")
    return result


def _check_basis(envelope: contracts.AuthorityCommand, current: str) -> None:
    if envelope.basis_revision != current:
        raise _RejectedCommand(
            "stale-basis",
            "command basis_revision does not match current authority state",
            current_revision=current,
        )


def _command_envelope(record: outbox.OutboxRecord) -> contracts.AuthorityCommand:
    try:
        envelope = contracts.record_from_dict(record.payload)
    except (TypeError, ValueError) as exc:
        raise _RejectedCommand("invalid-command", f"invalid command envelope: {exc}") from exc
    if not isinstance(envelope, contracts.AuthorityCommand):
        raise _RejectedCommand("invalid-command", "ledger record is not an authority command")
    if envelope.event_id != record.event_id or envelope.record_type != record.event_type:
        raise _RejectedCommand("invalid-command", "command envelope identity differs from ledger record")
    return envelope


def _insert_prepared(
    cur: Any,
    store: pg.PgStore,
    prepared: pg._PreparedIngestRecord,
    ingest_offset: int,
) -> pg.IngestedRecord:
    record = prepared.record
    cur.execute(
        """
        INSERT INTO ingest_record (
            repo_id, ingest_offset, origin_stream_id, origin_seq, event_id, schema_version,
            record_class, event_type, actor, runtime_session_id, occurred_at,
            basis_revision, correlation_id, causation_id, payload, payload_sha256,
            record_sha256, producer_created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            store.repo_id,
            ingest_offset,
            record.origin_stream_id,
            record.origin_seq,
            record.event_id,
            record.schema_version,
            record.record_class,
            record.event_type,
            record.actor,
            record.runtime_session_id,
            record.occurred_at,
            record.basis_revision,
            record.correlation_id,
            record.causation_id,
            prepared.payload_json,
            record.payload_sha256,
            prepared.record_sha256,
            record.created_at,
        ),
    )
    row = cur.fetchone()
    cur.execute(
        "UPDATE ingest_stream SET highest_origin_seq = %s "
        "WHERE repo_id = %s AND origin_stream_id = %s",
        (record.origin_seq, store.repo_id, record.origin_stream_id),
    )
    return pg._ingested_record_from_row(row)


def _admit_request(
    cur: Any,
    store: pg.PgStore,
    prepared: pg._PreparedIngestRecord,
    ingest_offset: int,
) -> tuple[pg.IngestedRecord, bool]:
    record = prepared.record
    high_water, bootstrapped = pg._lock_ingest_stream(cur, store, record.origin_stream_id)
    cur.execute(
        "SELECT * FROM ingest_record "
        "WHERE repo_id = %s AND origin_stream_id = %s AND origin_seq = %s",
        (store.repo_id, record.origin_stream_id, record.origin_seq),
    )
    existing = cur.fetchone()
    if existing is not None:
        if existing["record_sha256"] != prepared.record_sha256:
            raise AuthorityCommandConflict(
                "origin stream sequence already identifies a different record"
            )
        return pg._ingested_record_from_row(existing), True

    cur.execute(
        "SELECT * FROM ingest_record WHERE repo_id = %s AND event_id = %s",
        (store.repo_id, record.event_id),
    )
    same_event = cur.fetchone()
    if same_event is not None:
        if same_event["record_sha256"] != prepared.record_sha256:
            raise AuthorityCommandConflict("event_id already identifies a different record")
        return pg._ingested_record_from_row(same_event), True

    pg._admit_sequence(record.origin_stream_id, high_water, bootstrapped, record.origin_seq)
    return _insert_prepared(cur, store, prepared, ingest_offset), False


def _decision_type(command_type: str) -> str:
    return {
        "item.transition": "item.transitioned",
        "item.done": "item.transitioned",
        "sprint.activate": "sprint-activated",
        "sprint.close": "sprint-closed",
        "capability-receipt.accept": "capability-receipt.accepted",
    }[command_type]


def _lock_item(cur: Any, store: pg.PgStore, aggregate_uuid: str) -> Mapping[str, Any]:
    cur.execute(
        "SELECT * FROM work_item WHERE repo_id = %s AND aggregate_uuid = %s FOR UPDATE",
        (store.repo_id, aggregate_uuid),
    )
    row = cur.fetchone()
    if row is None:
        raise _RejectedCommand("not-found", "work item aggregate not found")
    return row


def _lock_sprint(cur: Any, store: pg.PgStore, aggregate_uuid: str) -> Mapping[str, Any]:
    cur.execute(
        "SELECT * FROM sprint WHERE repo_id = %s AND aggregate_uuid = %s FOR UPDATE",
        (store.repo_id, aggregate_uuid),
    )
    row = cur.fetchone()
    if row is None:
        raise _RejectedCommand("not-found", "sprint aggregate not found")
    return row




def _handle_item(
    cur: Any,
    store: pg.PgStore,
    envelope: contracts.AuthorityCommand,
) -> dict[str, Any]:
    item = _lock_item(cur, store, str(_required_ref(envelope, "aggregate_uuid")))
    current_revision = item_revision(item)
    _check_basis(envelope, current_revision)
    to_status = "done" if envelope.record_type == "item.done" else str(
        _required_payload(envelope, "to_status")
    )
    current = item["status"]
    if to_status not in VALID_TRANSITIONS.get(current, set()):
        raise _RejectedCommand(
            "invalid-transition",
            f"cannot transition item {current} -> {to_status}",
            current_revision=current_revision,
        )

    if to_status == "active":
        cur.execute(
            """
            SELECT d.item_id FROM dep d
            JOIN work_item blocker
              ON blocker.repo_id = d.repo_id AND blocker.id = d.item_id
            WHERE d.repo_id = %s AND d.blocked_item_id = %s
              AND blocker.status <> 'done'
            ORDER BY d.item_id
            """,
            (store.repo_id, item["id"]),
        )
        blockers = [int(row["item_id"]) for row in cur.fetchall()]
        if blockers:
            raise _RejectedCommand(
                "unresolved-dependencies",
                f"item has unresolved blockers: {blockers}",
                current_revision=current_revision,
            )

    cur.execute(
        "UPDATE work_item SET status = %s, updated_at = now() "
        "WHERE repo_id = %s AND id = %s RETURNING *",
        (to_status, store.repo_id, item["id"]),
    )
    updated = cur.fetchone()
    return {
        "aggregate_type": "item",
        "aggregate_uuid": str(updated["aggregate_uuid"]),
        "item_id": int(updated["id"]),
        "previous_status": current,
        "status": updated["status"],
        "revision": item_revision(updated),
    }


def _handle_sprint(
    cur: Any,
    store: pg.PgStore,
    envelope: contracts.AuthorityCommand,
) -> dict[str, Any]:
    sprint = _lock_sprint(cur, store, str(_required_ref(envelope, "aggregate_uuid")))
    current_revision = sprint_revision(sprint)
    _check_basis(envelope, current_revision)
    target = "active" if envelope.record_type == "sprint.activate" else "closed"
    current = sprint["status"]
    if target not in SPRINT_TRANSITIONS.get(current, set()):
        raise _RejectedCommand(
            "invalid-transition",
            f"cannot transition sprint {current} -> {target}",
            current_revision=current_revision,
        )
    cur.execute(
        "UPDATE sprint SET status = %s WHERE repo_id = %s AND id = %s RETURNING *",
        (target, store.repo_id, sprint["id"]),
    )
    updated = cur.fetchone()
    effect: dict[str, Any] = {
        "aggregate_type": "sprint",
        "aggregate_uuid": str(updated["aggregate_uuid"]),
        "sprint_id": int(updated["id"]),
        "previous_status": current,
        "status": target,
        "revision": sprint_revision(updated),
    }
    if target == "closed":
        payload = json.dumps({"previous_status": current, "status": "closed"})
        cur.execute(
            """
            INSERT INTO event (
                repo_id, sprint_id, work_item_id, source_type, actor, event_type, payload
            ) VALUES (%s, %s, NULL, 'actor', %s, %s, %s)
            RETURNING id
            """,
            (
                store.repo_id,
                sprint["id"],
                envelope.actor,
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                payload,
            ),
        )
        boundary_id = int(cur.fetchone()["id"])
        effect["boundary_event_id"] = boundary_id
        effect["boundary_revision"] = f"event:{boundary_id}"
    return effect




def _handle_receipt(
    cur: Any,
    store: pg.PgStore,
    envelope: contracts.AuthorityCommand,
) -> dict[str, Any]:
    sprint = _lock_sprint(cur, store, str(_required_ref(envelope, "aggregate_uuid")))
    if sprint["status"] != "closed":
        raise _RejectedCommand("invalid-transition", "capability receipt requires a closed sprint")
    cur.execute(
        "SELECT id FROM event WHERE repo_id = %s AND sprint_id = %s AND event_type = %s ORDER BY id",
        (store.repo_id, sprint["id"], contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE),
    )
    boundaries = cur.fetchall()
    if len(boundaries) != 1:
        raise _RejectedCommand(
            "invalid-boundary",
            "capability receipt requires exactly one sprint-close-boundary",
        )
    boundary_id = int(boundaries[0]["id"])
    current_revision = f"event:{boundary_id}"
    _check_basis(envelope, current_revision)
    pointer = envelope.payload.get("pointer")
    try:
        canonical = contracts.canonicalize_capability_receipt_drafted_payload(pointer)
        if canonical["project"] != store.repo_id:
            raise ValueError("capability receipt project must match repository authority")
        contracts.verify_capability_receipt_draft_pointer(
            canonical,
            sprint_id=int(sprint["id"]),
            boundary_event_id=boundary_id,
        )
    except (TypeError, ValueError) as exc:
        raise _RejectedCommand("artifact-unavailable", str(exc), current_revision=current_revision) from exc
    cur.execute(
        """
        INSERT INTO event (
            repo_id, sprint_id, work_item_id, source_type, actor, event_type, payload
        ) VALUES (%s, %s, NULL, 'actor', %s, %s, %s)
        RETURNING id
        """,
        (
            store.repo_id,
            sprint["id"],
            envelope.actor,
            contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            json.dumps(canonical),
        ),
    )
    event_id = int(cur.fetchone()["id"])
    return {
        "aggregate_type": "sprint",
        "aggregate_uuid": str(sprint["aggregate_uuid"]),
        "sprint_id": int(sprint["id"]),
        "receipt_event_id": event_id,
        "boundary_revision": current_revision,
        "receipt_id": canonical["receipt_id"],
        "receipt_path": canonical["receipt_path"],
        "receipt_sha256": canonical["receipt_sha256"],
    }


def _apply_command(
    cur: Any,
    store: pg.PgStore,
    envelope: contracts.AuthorityCommand,
) -> dict[str, Any]:
    # authority_repo_uuid is populated only by the legacy direct-PostgreSQL
    # "authority submit" CLI path, which reads a committed UUID from the
    # client's own sprintctl.dispatch.json and can therefore check it against
    # every command it sends. The served (Vuoro work-adapter) path has no
    # equivalent: there is no server-side repo-UUID registry to check
    # against, because tenant isolation there is already enforced earlier,
    # by identity (Identity.authorizes_repo gates every invocation on the
    # string repo_id before WorkApplication.invoke is ever called -- see
    # vuoro_service.composition). Treating an unset authority_repo_uuid as a
    # hard error made every served item/sprint/claim authority command fail
    # unconditionally (discovered via sprintctl #1220/#1221 gate-status
    # reconciliation, 2026-07-24) since composition.py never had a UUID to
    # set it to and never should. Only enforce the mismatch check when a
    # caller has actually committed a UUID to check against.
    if (
        store.authority_repo_uuid is not None
        and envelope.refs.get("repo_id") != store.authority_repo_uuid
    ):
        raise _RejectedCommand(
            "repository-mismatch",
            "command repository UUID does not match the remote authority tenant",
        )
    if envelope.record_type in {"item.transition", "item.done"}:
        return _handle_item(cur, store, envelope)
    if envelope.record_type in {"sprint.activate", "sprint.close"}:
        return _handle_sprint(cur, store, envelope)
    if envelope.record_type == "capability-receipt.accept":
        return _handle_receipt(cur, store, envelope)
    raise _RejectedCommand("unsupported-command", f"unsupported authority command {envelope.record_type}")

def _decision_record(
    cur: Any,
    store: pg.PgStore,
    request: outbox.OutboxRecord,
    envelope: contracts.AuthorityCommand | None,
    *,
    outcome: str,
    reason_code: str | None,
    reason_detail: str | None,
    effect: Mapping[str, Any],
    ingest_offset: int,
) -> tuple[pg.IngestedRecord, contracts.RemoteDecision]:
    if outcome == "accepted" and envelope is None:
        raise AuthorityProtocolError("an accepted command requires a valid command envelope")
    decision_type = (
        _decision_type(envelope.record_type)
        if outcome == "accepted" and envelope is not None
        else "command.rejected"
    )
    decision_id = str(uuid4())
    correlation_id = (
        envelope.correlation_id if envelope is not None else request.correlation_id
    ) or request.event_id
    refs = dict(envelope.refs) if envelope is not None else {}
    decision = contracts.RemoteDecision(
        event_id=decision_id,
        record_type=decision_type,
        schema_version="1",
        actor=_REMOTE_ACTOR,
        authored_at=_utc_now(),
        refs={**refs, "request_event_id": request.event_id},
        payload={
            "outcome": outcome,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "effect": dict(effect),
        },
        basis_revision=(envelope.basis_revision if envelope is not None else request.basis_revision),
        correlation_id=correlation_id,
        causation_id=request.event_id,
    )
    stream_id = str(uuid5(NAMESPACE_URL, f"sprintctl-authority:{store.repo_id}"))
    high_water, _bootstrapped = pg._lock_ingest_stream(cur, store, stream_id)
    payload = decision.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    record = outbox.OutboxRecord(
        origin_stream_id=stream_id,
        origin_seq=high_water + 1,
        event_id=decision.event_id,
        schema_version=1,
        record_class=REMOTE_DECISION,
        event_type=decision.record_type,
        actor=decision.actor,
        runtime_session_id=None,
        occurred_at=decision.authored_at,
        basis_revision=decision.basis_revision,
        correlation_id=decision.correlation_id,
        causation_id=decision.causation_id,
        payload=payload,
        payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        created_at=decision.authored_at,
    )
    prepared = pg._prepare_ingest_record(
        record,
        allowed_classes=frozenset({REMOTE_DECISION}),
    )
    return _insert_prepared(cur, store, prepared, ingest_offset), decision


def _decision_from_row(row: Mapping[str, Any], *, duplicate: bool) -> AuthorityDecision:
    effect = row["effect"]
    if isinstance(effect, str):
        effect = json.loads(effect)
    return AuthorityDecision(
        request_event_id=row["request_event_id"],
        decision_event_id=row["decision_event_id"],
        decision_ingest_offset=int(row["decision_ingest_offset"]),
        decision_type=row["decision_type"],
        outcome=row["outcome"],
        reason_code=row["reason_code"],
        reason_detail=row["reason_detail"],
        effect=dict(effect),
        duplicate=duplicate,
    )


def arbitrate_command(
    store: pg.PgStore,
    record: outbox.OutboxRecord,
    *,
    authenticated_actor: str | None = None,
) -> AuthorityDecision:
    """Admit, arbitrate, and decide one command in one PostgreSQL transaction.

    Expected semantic rejection commits the immutable request and one rejected
    decision while rolling back the attempted effect.  Infrastructure errors
    roll back the request as well.  Identical retries return the first decision.
    """
    prepared = pg._prepare_ingest_record(
        record,
        allowed_classes=frozenset({AUTHORITY_COMMAND}),
    )
    try:
        with store.conn.cursor() as cur:
            cursor_start = pg._lock_ingest_repo_cursor(cur, store)
            request, duplicate = _admit_request(
                cur, store, prepared, cursor_start + 1
            )
            if duplicate:
                cur.execute(
                    "SELECT d.*, r.event_type AS decision_type "
                    "FROM authority_decision d "
                    "JOIN ingest_record r ON r.repo_id = d.repo_id "
                    "AND r.ingest_offset = d.decision_ingest_offset "
                    "WHERE d.repo_id = %s AND d.request_event_id = %s",
                    (store.repo_id, request.record.event_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise AuthorityProtocolError(
                        "durable authority request has no atomic remote decision"
                    )
                store.conn.commit()
                return _decision_from_row(row, duplicate=True)

            try:
                envelope = _command_envelope(request.record)
            except _RejectedCommand as rejection:
                envelope = None
                outcome = "rejected"
                reason_code = rejection.reason_code
                reason_detail = rejection.detail
                effect = {}
                if rejection.current_revision is not None:
                    effect["current_revision"] = rejection.current_revision
            else:
                if authenticated_actor is not None and (
                    request.record.actor != authenticated_actor
                    or envelope.actor != authenticated_actor
                ):
                    outcome = "rejected"
                    reason_code = "actor-mismatch"
                    reason_detail = "record actor must match the authenticated identity"
                    effect = {}
                else:
                    cur.execute("SAVEPOINT authority_effect")
                    try:
                        effect = _apply_command(cur, store, envelope)
                        outcome = "accepted"
                        reason_code = None
                        reason_detail = None
                    except _RejectedCommand as rejection:
                        cur.execute("ROLLBACK TO SAVEPOINT authority_effect")
                        outcome = "rejected"
                        reason_code = rejection.reason_code
                        reason_detail = rejection.detail
                        effect = {}
                        if rejection.current_revision is not None:
                            effect["current_revision"] = rejection.current_revision
                    except ValueError as rejection:
                        cur.execute("ROLLBACK TO SAVEPOINT authority_effect")
                        outcome = "rejected"
                        reason_code = "validation-failed"
                        reason_detail = str(rejection)
                        effect = {}
                    cur.execute("RELEASE SAVEPOINT authority_effect")

            decision_record, decision = _decision_record(
                cur,
                store,
                request.record,
                envelope,
                outcome=outcome,
                reason_code=reason_code,
                reason_detail=reason_detail,
                effect=effect,
                ingest_offset=cursor_start + 2,
            )
            pg._advance_ingest_repo_cursor(cur, store, cursor_start + 2)
            cur.execute(
                """
                INSERT INTO authority_decision (
                    repo_id, request_event_id, request_record_sha256,
                    decision_event_id, decision_ingest_offset, outcome,
                    reason_code, reason_detail, effect
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    store.repo_id,
                    request.record.event_id,
                    prepared.record_sha256,
                    decision.event_id,
                    decision_record.ingest_offset,
                    outcome,
                    reason_code,
                    reason_detail,
                    json.dumps(effect),
                ),
            )
        store.conn.commit()
        return AuthorityDecision(
            request_event_id=request.record.event_id,
            decision_event_id=decision.event_id,
            decision_ingest_offset=decision_record.ingest_offset,
            decision_type=decision.record_type,
            outcome=outcome,
            reason_code=reason_code,
            reason_detail=reason_detail,
            effect=dict(effect),
        )
    except Exception:
        store.conn.rollback()
        raise


def list_authority_decisions(
    store: pg.PgStore,
    *,
    after_offset: int = 0,
    limit: int | None = None,
) -> list[AuthorityDecision]:
    if isinstance(after_offset, bool) or not isinstance(after_offset, int) or after_offset < 0:
        raise ValueError("after_offset must be a non-negative integer")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    query = (
        "SELECT d.*, r.event_type AS decision_type FROM authority_decision d "
        "JOIN ingest_record r ON r.repo_id = d.repo_id "
        "AND r.ingest_offset = d.decision_ingest_offset "
        "WHERE d.repo_id = %s AND d.decision_ingest_offset > %s "
        "ORDER BY d.decision_ingest_offset"
    )
    params: list[Any] = [store.repo_id, after_offset]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with store.conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_decision_from_row(row, duplicate=False) for row in rows]
