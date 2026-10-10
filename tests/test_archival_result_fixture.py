"""Real disposable owner records, archival import and native target reads.

Opt-in: ARCHIVE_PG_BIN=/path/to/pg16 pytest this file. The published optional
vuoro-demo consumer supplies its existing owned fixture and native artifact
scenario; it is not a production Sprintctl dependency.
"""

import asyncio
import copy
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest
from sprintctl import archival_result as archive
from sprintctl import pg

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARCHIVE_PG_BIN"), reason="owned PG fixture opt-in"
)


@contextmanager
def shell(store):
    import uvicorn
    from sprintctl.application import WorkApplication
    from sprintctl.vuoro_adapter import register_work_catalog
    from vuoro_service.app import ServiceSettings, create_app
    from vuoro_service.catalog import CatalogRegistry
    from vuoro_service.identity import Identity, StaticBearerIdentityResolver

    registry = CatalogRegistry()
    register_work_catalog(registry, WorkApplication.postgres(store))
    token = secrets.token_urlsafe(32)
    app = create_app(
        settings=ServiceSettings(
            environment_name="disposable-demo",
            environment_class="development",
            compatibility_state="compatible",
        ),
        registry=registry,
        identity_resolver=StaticBearerIdentityResolver(
            {
                token: Identity(
                    actor="archive-reader",
                    environment="disposable-demo",
                    authorities=frozenset({"work:read", "work.effect.get"}),
                    repo_ids=frozenset({store.repo_id}),
                    workspace_id="demo",
                    principal_id="archive:reader:0",
                )
            }
        ),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("owned native reader failed startup")
            time.sleep(0.02)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1]), token
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("owned reader cleanup incomplete")


async def create_result(endpoint, tokens, root, *, capability_in_payload=False):
    from vuoro_demo.scenario import Caller, artifact, register

    class SourceCaller(Caller):
        # Avoid the existing demo's semantic evidence IDs equalling replay keys.
        # Native API still creates/owns every source record and original digest.
        keys: ClassVar[dict[str, str]] = {}

        async def call(self, operation, arguments, **kwargs):
            arguments = copy.deepcopy(arguments)
            key = arguments.get("idempotency_key")
            if key:
                arguments["idempotency_key"] = self.keys.setdefault(
                    key, "source-key-" + secrets.token_hex(16)
                )
            if kwargs.get("key") == "demo-reserve":
                kwargs["key"] = self.keys.setdefault(
                    "demo-reserve", "source-key-" + secrets.token_hex(16)
                )
            if "causal_basis" in arguments:
                old = arguments["causal_basis"]["reserve_idempotency_key"]
                arguments["causal_basis"]["reserve_idempotency_key"] = self.keys[old]
            return await super().call(operation, arguments, **kwargs)

    b = SourceCaller(endpoint, tokens["B"], [])
    v = SourceCaller(endpoint, tokens["verifier"], [])
    try:
        sprint = (
            await b.call(
                "work.sprint.create",
                {
                    "name": "Archive fixture",
                    "goal": "historical only",
                    "status": "active",
                },
            )
        )["sprint"]["id"]
        item = (
            await b.call(
                "work.item.create",
                {
                    "sprint_id": sprint,
                    "track_name": "fixture",
                    "title": "Historical result",
                    "description": "owned fixture",
                },
            )
        )["item"]["id"]
        run = await register(b, "B")
        verifier = await register(v, "verifier")
        result = await artifact(
            b,
            v,
            item,
            root,
            run,
            verifier,
            omit_verification=False,
            wrong_artifact_digest=False,
        )
        lease = (
            await b.call(
                "work.lease.acquire-v1",
                {
                    "item_id": item,
                    "run_id": run,
                    "idempotency_key": "fixture-lease-key",
                },
            )
        )["lease"]
        outcome = await b.call(
            "work.lease.report-outcome-v1",
            {
                "lease_id": lease["lease_id"],
                "run_id": run,
                "outcome": "succeeded",
                "summary": "Historical verified result",
                "payload": {
                    "accepted_intent_id": result["accepted_intent"]["intent_id"],
                    "canonical_intent_digest": result["accepted_intent"][
                        "canonical_intent_digest"
                    ],
                    "release_digest": result["release"]["release_digest"],
                    **(
                        {"source_claim_handle": lease["lease_id"]}
                        if capability_in_payload
                        else {}
                    ),
                },
                "checks": [
                    {"name": "tests", "status": "passed"},
                    {"name": "review", "status": "passed"},
                ],
                "idempotency_key": "fixture-outcome-key",
            },
        )
        # Closed source ownership is fixture setup, before export baseline.
        reservations = await b.call("work.read.reservations", {"item_id": item})
        for r in reservations["reservations"]:
            if r["state"] == "active":
                await b.call(
                    "work.reservation.release",
                    {"reservation_id": r["id"]},
                    key="close-source-" + secrets.token_hex(16),
                )
        return (
            item,
            result,
            outcome,
            lease["lease_id"],
            list(SourceCaller.keys.values()),
        )
    finally:
        await b.client.aclose()
        await v.client.aclose()


