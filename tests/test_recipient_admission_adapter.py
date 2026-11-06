"""Integration with the exact published Vuoro gateway identity verifier.

Run with `uv run --extra dev --extra gateway-test pytest
tests/test_recipient_admission_adapter.py -q`. The optional dependency pins
the verified service wheel; this test does not import a workspace checkout.
"""
from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

vuoro_identity = pytest.importorskip("vuoro_service.identity")
gateway_identity = pytest.importorskip("vuoro_service.gateway_identity")
jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.requests import Request

from sprintctl.operator_request import RequestRefused
from sprintctl.recipient_admission_adapter import (
    lookup_for_invocation,
    requester_from_invocation_context,
)

assert version("vuoro-service") == "0.1.104"

ISSUER = "https://gateway.example"
WORKSPACE = "01K11111111111111111111111"
REQUEST_ID = "01K33333333333333333333333"
SUBJECT = "subject-42"


def _resolver_and_token(tmp_path: Path, *, authorities=None, repos=None):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_file = tmp_path / "gateway-public.pem"
    key_file.write_bytes(public)
    resolver = gateway_identity.GatewayAssertionIdentityResolver.from_file(
        key_file,
        issuer=ISSUER,
        environment="adapter-test",
        expected_workspace_id=WORKSPACE,
        allowed_repo_ids=frozenset({"repo-a", "repo-b"}),
    )
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": ISSUER,
        "aud": "vuoro-service",
        "sub": "github:42",
        "workspace_id": WORKSPACE,
        "actor": "github:42",
        "subject": SUBJECT,
        "principal_epoch": 7,
        "authorities": list(authorities if authorities is not None else ["work:evidence"]),
        "repo_ids": list(repos if repos is not None else ["repo-a"]),
        "request_id": REQUEST_ID,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 30,
        "jti": REQUEST_ID,
    }
    token = jwt.encode(
        claims,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="EdDSA",
        headers={"typ": "JWT", "kid": "gateway-2026-01"},
    )
    request = Request({"type": "http", "headers": [
        (b"x-vuoro-identity", token.encode()),
        (b"x-request-id", REQUEST_ID.encode()),
    ]})
    request.state.vuoro_invocation_request_id = REQUEST_ID
    return resolver(request)


def _context(identity, repo_id="repo-a"):
    return SimpleNamespace(identity=identity, repo_id=repo_id)


def test_real_signed_gateway_identity_builds_owner_requester_context(tmp_path):
    identity = _resolver_and_token(tmp_path)
    requester = requester_from_invocation_context(_context(identity))

    assert requester.repo_id == "repo-a"
    assert requester.workspace_id == WORKSPACE
    assert requester.principal_id == f"{ISSUER}:{SUBJECT}:7"
    assert requester.authorities == frozenset({"work:evidence"})


@pytest.mark.parametrize(
    ("identity_factory", "repo_id", "code"),
    [
        (lambda p: _resolver_and_token(p, authorities=["work:read"]), "repo-a", "authority-required"),
        (lambda p: _resolver_and_token(p, repos=["repo-a"]), "repo-b", "repo-unauthorized"),
    ],
)
def test_signed_identity_without_required_scope_or_repo_is_refused_before_sql(
    tmp_path, identity_factory, repo_id, code
):
    identity = identity_factory(tmp_path)

    class NoSqlCursor:
        def execute(self, *args, **kwargs):
            raise AssertionError("refusal must happen before any SQL")

    with pytest.raises(RequestRefused) as exc:
        lookup_for_invocation(
            NoSqlCursor(),
            SimpleNamespace(repo_id=repo_id),
            _context(identity, repo_id),
            request_id="opaque",
        )
    assert exc.value.code == code


def test_authorized_identity_cannot_be_used_with_another_store_before_sql(tmp_path):
    identity = _resolver_and_token(tmp_path)

    class NoSqlCursor:
        def execute(self, *args, **kwargs):
            raise AssertionError("repository mismatch must refuse before any SQL")

    with pytest.raises(RequestRefused) as exc:
        lookup_for_invocation(
            NoSqlCursor(),
            SimpleNamespace(repo_id="different-repo"),
            _context(identity, "repo-a"),
            request_id="opaque",
            request_revision=1,
            resolution_event_revision=1,
            work_item_id=1,
            work_revision="status:v1",
            binding_id="opaque",
            binding_revision=1,
        )
    assert exc.value.code == "repository-mismatch"


def test_lookalike_or_static_identity_without_gateway_provenance_is_refused(tmp_path):
    Identity = vuoro_identity.Identity
    forged = SimpleNamespace(
        principal_id=f"{ISSUER}:{SUBJECT}:7",
        principal_epoch=7,
        gateway_issuer=ISSUER,
        gateway_subject=SUBJECT,
        workspace_id=WORKSPACE,
        authorities=frozenset({"work:evidence"}),
        expires_at=datetime.now(UTC),
        authorizes_repo=lambda repo_id: True,
    )
    static = Identity(
        actor="static",
        environment="adapter-test",
        authorities=frozenset({"work:evidence"}),
        repo_ids=frozenset({"repo-a"}),
        workspace_id=WORKSPACE,
        principal_id=f"{ISSUER}:{SUBJECT}:7",
        principal_epoch=7,
        expires_at=datetime.now(UTC),
    )
    for identity in (forged, static):
        with pytest.raises(RequestRefused) as exc:
            requester_from_invocation_context(_context(identity))
        assert exc.value.code in {
            "gateway-identity-required",
            "gateway-provenance-unavailable",
        }
