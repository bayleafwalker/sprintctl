"""Effect-intent contract constants and the canonical intent digest.

agentops#2541 (M2-1; operator Decision 1 (A) and INV-E1 on agentops#253):
sprintctl owns the effect-intent lifecycle beside the work, runs and leases
it describes.  A hosted worker *proposes* a change as a unified diff; a
trusted-side principal *accepts* or *rejects* exactly that proposal; a
reconciler *marks it applied*.  Nothing here executes the change.

This module has no PostgreSQL or Vuoro dependency so the catalog
(``vuoro_adapter``), the application (``work_application``) and the storage
(``pg``) share one definition of the four effect capabilities and of the
digest that binds an acceptance to the content it accepted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

#: One capability per mutating transition.  They are deliberately not
#: ``work:claim`` / ``work:write``: a principal that may change ordinary work
#: state does not thereby hold acceptance authority, and a worker that may
#: propose cannot accept.
AUTHORITY_PROPOSE = "work.effect.propose"
AUTHORITY_ACCEPT = "work.effect.accept"
AUTHORITY_REJECT = "work.effect.reject"
AUTHORITY_MARK_APPLIED = "work.effect.mark-applied"
#: Reading an intent (its diff included) is its own capability too.
AUTHORITY_GET = "work.effect.get"
AUTHORITY_LIST_PROPOSED = "work.effect.list-proposed"
AUTHORITY_LIST_ACCEPTED = "work.effect.list-accepted"

OPERATION_PROPOSE = "work.effect.propose-v1"
OPERATION_BOUND_PROPOSE = "work.effect.propose-bound-v1"
OPERATION_GET = "work.effect.get-v1"
OPERATION_PREVIEW = "work.effect.preview-v1"
OPERATION_LIST_PROPOSED = "work.effect.list-proposed-v1"
OPERATION_LIST_ACCEPTED = "work.effect.list-accepted-v1"
OPERATION_ACCEPT = "work.effect.accept-v1"
OPERATION_REJECT = "work.effect.reject-v1"
OPERATION_MARK_APPLIED = "work.effect.mark-applied-v1"

#: operation -> the authority the caller's identity must hold.  The Vuoro
#: service enforces this from the published catalog; ``WorkApplication``
#: enforces it again so a caller that reaches the application directly
#: cannot skip it.
EFFECT_OPERATION_AUTHORITIES: Mapping[str, str] = {
    OPERATION_PROPOSE: AUTHORITY_PROPOSE,
    OPERATION_BOUND_PROPOSE: AUTHORITY_PROPOSE,
    OPERATION_GET: AUTHORITY_GET,
    OPERATION_PREVIEW: AUTHORITY_GET,
    OPERATION_LIST_PROPOSED: AUTHORITY_LIST_PROPOSED,
    OPERATION_LIST_ACCEPTED: AUTHORITY_LIST_ACCEPTED,
    OPERATION_ACCEPT: AUTHORITY_ACCEPT,
    OPERATION_REJECT: AUTHORITY_REJECT,
    OPERATION_MARK_APPLIED: AUTHORITY_MARK_APPLIED,
}

#: The states of an intent.  ``proposed`` is the only state that can move to
#: ``accepted`` or ``rejected``; only ``accepted`` moves to ``applied``.
EFFECT_STATES = ("proposed", "accepted", "rejected", "applied")

#: Version tag folded into the digest so a later change to what the digest
#: covers can never collide with a digest computed under this definition.
DIGEST_SCHEMA = "sprintctl-effect-intent/v1"

#: The content an acceptance is bound to.  Not in it: the intent id, run,
#: proposer and idempotency key (who proposed it and how many times do not
#: change what would be applied), the state and every timestamp.
DIGEST_FIELDS = (
    "item_id", "repository", "base_commit", "title", "rationale", "unified_diff",
)

MAX_TITLE = 200
MAX_REPOSITORY = 200
MAX_RATIONALE = 8000
MAX_UNIFIED_DIFF = 1_000_000
MAX_REASON = 4000
MAX_PR_URL = 2048
LIST_DEFAULT_LIMIT = 200
LIST_MAX_LIMIT = 1000


def canonical_intent_digest(content: Mapping[str, Any]) -> str:
    """sha256 (lowercase hex) of the canonical JSON of an intent's content.

    Computed by the authority from the stored content, never taken from a
    caller: the digest an acceptance names is checked against this, so a
    caller cannot accept content it has not seen.
    """
    body = {"schema": DIGEST_SCHEMA, **{field: content[field] for field in DIGEST_FIELDS}}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
