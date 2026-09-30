"""Oracle (agentops#2541, M2-1): the published ``work.effect.*`` surface.

sprintctl owns the effect-intent lifecycle (Decision 1 A, agentops#253).
This module pins, at the level that always imports (no PostgreSQL, no
Vuoro service), the catalog half of that contract:

* six operations -- propose, get, list-proposed, accept, reject,
  mark-applied -- published as ``work.effect.<name>-v1``;
* each mutating operation requires its *own* capability,
  ``work.effect.propose`` / ``.accept`` / ``.reject`` / ``.mark-applied``,
  never ``work:claim`` / ``work:write`` or another ordinary work authority,
  so a principal that may change work state does not inherit acceptance
  authority (the Vuoro service refuses a call whose identity lacks the
  operation's ``required_authority``);
* intents carry ``revision`` and ``canonical_intent_digest``;
  accept/reject/mark-applied name both, and the acceptor is never a wire
  argument (it is the authenticated principal);
* the acceptance record binds ``intent_id``, ``intent_revision``,
  ``canonical_intent_digest``, ``acceptor_principal``,
  ``acceptor_policy_version`` and ``accepted_at``.

The behaviour itself is pinned against PostgreSQL in
``tests/pg/test_effect_intent.py``.
"""

from __future__ import annotations

import jsonschema
import pytest

from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


_CONTRACTS = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}

PROPOSE = "work.effect.propose-v1"
GET = "work.effect.get-v1"
LIST_PROPOSED = "work.effect.list-proposed-v1"
ACCEPT = "work.effect.accept-v1"
REJECT = "work.effect.reject-v1"
MARK_APPLIED = "work.effect.mark-applied-v1"

EFFECT_OPERATIONS = (PROPOSE, GET, LIST_PROPOSED, ACCEPT, REJECT, MARK_APPLIED)
MUTATING_CAPABILITIES = {
    PROPOSE: "work.effect.propose",
    ACCEPT: "work.effect.accept",
    REJECT: "work.effect.reject",
    MARK_APPLIED: "work.effect.mark-applied",
}
# Authorities that let a principal change ordinary work state.  None of them
# may double as an effect capability.
ORDINARY_WORK_AUTHORITIES = frozenset(
    {"work:claim", "work:write", "work:lifecycle", "work:evidence", "work:sprint"}
)

DIGEST = "a" * 64
BINDING = {"intent_id": "intent-1", "revision": 1, "canonical_intent_digest": DIGEST}
PROPOSE_ARGS = {
    "run_id": "run_" + "0" * 26,
    "item_id": 1,
    "repository": "vuoro-e3-canary",
    "base_commit": "b" * 40,
    "title": "Fix a typo",
    "rationale": "The README misspells a word.",
    "unified_diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-teh\n+the\n",
    "idempotency_key": "propose-key-1",
}


def _contract(name: str):
    assert name in _CONTRACTS, f"{name} is not published in WORK_OPERATION_CONTRACTS"
    return _CONTRACTS[name]


def _valid(schema: dict, instance: object) -> bool:
    validator_class = jsonschema.validators.validator_for(schema)
    return validator_class(schema).is_valid(instance)


def _schema_props(schema: dict) -> dict:
    return schema.get("properties", {})


@pytest.mark.parametrize("name", EFFECT_OPERATIONS)
def test_every_effect_operation_is_published(name):
    _contract(name)


@pytest.mark.parametrize("name,capability", sorted(MUTATING_CAPABILITIES.items()))
def test_each_mutating_operation_requires_its_own_effect_capability(name, capability):
    contract = _contract(name)
    assert contract.required_authority == capability
    assert contract.execution_semantics == "write"


def test_the_four_effect_capabilities_are_distinct():
    authorities = [_contract(name).required_authority for name in MUTATING_CAPABILITIES]
    assert len(set(authorities)) == 4


@pytest.mark.parametrize("name", EFFECT_OPERATIONS)
def test_no_effect_operation_is_reachable_with_ordinary_work_authority(name):
    authority = _contract(name).required_authority
    assert authority is not None, f"{name} must require an authority"
    assert authority not in ORDINARY_WORK_AUTHORITIES


@pytest.mark.parametrize("name", (GET, LIST_PROPOSED))
def test_get_and_list_proposed_are_reads(name):
    assert _contract(name).execution_semantics == "read"


def test_propose_input_is_diff_shaped_bound_to_run_item_and_key():
    schema = _contract(PROPOSE).input_schema
    required = set(schema.get("required", ()))
    assert {
        "run_id", "item_id", "repository", "base_commit", "title", "rationale",
        "unified_diff", "idempotency_key",
    } <= required
    assert _valid(schema, PROPOSE_ARGS)
    without_key = {k: v for k, v in PROPOSE_ARGS.items() if k != "idempotency_key"}
    assert not _valid(schema, without_key)


def test_propose_cannot_name_its_proposer_or_digest():
    """The proposer is the authenticated principal and the digest is the
    authority's computation, never a caller claim."""
    schema = _contract(PROPOSE).input_schema
    for field in ("proposer_principal", "canonical_intent_digest", "revision", "state"):
        assert not _valid(schema, {**PROPOSE_ARGS, field: "x" if field != "revision" else 1}), field