def reseal(bundle):
    for row, provenance in zip(bundle["records"], bundle["row_provenance"]):
        provenance["projection_row_digest"] = archive.digest(row)
    bundle["bundle_digest"] = archive.digest(
        {k: v for k, v in bundle.items() if k != "bundle_digest"}
    )
    return bundle


def test_actual_native_source_archive_target_and_refusals(monkeypatch):
    import psycopg
    from vuoro_demo.fixture import fixture
    from vuoro_demo.scenario import REPO, Caller

    root = Path(tempfile.mkdtemp(prefix="vuoro-archive-", dir="/tmp"))
    root.chmod(0o700)
    state = {}
    receipt = {"status": "incomplete"}
    try:
        with fixture(root, Path(os.environ["ARCHIVE_PG_BIN"]), state=state) as (
            endpoint,
            tokens,
        ):
            item, result, outcome, old_lease, old_keys = asyncio.run(
                create_result(endpoint, tokens, root)
            )
            source = pg.get_connection(
                f"dbname=demo_disposable user=demo_migration host={root / 'socket'}"
            )
            source.repo_id = REPO
            with source.conn.cursor():
                # Second tenant repository canary through the actual owner function.
                canary = pg.PgStore(conn=source.conn, repo_id="other-tenant")
                pg.create_sprint(
                    canary, "Other tenant canary", goal="exclude", status="active"
                )
            source.conn.commit()

            def complete_source_digest():
                records = pg.export_from_postgres(source.conn, REPO)
                for table in (
                    *archive.NATIVE,
                    "work_idempotency_ledger",
                    "run_predecessor",
                    "session_note",
                    "work_effect_attempt",
                    "work_effect_attempt_event",
                ):
                    rows = source.conn.execute(
                        f"SELECT * FROM {table} WHERE repo_id=%s", (REPO,)
                    ).fetchall()
                    records += [
                        {"table": table, "data": dict(row)}
                        for row in sorted(
                            rows, key=lambda r: archive.canonical(dict(r))
                        )
                    ]
                return archive.digest(records)

            before = complete_source_digest()
            bundle = pg.export_from_postgres(
                source.conn,
                REPO,
                archive_result={"workspace_id": "demo", "item_id": item},
            )
            after = complete_source_digest()
            assert before == after
            # The native table read happens after another connection commits a
            # new run, but the export keeps its original read-only snapshot.
            original_export = pg.export_from_postgres
            concurrent_run = []

            def interleaved_export(connection, repo_id, **kwargs):
                if kwargs:
                    return original_export(connection, repo_id, **kwargs)
                rows = original_export(connection, repo_id)
                if connection is not source.conn:
                    writer = pg.get_connection(
                        f"dbname=demo_disposable user=demo_migration host={root / 'socket'}"
                    )
                    writer.repo_id = REPO
                    try:
                        registered = pg.register_run(
                            writer,
                            principal_id="demo:B:0",
                            workspace_id="demo",
                            client_id=None,
                            grant_id=None,
                            idempotency_key="concurrent-key-" + secrets.token_hex(16),
                            request_digest="b" * 64,
                            harness_id="fixture",
                            harness_build="1",
                            model_id="script",
                            recipe_id="fixture",
                            observed_profile={},
                        )
                        writer.conn.commit()
                        concurrent_run.append(registered["run_id"])
                    finally:
                        writer.conn.close()
                return rows

            with monkeypatch.context() as m:
                m.setattr(pg, "export_from_postgres", interleaved_export)
                coherent = pg.export_from_postgres(
                    source.conn,
                    REPO,
                    archive_result={"workspace_id": "demo", "item_id": item},
                )
            assert len(concurrent_run) == 1
            assert not any(
                r["table"] == "run" and r["data"]["run_id"] == concurrent_run[0]
                for r in coherent["records"]
            )
            assert (
                complete_source_digest() != before
            )  # concurrent change, not export-side mutation
            # Wrong workspace and predecessor table presence refuse at owner export.
            with pytest.raises(ValueError, match="workspace"):
                pg.export_from_postgres(
                    source.conn,
                    REPO,
                    archive_result={"workspace_id": "wrong", "item_id": item},
                )
            from uuid import uuid4

            original_run = result["accepted_intent"]["run_id"]
            pg.register_run(
                source,
                principal_id="demo:B:0",
                workspace_id="demo",
                client_id=None,
                grant_id=None,
                idempotency_key="predecessor-canary-" + uuid4().hex,
                request_digest="a" * 64,
                harness_id="fixture",
                harness_build="1",
                model_id="script",
                recipe_id="fixture",
                observed_profile={},
                predecessor_run_id=original_run,
            )
            source.conn.commit()
            with pytest.raises(ValueError, match="predecessor"):
                pg.export_from_postgres(
                    source.conn,
                    REPO,
                    archive_result={"workspace_id": "demo", "item_id": item},
                )
            # Owned fixture teardown removes this source; no historical row repaired.
            encoded = archive.canonical(bundle).decode()
            assert old_lease not in encoded and all(k not in encoded for k in old_keys)
            assert "Other tenant canary" not in encoded
            assert bundle["source_only_admissions"]
            assert all(
                a["source_observation_digest"] != a["projection_digest"]
                for a in bundle["source_only_admissions"]
            )
            name = "archive_fixture_" + secrets.token_hex(8) + "_target"
            with psycopg.connect(
                f"dbname=postgres user=demo_migration host={root / 'socket'}",
                autocommit=True,
            ) as admin:
                admin.execute(f"CREATE DATABASE {name} OWNER demo_migration")
                admin.execute(
                    f"COMMENT ON DATABASE {name} IS 'sprintctl:owned-disposable-archive/v1'"
                )
            target = pg.get_connection(
                f"dbname={name} user=demo_migration host={root / 'socket'}"
            )
            target.repo_id = REPO
            pg.init_db(target)
            args = {
                "workspace_id": "demo",
                "item_id": item,
                "expected_bundle_digest": bundle["bundle_digest"],
            }
            # Resealing outer projections cannot hide wrong native digest domains.
            mutations = [
                lambda b: b.update(workspace_id="other"),
                lambda b: b["source"].update(schema=999),
                lambda b: b["records"].append(
                    {"table": "unknown", "repo_id": REPO, "data": {}}
                ),
                lambda b: b["records"][0].update(repo_id="other"),
                lambda b: next(
                    r["data"]
                    for r in b["records"]
                    if r["table"] == "work_outcome_report"
                ).update(payload_digest="0" * 64),
                lambda b: next(
                    r["data"] for r in b["records"] if r["table"] == "work_release"
                ).update(release_digest="0" * 64),
                lambda b: next(
                    r["data"]
                    for r in b["records"]
                    if r["table"] == "work_effect_intent"
                )["verification_binding"]["receipt"]["artifact"].update(
                    digest="sha256:" + "0" * 64
                ),
                lambda b: next(
                    r["data"] for r in b["records"] if r["table"] == "evidence_item"
                ).update(chain_prev_digest="sha256:" + "0" * 64),
                lambda b: b.update(source_only_admissions=[]),
                lambda b: next(
                    r["data"]
                    for r in b["records"]
                    if r["table"] == "work_effect_intent"
                ).update(canonical_intent_digest="0" * 64),
                lambda b: next(
                    r["data"] for r in b["records"] if r["table"] == "work_lease"
                ).update(state="active"),
                lambda b: b["records"].__setitem__(
                    slice(None),
                    [r for r in b["records"] if r["table"] != "work_effect_intent"],
                ),
            ]
            for mutation in mutations:
                bad = copy.deepcopy(bundle)
                mutation(bad)
                # For malformed row count, digest can be correct but closure cannot.
                if len(bad["records"]) == len(bad["row_provenance"]):
                    reseal(bad)
                else:
                    bad["bundle_digest"] = archive.digest(
                        {k: v for k, v in bad.items() if k != "bundle_digest"}
                    )
                target.conn.rollback()
                with pytest.raises((ValueError, KeyError)):
                    pg.import_ndjson(
                        target,
                        bad,
                        archive_result={
                            **args,
                            "expected_bundle_digest": bad["bundle_digest"],
                        },
                    )
                target.conn.rollback()
                assert (
                    target.conn.execute(
                        "SELECT count(*) AS n FROM work_item"
                    ).fetchone()["n"]
                    == 0
                )
            target.conn.rollback()
            with pytest.raises(ValueError, match="digest"):
                pg.import_ndjson(
                    target,
                    bundle,
                    archive_result={**args, "expected_bundle_digest": "0" * 64},
                )
            # Fail inside native insert after base import, proving transaction rollback.
            original_insert = pg._import_row

            def interrupted_insert(cur, table, *a, **kw):
                if table == "work_outcome_report":
                    raise RuntimeError("owned import interrupted")
                return original_insert(cur, table, *a, **kw)

            target.conn.rollback()
            with monkeypatch.context() as m:
                m.setattr(pg, "_import_row", interrupted_insert)
                with pytest.raises(RuntimeError, match="interrupted"):
                    pg.import_ndjson(target, bundle, archive_result=args)
            assert (
                target.conn.execute("SELECT count(*) AS n FROM work_item").fetchone()[
                    "n"
                ]
                == 0
            )
            target.conn.rollback()
            # Independent imports contend on the exact empty-destination guard.
            entered = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            outcomes = []

            def pause_insert(cur, table, *a, **kw):
                if table == "work_item":
                    entered.set()
                    if not release_first.wait(10):
                        raise TimeoutError("owned import barrier")
                return original_insert(cur, table, *a, **kw)

            def competing_import(second):
                own = pg.get_connection(
                    f"dbname={name} user=demo_migration host={root / 'socket'}"
                )
                own.repo_id = REPO
                try:
                    if second:
                        second_started.set()
                    outcomes.append(
                        ("accepted", pg.import_ndjson(own, bundle, archive_result=args))
                    )
                except ValueError as error:
                    outcomes.append(("refused", str(error)))
                finally:
                    own.conn.close()

            with monkeypatch.context() as m:
                m.setattr(pg, "_import_row", pause_insert)
                first_thread = threading.Thread(target=competing_import, args=(False,))
                second_thread = threading.Thread(target=competing_import, args=(True,))
                first_thread.start()
                try:
                    assert entered.wait(10)
                    second_thread.start()
                    assert second_started.wait(10)
                    time.sleep(0.1)
                    assert second_thread.is_alive()
                finally:
                    release_first.set()
                    first_thread.join(15)
                    if second_thread.ident:
                        second_thread.join(15)
                assert not first_thread.is_alive() and not second_thread.is_alive()
            assert sorted(k for k, _ in outcomes) == ["accepted", "refused"]
            counts = next(v for k, v in outcomes if k == "accepted")
            assert "entirely empty" in next(v for k, v in outcomes if k == "refused")
            target.conn.rollback()
            with pytest.raises(ValueError, match="entirely empty"):
                pg.import_ndjson(target, bundle, archive_result=args)
            target.conn.rollback()
            # Separate newly created readonly identity; never source grants/permissions.
            target.conn.execute(
                "CREATE ROLE archive_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
            target.conn.execute("GRANT USAGE ON SCHEMA public TO archive_reader")
            target.conn.execute(
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO archive_reader"
            )
            target.conn.execute(
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC,archive_reader"
            )
            target.conn.execute(
                "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC,archive_reader"
            )
            target.conn.commit()
            target.conn.close()
            reader = pg.get_connection(
                f"dbname={name} user=archive_reader host={root / 'socket'}"
            )
            reader.repo_id = REPO
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                reader.conn.transaction(),
            ):
                reader.conn.execute("UPDATE work_item SET title='bad'")
            reader.conn.rollback()
            with shell(reader) as (target_endpoint, token):

                async def observe():
                    c = Caller(target_endpoint, token, [])
                    try:
                        lease_read = await c.call(
                            "work.lease.read-v1", {"item_id": item}
                        )
                        decisions = await c.call(
                            "work.read.item-decisions", {"item_id": item}
                        )
                        release = await c.call(
                            "work.read.release",
                            {"release_digest": result["release"]["release_digest"]},
                        )
                        intent = await c.call(
                            "work.effect.get-v1",
                            {"intent_id": result["accepted_intent"]["intent_id"]},
                        )
                        await c.refuse(
                            "work.lease.acquire-v1",
                            {
                                "item_id": item,
                                "run_id": result["accepted_intent"]["run_id"],
                                "idempotency_key": "reader-cannot-claim",
                            },
                            "authority-required",
                        )
                        return lease_read, decisions, release, intent
                    finally:
                        await c.client.aclose()

                observed = asyncio.run(observe())
            reader.conn.close()
            source.conn.close()
            assert observed[0]["current_lease"] is None
            assert (
                observed[0]["outcome_reports"][0]["payload_digest"]
                == outcome["report"]["payload_digest"]
            )
            assert (
                observed[1]["decisions"][0]["release_digest"]
                == result["release"]["release_digest"]
            )
            assert (
                observed[3]["intent"]["canonical_intent_digest"]
                == result["accepted_intent"]["canonical_intent_digest"]
            )
            protected = observed[3]["intent"]["acceptance"]["verification"]
            assert protected["receipt"] == result["verification_receipt"]
            assert protected["evidence_digest"] == "sha256:" + archive.digest(
                protected["receipt"]
            )
            assert (
                protected["run_id"]
                and protected["item_id"]
                and protected["entry_digest"]
            )
            receipt = {
                "status": "passed",
                "bundle_digest": bundle["bundle_digest"],
                "counts": counts,
                "native_outcome_digest": outcome["report"]["payload_digest"],
                "release_digest": result["release"]["release_digest"],
                "intent_digest": result["accepted_intent"]["canonical_intent_digest"],
                "source_unchanged": True,
                "source_semantic_snapshot_digest": before,
                "two_connection_imports": "one accepted, one occupied-target refusal",
                "source_concurrency": "writer committed between base/native reads; export retained coherent earlier snapshot, after-comparison changed/inconclusive",
                "atomic_interruption": "native insertion failed after base rows; target remained empty",
                "source_only_admission_projection": bundle["source_only_admissions"],
                "full_native_admission": "unknown",
                "current_authorization": "unknown",
                "evidence_read_limit": "native protected receipt plus owner validation of actual target chain; separate evidence.tail unavailable to fresh reader",
            }
    finally:
        receipt["cleanup"] = state.get("cleanup")
        if os.environ.get("ARCHIVE_RECEIPT"):
            path = Path(os.environ["ARCHIVE_RECEIPT"])
            with path.open("w") as out:
                os.chmod(path, 0o600)
                json.dump(receipt, out, indent=2)
        if state.get("cleanup") == "complete":
            shutil.rmtree(root)


def test_real_canonical_capability_refuses_without_scrubbing():
    from vuoro_demo.fixture import fixture
    from vuoro_demo.scenario import REPO

    root = Path(tempfile.mkdtemp(prefix="vuoro-archive-", dir="/tmp"))
    root.chmod(0o700)
    state = {}
    try:
        with fixture(root, Path(os.environ["ARCHIVE_PG_BIN"]), state=state) as (
            endpoint,
            tokens,
        ):
            item, _, _, _, _ = asyncio.run(
                create_result(endpoint, tokens, root, capability_in_payload=True)
            )
            source = pg.get_connection(
                f"dbname=demo_disposable user=demo_migration host={root / 'socket'}"
            )
            source.repo_id = REPO
            try:
                with pytest.raises(ValueError, match="canonical field"):
                    pg.export_from_postgres(
                        source.conn,
                        REPO,
                        archive_result={"workspace_id": "demo", "item_id": item},
                    )
            finally:
                source.conn.close()
    finally:
        if state.get("cleanup") == "complete":
            shutil.rmtree(root)
