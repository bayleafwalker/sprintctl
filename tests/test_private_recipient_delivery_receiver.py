from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.gateway_admission
pytest.importorskip("vuoro_service.identity")
pytest.importorskip("vuoro_service.gateway_identity")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sprintctl.recipient_delivery_receiver import (
    RECEIVER_AUDIENCE,
    RECEIVER_SCHEMA,
    ROUTE,
    PrivateDeliveryReceiverConfig,
    create_private_delivery_router,
)

ISSUER = "test-gateway"
WORKSPACE = "01K11111111111111111111111"
ENVIRONMENT = "test-private-receiver"
REPO = "repo-test"


def _signed_identity(private: Ed25519PrivateKey, invocation_id: str, **changes) -> str:
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": ISSUER,
        "aud": RECEIVER_AUDIENCE,
        "sub": "operator-123",
        "workspace_id": WORKSPACE,
        "actor": "operator-123",
        "subject": "user-123",
        "principal_epoch": 4,
        "authorities": ["work:read", "work:evidence"],
        "repo_ids": [REPO],
        "request_id": invocation_id,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 30,
        "jti": "one-use-" + invocation_id,
    }
    claims.update(changes)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="EdDSA", headers={"typ": "JWT", "kid": "test-gateway"})


def _payload(request_id="req_01K33333333333333333333333",
             invocation_id="01K55555555555555555555555"):
    return {
        "schema": RECEIVER_SCHEMA,
        "invocation_id": invocation_id,
        "request_id": request_id,
        "repo_id": REPO,
        "assertion": "cloud-signed-recipient-admission-fixture",
        "coords": {
            "request_id": request_id,
            "request_revision": 4,
            "resolution_event_revision": 7,
            "work_item_id": 91,
            "work_revision": "item:00000000-0000-0000-0000-000000000000@status:active",
            "binding_id": "binding-17",
            "binding_revision": 3,
        },
    }


def _key_path(tmp_path, private):
    path = tmp_path / "gateway-public.pem"
    path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return path


def _config(tmp_path, private, *, guard, store):
    from contextlib import contextmanager

    @contextmanager
    def store_lease(_repo_id):
        yield store

    return PrivateDeliveryReceiverConfig(
        enabled=True,
        gateway_public_key_file=_key_path(tmp_path, private),
        gateway_issuer=ISSUER,
        gateway_environment=ENVIRONMENT,
        expected_workspace_id=WORKSPACE,
        allowed_repo_ids=frozenset({REPO}),
        gateway_key_id="test-gateway",
        replay_not_before=datetime.now(UTC).timestamp() - 1,
        # A deployment-owned private-network policy is injected here; the
        # HTTP request cannot influence this function's result.
        private_ingress_guard=guard,
        trusted_cloud_adapter_guard=lambda _request: True,
        store_lease_for_repo=store_lease,
        admission_keys={"test-cloud": object()},
        admission_issuer="test-cloud",
    )


def _client(config):
    app = FastAPI()
    app.include_router(create_private_delivery_router(config))
    return TestClient(app)


def test_disabled_receiver_registers_no_route():
    with _client(PrivateDeliveryReceiverConfig()) as client:
        assert client.post(ROUTE).status_code == 404


def test_enabled_receiver_requires_all_trust_inputs():
    try:
        create_private_delivery_router(PrivateDeliveryReceiverConfig(enabled=True))
    except ValueError as exc:
        assert "exact gateway trust, ingress guard, owner, and issuer keys" in str(exc)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("incomplete private receiver config was accepted")


def test_enabled_receiver_requires_independent_service_guard(tmp_path):
    private = Ed25519PrivateKey.generate()
    config = _config(tmp_path, private, guard=lambda: True, store=None)
    with pytest.raises(ValueError, match="exact gateway trust"):
        create_private_delivery_router(
            replace(config, trusted_cloud_adapter_guard=None)
        )


@pytest.mark.parametrize("raises", [False, True])
def test_untrusted_cloud_service_is_hidden_before_owner_lease(tmp_path, raises):
    private = Ed25519PrivateKey.generate()
    lease_calls = []

    def refused(_request):
        if raises:
            raise RuntimeError("private test detail")
        return False

    config = _config(tmp_path, private, guard=lambda: True, store=None)
    config = replace(
        config,
        trusted_cloud_adapter_guard=refused,
        store_lease_for_repo=lambda repo_id: lease_calls.append(repo_id),
    )
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not-found"
    assert lease_calls == []


def test_real_gateway_signed_identity_builds_owner_context_and_replay_is_refused(
    tmp_path, monkeypatch,
):
    private = Ed25519PrivateKey.generate()
    calls = []

    class Conn:
        info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))

        def __init__(self):
            self.close_calls = 0

        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

        def cursor(self):
            from contextlib import nullcontext
            return nullcontext(object())

        def close(self):
            self.close_calls += 1

    store = type("Store", (), {"conn": Conn()})()

    def fake_deliver(cur, owner_store, context, **kwargs):
        calls.append((context, kwargs))
        return {"message_id": "msg_fixture", "historical_replay": False, "converged": False}

    monkeypatch.setattr("sprintctl.recipient_delivery_pg.deliver", fake_deliver)
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}

    with _client(_config(tmp_path, private, guard=lambda: True, store=store)) as client:
        first = client.post(ROUTE, content=json.dumps(payload), headers=headers)
        replay = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    # A second receiver replica has a separate in-memory gateway replay cache.
    # The durable owner idempotency key is still derived from the signed request
    # id, so the owner ledger can converge the duplicate before another insert.
    with _client(_config(tmp_path, private, guard=lambda: True, store=store)) as replica:
        replica_replay = replica.post(ROUTE, content=json.dumps(payload), headers=headers)

    assert first.status_code == 200
    assert first.json()["message_id"] == "msg_fixture"
    assert replay.status_code == 401  # one-use cache on the first receiver
    assert replica_replay.status_code == 200  # owner durable replay key handles this case
    assert replay.json()["error"] == "identity-required"
    assert len(calls) == 2
    context, kwargs = calls[0]
    assert context.identity.principal_id == f"{ISSUER}:user-123:4"
    assert context.identity.workspace_id == WORKSPACE
    assert context.identity.authorizes_repo(REPO)
    assert context.request_id == payload["invocation_id"]
    assert context.idempotency_key == f"gateway:{payload['invocation_id']}"
    assert kwargs["assertion"] == payload["assertion"]
    assert kwargs["coords"] == payload["coords"]
    assert calls[1][0].idempotency_key == context.idempotency_key


