from dataclasses import FrozenInstanceError, replace

import pytest

from sprintctl.recipient_provenance import (
    AuthenticatedOwnerIdentity,
    AuthenticatedRequesterContext,
    RecipientProvenance,
)


def provenance():
    return RecipientProvenance(
        principal_id="issuer:subject:1", workspace_id="workspace", gateway_issuer="issuer",
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
    {"effective_scopes": frozenset({"work:evidence"})},
    {"resource": ""},
    {"token_scopes": frozenset({"vuoro:work.read"})},
    {"token_scopes": frozenset({"vuoro:evidence.record"})},
])
def test_incomplete_or_expanded_provenance_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(provenance(), **changes)


@pytest.mark.parametrize("changes", [
    {"principal_id": "issuer:other:1"},
    {"gateway_issuer": "other"},
    {"subject": "other"},
    {"epoch": 2},
    {"principal_id": "issuer:subject:01"},
])
def test_inconsistent_identity_tuple_is_rejected(changes):
    with pytest.raises(ValueError, match="principal_id must match"):
        replace(provenance(), **changes)


def test_composed_identity_accepts_gateway_subject_and_safe_epoch_boundary():
    value = replace(provenance(), gateway_issuer="https://issuer:443",
                    subject="Subject.part_9-x", epoch=2**53 - 1,
                    principal_id=f"https://issuer:443:Subject.part_9-x:{2**53 - 1}")
    assert value.principal_id == f"{value.gateway_issuer}:{value.subject}:{value.epoch}"
    assert value.token_scopes != value.effective_scopes


@pytest.mark.parametrize("subject", ["subject:part", "subject/part", "subject part", "é", "subject\n"])
def test_subject_rejects_characters_outside_gateway_grammar(subject):
    with pytest.raises(ValueError, match="subject must match"):
        replace(provenance(), subject=subject, principal_id=f"issuer:{subject}:1")


def owner_identity(value=None):
    value = value or provenance()
    return AuthenticatedOwnerIdentity(value.principal_id, value.workspace_id, value.gateway_issuer,
                                      value.subject, value.epoch)


def test_owner_identity_is_separate_immutable_and_validated():
    owner = owner_identity()
    with pytest.raises(FrozenInstanceError):
        owner.workspace_id = "other"
    with pytest.raises(ValueError, match="principal_id must match"):
        replace(owner, subject="other")
    assert not isinstance(owner, RecipientProvenance)


def test_requester_context_requires_evidence_authority_and_is_distinct():
    requester = AuthenticatedRequesterContext(
        principal_id="issuer:requester:2", workspace_id="workspace",
        gateway_issuer="issuer", subject="requester", epoch=2,
        authorities=frozenset({"work:evidence"}),
    )
    assert requester.authorities == frozenset({"work:evidence"})
    assert not isinstance(requester, AuthenticatedOwnerIdentity)
    with pytest.raises(ValueError, match="work:evidence"):
        AuthenticatedRequesterContext(
            principal_id="issuer:requester:2", workspace_id="workspace",
            gateway_issuer="issuer", subject="requester", epoch=2,
            authorities=frozenset({"work:read"}),
        )
    with pytest.raises(ValueError, match="authorities"):
        AuthenticatedRequesterContext(
            principal_id="issuer:requester:2", workspace_id="workspace",
            gateway_issuer="issuer", subject="requester", epoch=2,
            authorities={"work:evidence"},
        )
