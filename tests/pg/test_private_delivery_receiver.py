"""End-to-end private receiver checks against disposable PostgreSQL only."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sprintctl import (
    operator_request,
    operator_request_pg,
    pg,
    recipient_binding_pg,
    recipient_delivery_pg,
)
from sprintctl.recipient_provenance import AuthenticatedRequesterContext
from tests.pg._shared import (
    _PG_URL,
    PG_MARKS,
    assert_disposable_connection,
    dict_row,
    psycopg,
)

pytestmark = [*PG_MARKS, pytest.mark.gateway_admission]


def test_signed_gateway_delivery_and_private_owner_readback(
    pg_test_scope, tmp_path: Path,
):
    pytest.importorskip("fastapi")
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sprintctl.recipient_delivery_receiver import (
        RECEIVER_AUDIENCE,
        ROUTE,
        PrivateDeliveryReceiverConfig,
        create_private_delivery_router,
    )
    from sprintctl.workitemcore import item_status_revision
    from tests.pg.test_recipient_binding import (
        _publish_v2,
        _register_v1,
        _signed_delivery_assertion,
    )
    from tests.test_operator_request_pg import _packet
    from tests.test_recipient_provenance import provenance

    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema_name = "private_receiver_" + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema_name}"')
        cur.execute(f'SET search_path TO "{schema_name}"')
    conn.commit()
    repo_id = pg_test_scope("private-receiver")
    store = pg.PgStore(conn, repo_id)
    workspace = "01K11111111111111111111111"
    request_id = "req_01K33333333333333333333333"
    invocation_id = "01K55555555555555555555555"
    requester = AuthenticatedRequesterContext(
        repo_id=repo_id,
        principal_id="issuer:requester:2",
        workspace_id=workspace,
        gateway_issuer="issuer",
        subject="requester",
        epoch=2,
        authorities=frozenset({"work:evidence"}),
    )
    recipient = replace(
        provenance(),
        workspace_id=workspace,
        token_scopes=frozenset({"vuoro:work.read", "vuoro:evidence.record"}),
    )
    try:
        pg.init_db(store)
        sprint = pg.create_sprint(store, "private-receiver", status="active")
        track = pg.get_or_create_track(store, sprint, "receiver")
        work_item_id = pg.create_work_item(store, sprint, track, "Private receiver fixture")
        args = operator_request.publish_arguments({
            "request_id": request_id,
            "packet": _packet(),
            "dependants": [{"work_item_id": work_item_id, "session_id": "session-a", "runtime_id": "codex"}],
            "idempotency_key": "publish-private-receiver",
        })
        with conn.transaction(), conn.cursor() as cur:
            operator_request_pg.publish(cur, store, requester.principal_id, workspace, args)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE operator_request SET packet=jsonb_set(packet, '{blocker,fresh_until}', "
                "to_jsonb((clock_timestamp()+interval '1 hour')::text)) "
                "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
                (repo_id, workspace, request_id),
            )
            operator_request_pg.report_performed(cur, store, requester.principal_id, workspace, {
                "request_id": request_id, "expected_revision": 1,
                "outcome": "performed", "note": "disposable integration fixture",
            })
            resolved = operator_request_pg.attest(cur, store, requester.principal_id, workspace, {
                "request_id": request_id, "expected_revision": 2,
                "statement": "disposable owner receipt verified",
                "performed_at": "2026-11-07T00:00:00Z",
            })

        instance_id = "ins_" + "A" * 26
        registered = _register_v1(store, recipient, instance_id)
        with conn.transaction(), conn.cursor() as cur:
            _publish_v2(store, recipient, instance_id,
                        base_revision=registered["declaration_revision"])
            bound = recipient_binding_pg.bind(
                cur, store, recipient, request_id=request_id, work_item_id=work_item_id,
                instance_id=instance_id, idempotency_key="bind-private-receiver",
            )
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s",
                        (repo_id, work_item_id))
            work_revision = item_status_revision(cur.fetchone())
        coords = {
            "request_id": request_id, "request_revision": resolved["revision"],
            "resolution_event_revision": resolved["revision"], "work_item_id": work_item_id,
            "work_revision": work_revision, "binding_id": bound["binding_id"],
            "binding_revision": bound["revision"],
        }
        with conn.transaction(), conn.cursor() as cur:
            owner = recipient_binding_pg.lookup_for_recipient_admission(
                cur, store, requester, **coords,
            )
        expected = recipient_delivery_pg._expected_binding(requester, owner)
        admission, admission_keys = _signed_delivery_assertion(expected)

        gateway_private = Ed25519PrivateKey.generate()
        public_key_path = tmp_path / "gateway-public.pem"
        public_key_path.write_bytes(gateway_private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        now = int(datetime.now(UTC).timestamp())
        gateway_claims = {
            "iss": "issuer", "aud": RECEIVER_AUDIENCE,
            "sub": "requester", "workspace_id": workspace, "actor": "requester",
            "subject": "requester", "principal_epoch": 2,
            "authorities": ["work:evidence", "work:read"],
            "repo_ids": [repo_id], "request_id": invocation_id,
            "iat": now, "nbf": now - 1, "exp": now + 30,
            "jti": "gateway-one-use-" + invocation_id,
        }
        private_pem = gateway_private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        gateway_assertion = jwt.encode(
            gateway_claims, private_pem, algorithm="EdDSA",
            headers={"typ": "JWT", "kid": "gateway-test"},
        )
        payload = {
            "schema": "operator-request-delivery/v1", "invocation_id": invocation_id,
            "request_id": request_id, "repo_id": repo_id,
            "assertion": admission, "coords": coords,
        }
        headers = {"X-Vuoro-Identity": gateway_assertion, "X-Request-ID": invocation_id}

        def config():
            @contextmanager
            def fresh_store(scoped_repo_id):
                fresh = psycopg.connect(_PG_URL, row_factory=dict_row)
                assert_disposable_connection(fresh)
                fresh.execute(f'SET search_path TO "{schema_name}"')
                fresh.commit()
                try:
                    yield pg.PgStore(fresh, scoped_repo_id)
                finally:
                    fresh.close()

            return PrivateDeliveryReceiverConfig(
                enabled=True,
                gateway_public_key_file=public_key_path,
                gateway_issuer="issuer",
                gateway_environment="receiver-test",
                expected_workspace_id=workspace,
                allowed_repo_ids=frozenset({repo_id}),
                gateway_key_id="gateway-test",
                replay_not_before=now - 1,
                private_ingress_guard=lambda: True,
                store_lease_for_repo=fresh_store,
                admission_keys=admission_keys,
                admission_issuer="recipient-admission-test",
            )

        def post_once():
            app = FastAPI()
            app.include_router(create_private_delivery_router(config()))
            with TestClient(app) as client:
                return client.post(ROUTE, json=payload, headers=headers)

        first = post_once()
        second = post_once()  # fresh resolver + separate PG connection
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["message_id"] == second.json()["message_id"]
        assert second.json()["historical_replay"] is True

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n, min(message_id) AS message_id FROM operator_request_delivery "
                        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
                        (repo_id, workspace, request_id))
            delivery_rows = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM operator_request_delivery_key "
                        "WHERE repo_id=%s AND workspace_id=%s AND sender_principal_id=%s AND idempotency_key=%s",
                        (repo_id, workspace, requester.principal_id, f"gateway:{invocation_id}"))
            key_rows = cur.fetchone()
        assert delivery_rows["n"] == 1
        assert delivery_rows["message_id"] == first.json()["message_id"]
        assert key_rows["n"] == 1
        conn.commit()

        # The private read API uses a separate signed audience. Every call
        # rebuilds the resolver and obtains a fresh owner DB connection.
        from sprintctl.recipient_delivery_read_receiver import (
            READ_AUDIENCE,
            RECEIPT_ROUTE,
            RESOLVE_ROUTE,
            PrivateDeliveryReadConfig,
            create_private_delivery_read_router,
        )
        from sprintctl.recipient_provenance import AuthenticatedOwnerIdentity

        def signed_read_token(read_invocation_id, *, subject="requester", epoch=2,
                              audience=READ_AUDIENCE):
            issued_at = int(datetime.now(UTC).timestamp())
            claims = {
                "iss": "issuer", "aud": audience,
                "sub": subject, "workspace_id": workspace, "actor": subject,
                "subject": subject, "principal_epoch": epoch,
                "authorities": ["work:evidence", "work:read"],
                "repo_ids": [repo_id], "request_id": read_invocation_id,
                "iat": issued_at, "nbf": issued_at - 1, "exp": issued_at + 30,
                "jti": "gateway-read-" + read_invocation_id,
            }
            return jwt.encode(
                claims, private_pem, algorithm="EdDSA",
                headers={"typ": "JWT", "kid": "gateway-test"},
            )

        @contextmanager
        def read_fresh_store(scoped_repo_id):
            fresh = psycopg.connect(_PG_URL, row_factory=dict_row)
            assert_disposable_connection(fresh)
            fresh.execute(f'SET search_path TO "{schema_name}"')
            fresh.commit()
            try:
                yield pg.PgStore(fresh, scoped_repo_id)
            finally:
                fresh.close()

        disabled_read = create_private_delivery_read_router(PrivateDeliveryReadConfig())
        assert disabled_read.routes == []

        def read_once(operation, payload, *, token=None, lease=read_fresh_store,
                      service_guard=lambda _request: True):
            token = token or signed_read_token(payload["invocation_id"])
            read_now = int(datetime.now(UTC).timestamp())
            read_config = PrivateDeliveryReadConfig(
                enabled=True,
                gateway_public_key_file=public_key_path,
                gateway_issuer="issuer",
                gateway_environment="receiver-test",
                expected_workspace_id=workspace,
                allowed_repo_ids=frozenset({repo_id}),
                gateway_key_id="gateway-test",
                replay_not_before=read_now - 1,
                private_ingress_guard=lambda: True,
                trusted_cloud_adapter_guard=service_guard,
                store_lease_for_repo=lease,
            )
            app = FastAPI()
            app.include_router(create_private_delivery_read_router(read_config))
            route = RESOLVE_ROUTE if operation == "resolve" else RECEIPT_ROUTE
            with TestClient(app) as client:
                return client.post(
                    route, json=payload,
                    headers={"X-Vuoro-Identity": token,
                             "X-Request-ID": payload["invocation_id"]},
                )

        resolve_id = "01K88888888888888888888888"
        resolve_body = {
            "schema": "operator-request-delivery-read/v1",
            "invocation_id": resolve_id, "repo_id": repo_id, "request_id": request_id,
            "binding_id": bound["binding_id"],
        }
        resolved_response = read_once("resolve", resolve_body)
        assert resolved_response.status_code == 200, resolved_response.text
        resolved_body = resolved_response.json()
        candidate = resolved_body
        assert candidate["coords"]["binding_id"] == bound["binding_id"]
        assert candidate["recipient"]["effective_authority"] == ["work:read"]
        assert set(candidate["recipient"]["effective_scopes"]) == {"work:read", "work:evidence"}
        wrong_audience = read_once(
            "resolve", {**resolve_body, "invocation_id": invocation_id},
            token=gateway_assertion,
        )
        assert wrong_audience.status_code == 401
        multi_audience = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K55555555555555555555556"},
            token=signed_read_token(
                "01K55555555555555555555556", audience=[READ_AUDIENCE, "vuoro-service"],
            ),
        )
        assert multi_audience.status_code == 401
        guard_refusal = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K11111111111111111111114"},
            service_guard=lambda _request: False,
        )
        assert guard_refusal.status_code == 404

        receipt_id = "01K99999999999999999999999"
        receipt_body = {
            "schema": "operator-request-delivery-read/v1",
            "invocation_id": receipt_id, "repo_id": repo_id, "request_id": request_id,
            "prior_invocation_id": invocation_id,
        }
        receipt_response = read_once("receipts", receipt_body)
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = receipt_response.json()["receipt"]
        assert receipt["message_id"] == first.json()["message_id"]
        assert not ({"recipient_principal_id", "recipient_instance_id", "admission_jti",
                     "grant_id", "client_id", "token_scopes"} & set(receipt))
        foreign_receipt = read_once(
            "receipts", {**receipt_body, "invocation_id": "01K77777777777777777777778"},
            token=signed_read_token("01K77777777777777777777778", subject="other", epoch=9),
        )
        assert foreign_receipt.status_code == 404

        # A gateway assertion signs identity/invocation, not the owner request
        # ID in this body. Reusing it at a fresh resolver with a foreign owner
        # coordinate must still be refused by the owner authorization query.
        foreign_body = {**resolve_body, "request_id": "req_01K77777777777777777777777"}
        tampered = read_once("resolve", foreign_body,
                             token=signed_read_token(resolve_id))
        assert tampered.status_code == 404
        foreign_principal = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K66666666666666666666666"},
            token=signed_read_token("01K66666666666666666666666", subject="other", epoch=9),
        )
        assert foreign_principal.status_code == 404

        # Changed work status makes stored dependant revisions stale; lookup
        # omits that candidate instead of reusing old coordinates.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT status FROM work_item WHERE repo_id=%s AND id=%s",
                        (repo_id, work_item_id))
            current_status = cur.fetchone()["status"]
            next_status = "blocked" if current_status != "blocked" else "active"
            cur.execute("UPDATE work_item SET status=%s WHERE repo_id=%s AND id=%s",
                        (next_status, repo_id, work_item_id))
            cur.execute("SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s",
                        (repo_id, work_item_id))
            changed_revision = item_status_revision(cur.fetchone())
        stale_work = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K44444444444444444444444"},
        )
        assert stale_work.status_code == 404

        with conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE operator_request_dependant SET work_revision=%s WHERE repo_id=%s "
                        "AND workspace_id=%s AND request_id=%s AND work_item_id=%s",
                        (changed_revision, repo_id, workspace, request_id, work_item_id))
            cur.execute("UPDATE operator_request SET packet=jsonb_set(packet, "
                        "'{blocker,fresh_until}', to_jsonb((clock_timestamp()-interval '1 second')::text)) "
                        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
                        (repo_id, workspace, request_id))
        expired = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K33333333333333333333334"},
        )
        assert expired.status_code == 404
        # The old exact receipt remains readable after expiry and does not
        # certify current eligibility or recipient consumption.
        historical = read_once(
            "receipts", {**receipt_body, "invocation_id": "01K33333333333333333333335"},
        )
        assert historical.status_code == 200
        assert historical.json()["receipt"]["message_id"] == receipt["message_id"]

        # A read failure returns a generic retryable unknown with no internal
        # exception detail. A null exact receipt is only a snapshot, not proof
        # that an in-flight writer cannot still commit.
        def unavailable(_repo_id):
            raise RuntimeError("private database connection details")

        uncertain = read_once("receipts", {**receipt_body,
                                             "invocation_id": "01K22222222222222222222222"},
                              lease=unavailable)
        assert uncertain.status_code == 503
        assert "private database" not in uncertain.text

        # Revoke the recipient binding after the historical receipt exists.
        # Exact current resolution is refused, but owner-authorized receipt recovery is
        # still safe and remains requester-scoped.
        owner_identity = AuthenticatedOwnerIdentity(
            principal_id=recipient.principal_id,
            workspace_id=workspace,
            gateway_issuer=recipient.gateway_issuer,
            subject=recipient.subject,
            epoch=recipient.epoch,
        )
        with conn.transaction(), conn.cursor() as cur:
            recipient_binding_pg.withdraw(
                cur, store, owner_identity, request_id=request_id,
                binding_id=bound["binding_id"], expected_revision=bound["revision"],
            )
            cur.execute("UPDATE operator_request SET packet=jsonb_set(packet, "
                        "'{blocker,fresh_until}', to_jsonb((clock_timestamp()+interval '1 hour')::text)) "
                        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
                        (repo_id, workspace, request_id))
        revoked = read_once(
            "resolve", {**resolve_body, "invocation_id": "01K11111111111111111111112"},
        )
        assert revoked.status_code == 404
        after_revoke = read_once(
            "receipts", {**receipt_body, "invocation_id": "01K11111111111111111111113"},
        )
        assert after_revoke.status_code == 200
        assert after_revoke.json()["receipt"]["message_id"] == receipt["message_id"]
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.commit()
        conn.close()