def test_wrong_audience_fails_closed(tmp_path):
    private = Ed25519PrivateKey.generate()
    config = _config(tmp_path, private, guard=lambda: True, store=None)
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"], aud="vuoro-service")
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "identity-required"


def test_multi_audience_gateway_assertion_fails_closed(tmp_path):
    private = Ed25519PrivateKey.generate()
    config = _config(tmp_path, private, guard=lambda: True, store=None)
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"],
                            aud=[RECEIVER_AUDIENCE, "vuoro-service"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "identity-required"


def test_untrusted_network_is_hidden_before_identity_resolution(tmp_path):
    private = Ed25519PrivateKey.generate()
    config = _config(tmp_path, private, guard=lambda: False, store=None)
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not-found"


def test_caller_cannot_add_recipient_authority_coordinates(tmp_path):
    private = Ed25519PrivateKey.generate()
    config = _config(tmp_path, private, guard=lambda: True, store=None)
    payload = _payload()
    payload["recipient_principal"] = "forged"
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid-request"


def test_chunked_body_limit_is_incremental_and_does_not_buffer_tail():
    from sprintctl.recipient_delivery_receiver import _read_bounded_body

    class FakeRequest:
        headers = {}

        def __init__(self):
            self.consumed = 0

        async def stream(self):
            for chunk in (b"a" * 16_000, b"b" * 17_000, b"unread-tail"):
                self.consumed += 1
                yield chunk

    request = FakeRequest()
    assert asyncio.run(_read_bounded_body(request)) is None
    assert request.consumed == 2


def test_oversized_declared_body_is_rejected_without_reading():
    from sprintctl.recipient_delivery_receiver import (
        _MAX_BODY_BYTES,
        _read_bounded_body,
    )

    class FakeRequest:
        headers = {"content-length": str(_MAX_BODY_BYTES + 1)}
        consumed = False

        async def stream(self):
            self.consumed = True
            yield b"never-read"

    request = FakeRequest()
    assert asyncio.run(_read_bounded_body(request)) is None
    assert request.consumed is False


def test_non_idle_owner_lease_is_refused_without_receiver_rollback_or_close(tmp_path):
    private = Ed25519PrivateKey.generate()

    class Conn:
        info = SimpleNamespace(transaction_status=SimpleNamespace(name="INTRANS"))
        close_calls = 0

        def close(self):
            self.close_calls += 1

    store = type("Store", (), {"conn": Conn()})()
    payload = _payload()
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    config = _config(tmp_path, private, guard=lambda: True, store=store)
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "delivery-outcome-unknown"
    assert store.conn.close_calls == 0


def test_commit_value_error_is_unknown_and_domain_refusal_is_terminal(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from sprintctl.operator_request import RequestRefused

    private = Ed25519PrivateKey.generate()

    class Conn:
        info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))

        @contextmanager
        def transaction(self):
            yield
            raise ValueError("simulated commit ambiguity")

        def cursor(self):
            from contextlib import nullcontext
            return nullcontext(object())

    store = type("Store", (), {"conn": Conn()})()
    payload = _payload(invocation_id="01K66666666666666666666666")
    token = _signed_identity(private, payload["invocation_id"])
    headers = {"X-Vuoro-Identity": token, "X-Request-ID": payload["invocation_id"]}
    monkeypatch.setattr(
        "sprintctl.recipient_delivery_pg.deliver",
        lambda *_args, **_kwargs: {"message_id": "unknown", "historical_replay": False},
    )
    config = _config(tmp_path, private, guard=lambda: True, store=store)
    with _client(config) as client:
        response = client.post(ROUTE, content=json.dumps(payload), headers=headers)
    assert response.status_code == 503
    assert response.json() == {
        "schema": RECEIVER_SCHEMA, "error": "delivery-outcome-unknown", "retryable": True,
    }

    class NormalConn:
        info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))

        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

        def cursor(self):
            from contextlib import nullcontext
            return nullcontext(object())

    refused_store = type("Store", (), {"conn": NormalConn()})()
    monkeypatch.setattr(
        "sprintctl.recipient_delivery_pg.deliver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RequestRefused("not-found", "hidden")),
    )
    refused_payload = _payload(invocation_id="01K77777777777777777777777")
    refused_token = _signed_identity(private, refused_payload["invocation_id"])
    refused_headers = {"X-Vuoro-Identity": refused_token,
                       "X-Request-ID": refused_payload["invocation_id"]}
    refused_config = _config(tmp_path, private, guard=lambda: True, store=refused_store)
    with _client(refused_config) as client:
        refused = client.post(ROUTE, content=json.dumps(refused_payload), headers=refused_headers)
    assert refused.status_code == 409
    assert refused.json()["error"] == "delivery-refused"
