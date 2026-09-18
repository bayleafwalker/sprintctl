"""Work decisions: the only writer of a work item's terminal status.

TS-5.  A work item becomes terminal (``status = 'done'``) only by recording a
terminal decision: ``accept``, ``reject``, ``withdraw`` or ``supersede``.  The
decision row is append-only; the item keeps a pointer to it
(``terminal_decision_id``) and a ``resolution`` derived from its kind.
``revise`` is recorded without changing the item.

Rows that predate decisions are marked ``legacy`` by the migration that
introduced them (PostgreSQL schema 14, SQLite schema 23).  A legacy item that
is already done keeps no invented decision; an open legacy item still needs a
decision to become terminal.

Both backends share the argument validation and transition rule here; each
backend owns its SQL and the database triggers that enforce the same rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence

DECISION_KINDS = ("accept", "reject", "withdraw", "supersede", "revise")
TERMINAL_RESOLUTIONS: dict[str, str] = {
    "accept": "accepted",
    "reject": "rejected",
    "withdraw": "withdrawn",
    "supersede": "superseded",
}
RESOLUTIONS = tuple(TERMINAL_RESOLUTIONS.values())
# Legacy sprint-subject decisions folded from retired capability receipts.
LEGACY_CAPABILITY_RECEIPT_SOURCE = "capability-receipt"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_EVIDENCE_DIGESTS = 64
_MAX_RATIONALE_LENGTH = 4000


def is_terminal(kind: str) -> bool:
    return kind in TERMINAL_RESOLUTIONS


def resolution_for(kind: str) -> str | None:
    return TERMINAL_RESOLUTIONS.get(kind)


def normalize_decision_fields(
    kind: Any,
    *,
    rationale: Any = "",
    evidence_digests: Sequence[Any] | None = None,
    release_digest: Any = None,
) -> dict[str, Any]:
    """Validate the subject-independent part of a decision."""
    if kind not in DECISION_KINDS:
        raise ValueError(
            "decision kind must be one of " + ", ".join(DECISION_KINDS)
        )
    if rationale is None:
        rationale = ""
    if not isinstance(rationale, str):
        raise ValueError("decision rationale must be a string")
    if len(rationale) > _MAX_RATIONALE_LENGTH:
        raise ValueError(
            f"decision rationale must be at most {_MAX_RATIONALE_LENGTH} characters"
        )
    if evidence_digests is None:
        evidence_digests = ()
    if isinstance(evidence_digests, (str, bytes)) or not isinstance(evidence_digests, Sequence):
        raise ValueError("evidence_digests must be an array of digests")
    digests: list[str] = []
    for index, digest in enumerate(evidence_digests):
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError(
                f"evidence_digests[{index}] must be 64 lowercase hexadecimal characters"
            )
        if digest not in digests:
            digests.append(digest)
    if len(digests) > _MAX_EVIDENCE_DIGESTS:
        raise ValueError(f"at most {_MAX_EVIDENCE_DIGESTS} evidence digests are allowed")
    if release_digest is not None and (
        not isinstance(release_digest, str) or not _DIGEST.fullmatch(release_digest)
    ):
        raise ValueError("release_digest must be 64 lowercase hexadecimal characters")
    return {
        "kind": kind,
        "rationale": rationale,
        "evidence_digests": digests,
        "release_digest": release_digest,
    }


def normalize_decision(
    kind: Any,
    *,
    actor: Any,
    rationale: Any = "",
    evidence_digests: Sequence[Any] | None = None,
    release_digest: Any = None,
    superseded_by_item_id: Any = None,
) -> dict[str, Any]:
    """Validate decision arguments and return their canonical form."""
    fields = normalize_decision_fields(
        kind,
        rationale=rationale,
        evidence_digests=evidence_digests,
        release_digest=release_digest,
    )
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("decision actor must be a non-empty string")
    if kind == "supersede":
        if (
            isinstance(superseded_by_item_id, bool)
            or not isinstance(superseded_by_item_id, int)
            or superseded_by_item_id < 1
        ):
            raise ValueError("a supersede decision must name the superseding item")
    elif superseded_by_item_id is not None:
        raise ValueError("only a supersede decision names a superseding item")
    return {
        **fields,
        "actor": actor.strip(),
        "superseded_by_item_id": superseded_by_item_id,
    }


def transition_error(kind: str, item_id: int, current_status: str) -> str | None:
    """Return why ``kind`` cannot be decided on an item in ``current_status``.

    Terminal items accept no further decisions.  ``accept`` keeps the old
    ``active -> done`` rule, so an old client's ``item.done`` is exactly an
    accept decision.  ``reject``, ``withdraw`` and ``supersede`` may close any
    open item; ``revise`` leaves the item open.
    """
    if current_status == "done":
        return f"Item #{item_id} is terminal; it accepts no further decisions"
    if kind == "accept" and current_status != "active":
        return (
            f"cannot accept item #{item_id} from {current_status}; "
            "an accept decision requires an active item"
        )
    return None


def legacy_evidence_digest(payload: Any) -> str:
    """SHA-256 of an event payload in the canonical JSON form.

    Sorted keys and compact separators, as the authority journal hashes its
    records, so both backends record the same digest for the same payload
    whatever their storage type (jsonb on PostgreSQL, text on SQLite).
    """
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload or "{}")
    if payload is None:
        payload = {}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
