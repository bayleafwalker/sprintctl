"""Optional consumer composition: real edge tools over the real PG owner.

Run with the Vuoro workspace environment on PYTHONPATH. Ordinary owner CI
still proves the owner operations without requiring the consumer package.
"""
import asyncio
import hashlib
import json
import uuid

import pytest

# Collection is explicitly enabled through SPRINTCTL_TEST_E4_EDGE_COMPOSITION.
# Once requested, a missing consumer must fail instead of silently skipping.
import vuoro_mcp_edge
import httpx
import jsonschema

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS
from tests.pg.test_run_evidence import _new_run
from tests.pg.test_work_lease import A, B, _items
from vuoro_mcp_edge.claim_tools import build_toolset as claim_tools
from vuoro_mcp_edge.record_tools import SprintctlRecordStore, build_toolset as record_tools
from vuoro_mcp_edge.toolsets import ToolFailure, ToolsetContext
from vuoro_mcp_edge.work_source import ForwardedIdentity, ShellWorkSource

pytestmark = PG_MARKS


@pytest.mark.parametrize("scope", ["plan", "family"])
@pytest.mark.parametrize("same_family", [False, True])
def test_real_edge_owner_denial_park_explicit_release_and_observed_reclaim(store, scope, same_family):
    app = WorkApplication.postgres(store)
    tag = uuid.uuid4().hex
    item = _items(store)[0]
    old_model = "claude-opus-5-5"
    new_model = "claude-opus-5-5" if same_family else "claude-sonnet-5-5"
    old_run = _new_run(app, "e4-old-" + tag, context=A, model_id=old_model)
    new_run = _new_run(app, "e4-new-" + tag, context=B, model_id=new_model)
    contracts = {c.name: c for c in WORK_OPERATION_CONTRACTS}
    invocations = []

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json={"operations": [{"name": name} for name in contracts]})
        envelope = json.loads(request.content)
        assert envelope["repo_id"] == store.repo_id
        context = {"test-A": A, "test-B": B}[request.headers["X-Vuoro-Identity"]]
        op, args = envelope["operation"], envelope["arguments"]
        invocations.append((context.identity.principal_id, op, args))
        jsonschema.validate(args, contracts[op].input_schema)
        try:
            result = app.invoke(op, args, context)
            jsonschema.validate(result, contracts[op].result_schema)
            return httpx.Response(200, json={"operation": op, "status": "accepted", "result": result})
        except ApplicationRejection as exc:
            return httpx.Response(exc.http_status, json={"operation": op, "status": "rejected",
                "error": {"code": exc.code, "message": str(exc)}})

    async def scenario():
        runs = SprintctlRecordStore(base_url="http://test-owner", timeout=5,
                                   transport=httpx.MockTransport(transport))
        source = ShellWorkSource(base_url="http://test-owner", transport=httpx.MockTransport(transport))
        context = ToolsetContext(env={}, work_source=source, runs=runs)
        tools = {tool.name: tool for group in (claim_tools(context), record_tools(context)) for tool in group.tools}
        forwarded = lambda name, identity: ForwardedIdentity(assertion=name, request_id="e4-test", repo_id=store.repo_id,
                                                            identity=identity)
        a, b = forwarded("test-A", A.identity), forwarded("test-B", B.identity)

        async def call(name, args, caller):
            tool = tools[name]
            return await tool.run(tool.parse(args), caller)

        try:
            claimed = await call("claim_work", {"item_id": item, "run_id": old_run, "idempotency_key": "e4-claim-" + tag}, a)
            observation = {"schema": "vuoro-rate-limit-observation/v1", "observed_at": "2026-10-20T00:00:00Z",
                "status": "rejected", "limit_scope": scope, "model_id": old_model, "model_family": "claude-opus",
                "resets_at": "2026-10-20T01:00:00Z", "reset_source": "harness-structured", "http_status": 429, "overage": None}
            digest = "sha256:" + hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            evidence = await call("append_evidence", {"run_id": old_run, "kind": "rate_limit_event", "ref": "stream-json:synthetic#1",
                "digest": digest, "collector": "test-harness@v1", "validity": {"basis": "bounded", "valid_from": observation["observed_at"],
                    "valid_until": observation["resets_at"], "component_digests": {}}, "claims": [],
                "provenance": {"observation": observation, "private": "PRIVATE-CANARY"}, "idempotency_key": "e4-denial-" + tag}, a)
            # The citation remains resolvable after a later entry advances the tail.
            later = {"run_id": old_run, "kind": "test", "ref": "later", "digest": "sha256:" + "f" * 64,
                "collector": "test", "validity": {"basis": "indefinite", "valid_from": observation["observed_at"],
                    "valid_until": None, "component_digests": {}}, "claims": [], "provenance": {}, "idempotency_key": "e4-later-" + tag}
            await call("append_evidence", later, a)
            report = await call("report_outcome", {"lease_id": claimed["lease"]["lease_id"], "run_id": old_run,
                "outcome": "failed", "disposition": "parked", "reason_ref": f"vuoro-evidence:{old_run}/seq/{evidence['chain_seq']}",
                "idempotency_key": "e4-report-" + tag}, a)
            assert report["settlement_effect"] == "parked"
            with pytest.raises(ToolFailure) as refused:
                await call("claim_work", {"item_id": item, "run_id": new_run, "idempotency_key": "e4-too-early-" + tag}, b)
            assert refused.value.code == "work-parked"
            # Release is a separate coordinator act; the edge never calls it.
            pg.set_work_item_status(store, item, "pending", "coordinator", reason="partial")
            args = {"item_id": item, "run_id": new_run, "idempotency_key": "e4-reclaim-" + tag}
            reclaimed = await call("claim_work", args, b)
            assert "warnings" not in reclaimed
            recorded = reclaimed["reclaim_observation"]["provenance"]["observation"]
            assert recorded["predecessor_model_id"] == old_model
            assert recorded["model_id"] == new_model
            assert recorded["same_family"] is same_family
            assert "PRIVATE-CANARY" not in json.dumps(reclaimed)
            # Replay after another successor entry: exactly one reclaim observation.
            await call("append_evidence", {**later, "run_id": new_run, "idempotency_key": "e4-successor-later-" + tag}, b)
            replay = await call("claim_work", args, b)
            assert replay["reclaim_observation"] == reclaimed["reclaim_observation"]
            with store.conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM evidence_item WHERE repo_id=%s AND run_id=%s AND kind='parked_reclaim'",
                            (store.repo_id, new_run))
                assert cur.fetchone()["n"] == 1
                cur.execute("SELECT count(*) AS n FROM work_lease WHERE repo_id=%s AND work_item_id=%s AND state='active'",
                            (store.repo_id, item))
                assert cur.fetchone()["n"] == 1
            store.conn.rollback()
            assert all(args.get("run_id") != old_run for principal, op, args in invocations if principal == B.identity.principal_id)
        finally:
            await runs.aclose()
            await source.aclose()

    asyncio.run(scenario())
