"""End-to-end private receiver checks against disposable PostgreSQL only."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import uuid

import pytest

from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection
from sprintctl import pg, operator_request, operator_request_pg, recipient_binding_pg, recipient_delivery_pg
from sprintctl.recipient_provenance import AuthenticatedRequesterContext

pytestmark = [*PG_MARKS, pytest.mark.gateway_admission]


def test_signed_gateway_request_reaches_owner_and_dedupes_across_resolvers(
    pg_test_scope, tmp_path: Path,
):
    pytest.importorskip("fastapi")
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sprintctl.recipient_delivery_receiver import (
        RECEIVER_AUDIENCE, ROUTE, PrivateDeliveryReceiverConfig,
        create_private_delivery_router,
    )
    from tests.pg.test_recipient_binding import (
        _publish_v2, _register_v1, _signed_delivery_assertion,
    )
    from tests.test_operator_request_pg import _packet
    from tests.test_recipient_provenance import provenance
    from sprintctl.workitemcore import item_status_revision

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
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.commit()
        conn.close()