@pytest.mark.parametrize("name", (ACCEPT, REJECT, MARK_APPLIED))
def test_transitions_must_name_revision_and_digest(name):
    schema = _contract(name).input_schema
    required = set(schema.get("required", ()))
    assert {"intent_id", "revision", "canonical_intent_digest"} <= required
    props = _schema_props(schema)
    assert props["revision"].get("type") == "integer"
    # A malformed digest is refused by the published schema.
    extra = {"reason": "no"} if name == REJECT else {}
    if name == MARK_APPLIED:
        extra = {"commit_sha": "c" * 40, "pr_url": "https://example.invalid/pr/1"}
    assert _valid(schema, {**BINDING, **extra})
    assert not _valid(schema, {**BINDING, **extra, "canonical_intent_digest": "not-a-digest"})
    for field in ("revision", "canonical_intent_digest"):
        assert not _valid(schema, {k: v for k, v in {**BINDING, **extra}.items() if k != field})


@pytest.mark.parametrize("name", (ACCEPT, REJECT, MARK_APPLIED))
@pytest.mark.parametrize(
    "field", ("acceptor_principal", "acceptor", "principal_id", "actor", "acceptor_policy_version")
)
def test_the_acceptor_is_never_a_wire_argument(name, field):
    schema = _contract(name).input_schema
    extra = {"reason": "no"} if name == REJECT else {}
    if name == MARK_APPLIED:
        extra = {"commit_sha": "c" * 40, "pr_url": "https://example.invalid/pr/1"}
    assert not _valid(schema, {**BINDING, **extra, field: "github:1:0"})


def test_reject_requires_a_reason():
    schema = _contract(REJECT).input_schema
    assert "reason" in set(schema.get("required", ()))


def test_mark_applied_requires_the_commit_and_pull_request():
    schema = _contract(MARK_APPLIED).input_schema
    assert {"commit_sha", "pr_url"} <= set(schema.get("required", ()))


def _intent_schema(name: str) -> dict:
    result = _contract(name).result_schema
    assert "intent" in set(result.get("required", ())), f"{name} returns an intent"
    return _schema_props(result)["intent"]


def _object_branch(schema: dict) -> dict:
    """The object alternative of a nullable subschema."""
    if schema.get("type") == "object":
        return schema
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, ()):
            if branch.get("type") == "object":
                return branch
    raise AssertionError(f"no object branch in {schema!r}")


@pytest.mark.parametrize("name", (PROPOSE, GET, ACCEPT, REJECT, MARK_APPLIED))
def test_every_intent_result_carries_revision_digest_and_state(name):
    schema = _object_branch(_intent_schema(name))
    required = set(schema.get("required", ()))
    assert {
        "intent_id", "revision", "canonical_intent_digest", "state", "item_id",
        "proposer_principal", "acceptance",
    } <= required
    props = _schema_props(schema)
    assert _valid(props["canonical_intent_digest"], DIGEST)
    assert not _valid(props["canonical_intent_digest"], "not-a-digest")
    assert not _valid(props["canonical_intent_digest"], "A" * 64)
    assert _valid(props["revision"], 1) and not _valid(props["revision"], 0)
    for state in ("proposed", "accepted", "rejected", "applied"):
        assert _valid(props["state"], state), state
    assert not _valid(props["state"], "executing")
    assert _valid(props["acceptance"], None), "a proposed intent has no acceptance"


ACCEPTANCE_FIELDS = (
    "intent_id",
    "intent_revision",
    "canonical_intent_digest",
    "acceptor_principal",
    "acceptor_policy_version",
    "accepted_at",
)


@pytest.mark.parametrize("name", (PROPOSE, GET, ACCEPT, REJECT, MARK_APPLIED))
def test_the_acceptance_record_binds_intent_revision_digest_and_principal(name):
    acceptance = _object_branch(_schema_props(_object_branch(_intent_schema(name)))["acceptance"])
    assert set(ACCEPTANCE_FIELDS) <= set(acceptance.get("required", ()))
    props = _schema_props(acceptance)
    assert _valid(props["intent_revision"], 1)
    assert not _valid(props["intent_revision"], "1")
    assert _valid(props["canonical_intent_digest"], DIGEST)
    assert not _valid(props["canonical_intent_digest"], "not-a-digest")
    # Null until a policy acceptor exists; an operator acceptance carries none.
    assert _valid(props["acceptor_policy_version"], None)


def test_list_proposed_returns_intents():
    result = _contract(LIST_PROPOSED).result_schema
    assert "intents" in set(result.get("required", ()))
    intents = _schema_props(result)["intents"]
    assert intents.get("type") == "array"
    item = _object_branch(intents["items"])
    assert {"intent_id", "revision", "canonical_intent_digest", "state"} <= set(item.get("required", ()))


def test_the_intent_store_is_a_schema_after_the_lease_tables():
    """Decision 1: the store goes into a sprintctl schema after 18 (schema 18
    is the lease tables)."""
    from sprintctl import pg_migrations

    assert pg_migrations.CURRENT_SCHEMA_VERSION > 18
