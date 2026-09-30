"""Unit checks for the effect-intent digest and the application's capability
gate (agentops#2541).  The catalog half of the contract is pinned by
tests/test_effect_intent_contract.py and the lifecycle by
tests/pg/test_effect_intent.py; this module pins what those two rely on."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sprintctl import effect_intent as effect
from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from sprintctl.work_application import _effect_arguments, _require_effect_authority

CONTENT = {
    "item_id": 7,
    "repository": "vuoro-e3-canary",
    "base_commit": "b" * 40,
    "title": "Fix a typo",
    "rationale": "The README misspells a word.",
    "unified_diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-teh\n+the\n",
}


def test_the_digest_is_lowercase_sha256_hex():
    digest = effect.canonical_intent_digest(CONTENT)
    assert len(digest) == 64 and digest == digest.lower()
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_the_digest_is_stable_across_key_order_and_extra_fields():
    shuffled = dict(reversed(list(CONTENT.items())))
    extra = {**CONTENT, "intent_id": "intent_x", "run_id": "run_x", "idempotency_key": "k"}
    assert effect.canonical_intent_digest(shuffled) == effect.canonical_intent_digest(CONTENT)
    assert effect.canonical_intent_digest(extra) == effect.canonical_intent_digest(CONTENT)


@pytest.mark.parametrize("field", effect.DIGEST_FIELDS)
def test_the_digest_covers_every_content_field(field):
    changed = {**CONTENT, field: 8 if field == "item_id" else CONTENT[field] + "x"}
    assert effect.canonical_intent_digest(changed) != effect.canonical_intent_digest(CONTENT)


def test_a_field_boundary_cannot_be_shifted():
    """Canonical JSON, not concatenation: moving text between two fields
    changes the digest."""
    left = {**CONTENT, "title": "ab", "rationale": "c"}
    right = {**CONTENT, "title": "a", "rationale": "bc"}
    assert effect.canonical_intent_digest(left) != effect.canonical_intent_digest(right)


def test_every_effect_operation_has_a_catalog_authority_that_matches():
    contracts = {c.name: c for c in WORK_OPERATION_CONTRACTS}
    assert set(effect.EFFECT_OPERATION_AUTHORITIES) == {
        name for name in contracts if name.startswith("work.effect.")
    }
    for name, authority in effect.EFFECT_OPERATION_AUTHORITIES.items():
        assert contracts[name].required_authority == authority


def _context(authorities):
    return SimpleNamespace(identity=SimpleNamespace(authorities=frozenset(authorities)))


@pytest.mark.parametrize("operation,authority", sorted(effect.EFFECT_OPERATION_AUTHORITIES.items()))
def test_the_gate_admits_exactly_the_operations_own_capability(operation, authority):
    _require_effect_authority(operation, _context({authority}))
    others = set(effect.EFFECT_OPERATION_AUTHORITIES.values()) - {authority}
    ordinary = {"work:read", "work:write", "work:claim", "work:lifecycle", "work:evidence"}
    with pytest.raises(ApplicationRejection) as refused:
        _require_effect_authority(operation, _context(others | ordinary))
    assert (refused.value.code, refused.value.http_status) == ("authority-required", 403)


def test_the_gate_fails_closed_without_authorities():
    for context in (_context(()), SimpleNamespace(identity=SimpleNamespace()), SimpleNamespace()):
        with pytest.raises(ApplicationRejection) as refused:
            _require_effect_authority(effect.OPERATION_ACCEPT, context)
        assert refused.value.code == "authority-required"


def test_the_gate_leaves_other_operations_alone():
    _require_effect_authority("work.lease.read-v1", _context(()))


def test_unknown_arguments_are_refused_not_ignored():
    with pytest.raises(ApplicationRejection) as refused:
        _effect_arguments({"intent_id": "i", "acceptor_principal": "x"}, frozenset({"intent_id"}))
    assert (refused.value.code, refused.value.http_status) == ("invalid-arguments", 422)
    _effect_arguments({"intent_id": "i"}, frozenset({"intent_id"}))
