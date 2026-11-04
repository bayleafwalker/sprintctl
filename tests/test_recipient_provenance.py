from dataclasses import FrozenInstanceError, replace

import pytest

from sprintctl.recipient_provenance import RecipientProvenance


def provenance():
    return RecipientProvenance(
        principal_id="principal", workspace_id="workspace", gateway_issuer="issuer",
        subject="subject", epoch=1, client_id="client", grant_id="grant", resource="resource",
        token_scopes=frozenset({"vuoro:work.read", "vuoro:evidence.record", "other"}),
        effective_scopes=frozenset({"work:read", "work:evidence"}),
    )


def test_provenance_is_immutable_and_preserves_original_scopes():
    value = provenance()
    assert "other" in value.token_scopes
    with pytest.raises(FrozenInstanceError):
        value.grant_id = "replacement"


@pytest.mark.parametrize("changes", [
    {"subject": ""}, {"gateway_issuer": None}, {"epoch": True}, {"epoch": -1},
    {"token_scopes": {"work:read", "work:evidence"}},
    {"effective_scopes": frozenset({"work:read"})},
    {"resource": ""},
])
def test_incomplete_or_expanded_provenance_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(provenance(), **changes)
