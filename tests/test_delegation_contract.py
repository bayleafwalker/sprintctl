from types import SimpleNamespace

import pytest

from sprintctl import delegation, delegation_pg

RUN = "run_" + "A" * 26
DELEGATION = "dlg_" + "B" * 26


@pytest.mark.parametrize("ttl", [True, False, 0, -1, 14_401, 1.0, "1", None])
def test_ttl_is_an_integer_in_the_closed_owner_range(ttl):
    with pytest.raises(delegation.DelegationRefused) as caught:
        delegation_pg.validate_ttl_seconds(ttl)
    assert caught.value.code == "invalid-arguments"


@pytest.mark.parametrize("ttl", [1, delegation.MAX_TTL_SECONDS])
def test_ttl_accepts_closed_range_boundaries(ttl):
    assert delegation_pg.validate_ttl_seconds(ttl) == ttl


def test_minted_token_has_256_random_bits_in_canonical_encoding():
    token = delegation_pg.mint_token()
    assert len(token) == 56
    assert token.startswith("vrd_")
    assert set(token[4:]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert delegation_pg.mint_token() != token


@pytest.mark.parametrize("context", [
    None,
    SimpleNamespace(delegation_id=DELEGATION, run_id=RUN, operations=("work.run.resolve-v1",)),
    delegation.DelegatedRunContext(DELEGATION, "run_bad", ("work.run.resolve-v1",)),
    delegation.DelegatedRunContext(DELEGATION, RUN, ("work.run.resolve-v1", "work.run.resolve-v1")),
    delegation.DelegatedRunContext(DELEGATION, RUN, ("work.run.delegate-v1",)),
    delegation.DelegatedRunContext(DELEGATION, RUN, ([],)),
])
def test_context_must_be_owner_typed_and_closed(context):
    with pytest.raises(delegation.DelegationRefused):
        delegation.validate_delegated_context(context)


def test_valid_context_is_immutable_and_scoped():
    context = delegation.DelegatedRunContext(DELEGATION, RUN, ("work.run.resolve-v1",))
    assert delegation.validate_delegated_context(context) is context
    with pytest.raises((AttributeError, TypeError)):
        context.run_id = "run_" + "C" * 26


def test_resolver_is_internal_and_public_surface_is_three_owner_operations():
    assert delegation.INTERNAL_RESOLVE not in delegation.PUBLIC_OPERATIONS
    assert len(delegation.PUBLIC_OPERATIONS) == 3
    assert delegation.DELEGATED_OPERATIONS == frozenset({
        "work.run.resolve-v1", "work.run.predecessor-context-v1",
        "work.session-note.write-v1", "work.evidence.append-v1",
    })


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "A" * 64, "x" * 64])
def test_digest_resolver_rejects_noncanonical_digests_before_storage(digest):
    with pytest.raises(delegation.DelegationRefused) as caught:
        delegation_pg.resolve_token_digest(SimpleNamespace(), digest)
    assert caught.value.code == "credential-invalid"
