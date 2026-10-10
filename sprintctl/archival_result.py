"""Fixture-only historical result projection. No serving/export/import endpoint.

Source rows are projected, never installed as replay authority. Canonical native
content digests and original row/projection digests are separate domains.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

SCHEMA = "sprintctl-fixture-archival-result/v1"
NATIVE = (
    "run",
    "evidence_item",
    "work_lease",
    "work_outcome_report",
    "work_effect_intent",
)
MAX_ROWS = 2000
MAX_BYTES = 8_000_000


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x),
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _fixture(conn, *, destination=False):
    """Refuse ambient network/customer sources; this is not a public grant API."""
    host = Path(conn.info.host)
    if not host.is_absolute() or host.name != "socket" or host.is_symlink():
        raise ValueError("owned local fixture socket required")
    root = host.parent
    if (
        root.parent != Path("/tmp")
        or not root.name.startswith("vuoro-archive-")
        or root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
        or stat.S_IMODE(host.stat().st_mode) != 0o700
    ):
        raise ValueError("private owned archival fixture required")
    if not (
        not destination and conn.info.dbname == "demo_disposable"
    ) and not re.fullmatch(
        r"archive_fixture_[0-9a-f]{16}_(source|target)", conn.info.dbname
    ):
        raise ValueError("fixture database identity required")
    expected = "target" if destination else "source"
    if conn.info.dbname != "demo_disposable" and not conn.info.dbname.endswith(
        "_" + expected
    ):
        raise ValueError("wrong fixture database role")
    marker = conn.execute(
        "SELECT shobj_description(oid,'pg_database') AS marker "
        "FROM pg_database WHERE datname=current_database()"
    ).fetchone()
    if marker["marker"] != (
        "vuoro:owned-disposable-demo"
        if conn.info.dbname == "demo_disposable"
        else "sprintctl:owned-disposable-archive/v1"
    ):
        raise ValueError("owned disposable fixture marker required")
    from .pg_migrations import CURRENT_SCHEMA_VERSION

    version = conn.execute("SELECT version FROM schema_version").fetchall()
    if len(version) != 1 or version[0]["version"] != CURRENT_SCHEMA_VERSION:
        raise ValueError("ambiguous owner schema")
    return {
        "database": conn.info.dbname,
        "socket": str(host),
        "schema": version[0]["version"],
    }


def _guard_records(records, repo, workspace):
    from . import pg

    allowed = set(pg._EXPORT_TABLES) | set(NATIVE)
    if not records or len(records) > MAX_ROWS or len(canonical(records)) > MAX_BYTES:
        raise ValueError("archive bound exceeded or empty")
    for r in records:
        if (
            set(r) != {"table", "repo_id", "data"}
            or r["table"] not in allowed
            or r["repo_id"] != repo
        ):
            raise ValueError("unknown table or mixed repository")
        d = r["data"]
        if d.get("workspace_id", workspace) != workspace:
            raise ValueError("wrong workspace")
        if (
            r["table"] == "reservation"
            and d["state"] == "active"
            or r["table"] == "claim_history"
            and d["status"] == "active"
            or r["table"] == "work_lease"
            and d["state"] == "active"
        ):
            raise ValueError("live ownership cannot be archived")
        if r["table"] == "claim_history" and d.get("claim_token"):
            raise ValueError("claim proof cannot be exported")


def _validate(records, repo, workspace, item_id):
    from . import effect_intent, effect_verification, pg, releases

    _guard_records(records, repo, workspace)
    tables = {
        t: [r["data"] for r in records if r["table"] == t]
        for t in set(pg._EXPORT_TABLES) | set(NATIVE)
    }
    items = {d["id"]: d for d in tables["work_item"]}
    decisions = {d["id"]: d for d in tables["work_decision"]}
    rels = {d["release_digest"]: d for d in tables["work_release"]}
    runs = {d["run_id"]: d for d in tables["run"]}
    leases = {d["lease_id"]: d for d in tables["work_lease"]}
    evidence = {(d["run_id"], d["item_id"]): d for d in tables["evidence_item"]}
    subject = items.get(item_id)
    if not subject or subject.get("resolution") != "accepted":
        raise ValueError("selected historical result is not accepted")
    reports = [
        d
        for d in tables["work_outcome_report"]
        if d["work_item_id"] == item_id and d["disposition"] == "settled"
    ]
    if len(reports) != 1:
        raise ValueError("one settled historical result required")
    report = reports[0]
    decision = decisions.get(report["decision_id"])
    if (
        not decision
        or subject["terminal_decision_id"] != decision["id"]
        or decision["work_item_id"] != item_id
        or decision["kind"] != "accept"
        or report["payload_digest"] not in decision["evidence_digests"]
        or decision["release_digest"] not in rels
    ):
        raise ValueError("broken historical Decision/Release join")
    for d in tables["work_release"]:
        # Owner's immutable release digest is over its exact stored canonical basis.
        if (
            releases.release_digest(
                items[d["work_item_id"]]["aggregate_uuid"],
                d["item_revision"],
                d["acceptance_contract"],
                d["context_refs"],
            )
            != d["release_digest"]
        ):
            raise ValueError("wrong Release digest")
    for d in tables["work_outcome_report"]:
        if (
            pg.outcome_payload_digest(
                d["outcome"], d["summary"], d["payload"], d["checks"]
            )
            != d["payload_digest"]
            or d["lease_id"] not in leases
            or d["run_id"] not in runs
            or leases[d["lease_id"]]["run_id"] != d["run_id"]
        ):
            raise ValueError("broken outcome digest/lease/run join")
    for d in tables["work_lease"]:
        if d["run_id"] not in runs or d["work_item_id"] not in items:
            raise ValueError("broken lease closure")
        for key in ("takeover_of", "superseded_by"):
            if d.get(key) and d[key] not in leases:
                raise ValueError("broken takeover closure")
    for run in runs:
        chain = sorted(
            (d for (r, _), d in evidence.items() if r == run),
            key=lambda d: d["chain_seq"],
        )
        prev = None
        for seq, d in enumerate(chain):
            if d["chain_seq"] != seq or d["chain_prev_digest"] != prev:
                raise ValueError("broken evidence chain")
            prev = pg.evidence_entry_digest(d)
    for d in tables["evidence_item"]:
        if d["run_id"] not in runs:
            raise ValueError("broken evidence run closure")
    for d in tables["work_effect_intent"]:
        if d["run_id"] not in runs or d["work_item_id"] not in items:
            raise ValueError("broken intent closure")
        content = {**d, "item_id": d["work_item_id"]}
        if (
            effect_intent.canonical_intent_digest(content)
            != d["canonical_intent_digest"]
        ):
            raise ValueError("wrong canonical intent digest")
        if d["state"] == "proposed":
            raise ValueError("unresolved proposed intent")
        binding = d.get("verification_binding")
        if binding:
            ref = binding
            ev = evidence.get((ref["run_id"], ref["item_id"]))
            if ev is None:
                raise ValueError("missing protected verification")
            detail = effect_verification.validate_receipt(
                pg._evidence_row(ev), pg._effect_row(d), d["release_digest"]
            )
            verifier = runs[ev["run_id"]]
            if (
                binding["receipt"] != detail
                or binding["evidence_digest"] != ev["digest"]
                or binding["entry_digest"] != pg.evidence_entry_digest(ev)
                or binding["verifier_principal"] != verifier["principal_id"]
                or binding["workspace_id"] != verifier["workspace_id"]
                or binding.get("client_id") != verifier["client_id"]
                or binding.get("grant_id") != verifier["grant_id"]
            ):
                raise ValueError(
                    "protected verification binding differs from source evidence"
                )
    payload = report["payload"]
    if "accepted_intent_id" in payload:
        matching = [
            d
            for d in tables["work_effect_intent"]
            if d["intent_id"] == payload["accepted_intent_id"]
        ]
        if (
            len(matching) != 1
            or matching[0]["canonical_intent_digest"]
            != payload.get("canonical_intent_digest")
            or matching[0].get("release_digest") != payload.get("release_digest")
            or payload.get("release_digest") != decision["release_digest"]
        ):
            raise ValueError("missing outcome intent join")
    return report


def _admission_joins(records, admissions, item_id):
    from . import pg

    intents = [
        r["data"]
        for r in records
        if r["table"] == "work_effect_intent" and r["data"]["work_item_id"] == item_id
    ]
    if intents and not admissions:
        raise ValueError("missing required historical admission facts")
    for a in admissions:
        f = a["facts"]
        matching = [d for d in intents if d["intent_id"] == f["intent_id"]]
        if len(matching) != 1:
            raise ValueError("historical admission intent missing")
        intent = matching[0]
        rels = [
            r["data"]
            for r in records
            if r["table"] == "work_release"
            and r["data"]["release_digest"] == f["release_digest"]
        ]
        if (
            len(rels) != 1
            or rels[0]["item_revision"] != f["expected_revision"]
            or rels[0]["work_item_id"] != item_id
            or intent.get("release_digest") != f["release_digest"]
        ):
            raise ValueError("historical admission Release mismatch")
        if not any(
            r["table"] == "release_commit"
            and r["data"]["release_digest"] == f["release_digest"]
            and r["data"]["commit_sha"] == f["commit_sha"]
            for r in records
        ):
            raise ValueError("historical admission commit missing")
        tail = f["evidence_tail"]
        chain = [
            r["data"]
            for r in records
            if r["table"] == "evidence_item" and r["data"]["run_id"] == intent["run_id"]
        ]
        match = [
            d
            for d in chain
            if d["item_id"] == tail["item_id"] and d["chain_seq"] == tail["chain_seq"]
        ]
        if (
            len(match) != 1
            or pg.evidence_entry_digest(match[0]) != tail["entry_digest"]
        ):
            raise ValueError("historical admission evidence link missing")
    if any(
        not any(a["facts"]["intent_id"] == d["intent_id"] for a in admissions)
        for d in intents
    ):
        raise ValueError("incomplete historical admission coverage")


def export_fixture_result(conn, repo_id, *, workspace_id, item_id):
    from . import effect_intent, pg

    identity = _fixture(conn)
    store = pg.PgStore(conn=conn, repo_id=repo_id)
    with pg.repeatable_read_snapshot(store) as snapshot:
        snapshot.conn.execute("SET LOCAL statement_timeout='5s'")
        identity = _fixture(snapshot.conn)
        raw = pg.export_from_postgres(snapshot.conn, repo_id)
        for table in NATIVE:
            rows = snapshot.conn.execute(
                f"SELECT * FROM {table} WHERE repo_id=%s", (repo_id,)
            ).fetchall()
            raw += [
                {
                    "table": table,
                    "repo_id": repo_id,
                    "data": {k: v for k, v in dict(row).items() if k != "repo_id"},
                }
                for row in rows
            ]
        for table in (
            "work_effect_attempt",
            "work_effect_attempt_event",
            "run_predecessor",
        ):
            if snapshot.conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE repo_id=%s", (repo_id,)
            ).fetchone()["n"]:
                raise ValueError("effect attempt or predecessor closure is unsupported")
        _validate(raw, repo_id, workspace_id, item_id)
        ledger = snapshot.conn.execute(
            "SELECT idempotency_key,result FROM work_idempotency_ledger WHERE repo_id=%s "
            "AND workspace_id=%s",
            (repo_id, workspace_id),
        ).fetchall()
        observed = (
            snapshot.conn.execute("SELECT transaction_timestamp() AS at")
            .fetchone()["at"]
            .isoformat()
        )
    secrets = {row["idempotency_key"] for row in ledger}
    leases = {}
    for r in raw:
        d = r["data"]
        for k in ("claim_key", "idempotency_key", "claim_token"):
            if isinstance(d.get(k), str) and d[k]:
                secrets.add(d[k])
        if r["table"] == "work_lease":
            leases[d["lease_id"]] = pg._mint_prefixed_id("lease_")
    replacements = {s: "archive-" + uuid4().hex for s in secrets}
    replacements.update(leases)

    def forbidden(value):
        encoded = canonical(value).decode()
        return any(s in encoded for s in replacements)

    # These original canonical domains must not be rewritten or mislabeled.
    for r in raw:
        d = r["data"]
        protected = None
        if r["table"] == "work_outcome_report":
            protected = {k: d[k] for k in ("outcome", "summary", "payload", "checks")}
        elif r["table"] == "work_effect_intent":
            protected = {k: d[k] for k in effect_intent.DIGEST_FIELDS if k != "item_id"}
            protected["item_id"] = d["work_item_id"]
        elif r["table"] == "evidence_item":
            protected = {
                k: d[k]
                for k in (
                    "item_id",
                    "digest",
                    "chain_seq",
                    "chain_prev_digest",
                    "claims",
                )
            }
        elif r["table"] == "work_release":
            protected = {
                k: d[k]
                for k in ("item_revision", "acceptance_contract", "context_refs")
            }
        if protected is not None and forbidden(protected):
            raise ValueError("source capability in canonical field is unsupported")

    def project(value):
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items()}
        if isinstance(value, list):
            return [project(v) for v in value]
        if isinstance(value, str):
            for old, new in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
                value = value.replace(old, new)
        return value

    projected_fields = {
        "run": {"idempotency_key"},
        "evidence_item": {"idempotency_key"},
        "work_lease": {"lease_id", "claim_key", "takeover_of", "superseded_by"},
        "work_outcome_report": {"lease_id", "idempotency_key"},
        "work_effect_intent": {"idempotency_key"},
        "event": {"payload"},
        "work_decision": {"rationale"},
    }
    records = []
    for r in raw:
        d = dict(r["data"])
        fields = projected_fields.get(r["table"], set())
        for key, value in d.items():
            if key in fields:
                d[key] = project(value)
            elif forbidden(value):
                raise ValueError("capability outside allowed projection field")
        records.append({**r, "data": d})
    # IDs/keys are never removed from digest-bearing content, but descriptive
    # owner events/rationales may project the same alias consistently.
    provenance = [
        {
            "table": r["table"],
            "source_row_digest": digest(r),
            "projection_row_digest": digest(a),
        }
        for r, a in zip(raw, records)
    ]
    admissions = []
    for row in ledger:
        original = row["result"]
        admission = original.get("admission")
        if not admission:
            continue
        if original.get("intent", {}).get("item_id") != item_id:
            continue
        basis = admission["causal_basis"]
        facts = {
            k: basis[k]
            for k in (
                "expected_revision",
                "release_digest",
                "commit_sha",
                "evidence_tail",
            )
        }
        facts["intent_id"] = original["intent"]["intent_id"]
        if forbidden(facts):
            raise ValueError("capability in archival admission facts")
        admissions.append(
            {
                "source_observation_digest": digest(original),
                "facts": facts,
                "projection_digest": digest(facts),
                "omitted": [
                    "causal_basis.reserve_idempotency_key",
                    "run_binding",
                    "reservation_id",
                    "operational_ledger_keys",
                ],
            }
        )
    _validate(records, repo_id, workspace_id, item_id)
    _admission_joins(records, admissions, item_id)
    bundle = {
        "schema": SCHEMA,
        "repo_id": repo_id,
        "workspace_id": workspace_id,
        "item_id": item_id,
        "source": identity,
        "observed_at": observed,
        "records": records,
        "row_provenance": provenance,
        "source_only_admissions": admissions,
        "full_native_admission": "unknown",
        "current_authorization": "unknown",
    }
    if len(canonical(bundle)) > MAX_BYTES:
        raise ValueError("archive byte bound exceeded")
    return {**bundle, "bundle_digest": digest(bundle)}


def import_fixture_result(
    store, bundle, *, workspace_id, item_id, expected_bundle_digest
):
    from psycopg.pq import TransactionStatus

    from . import pg

    if store.conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("archival import requires idle owned fixture connection")
    target = _fixture(store.conn, destination=True)
    if (
        bundle.get("schema") != SCHEMA
        or bundle.get("repo_id") != store.repo_id
        or bundle.get("workspace_id") != workspace_id
        or bundle.get("item_id") != item_id
        or target["schema"] != bundle["source"]["schema"]
        or target["database"] == bundle["source"]["database"]
    ):
        raise ValueError("wrong archive identity or schema")
    content = {k: v for k, v in bundle.items() if k != "bundle_digest"}
    if digest(content) != bundle.get(
        "bundle_digest"
    ) or expected_bundle_digest != bundle.get("bundle_digest"):
        raise ValueError("wrong archive digest")
    admissions = bundle["source_only_admissions"]
    for a in admissions:
        if (
            set(a)
            != {"source_observation_digest", "facts", "projection_digest", "omitted"}
            or set(a["facts"])
            != {
                "expected_revision",
                "release_digest",
                "commit_sha",
                "evidence_tail",
                "intent_id",
            }
            or digest(a["facts"]) != a["projection_digest"]
            or not re.fullmatch("[0-9a-f]{64}", a["source_observation_digest"])
        ):
            raise ValueError("invalid source-only admission projection")
    records = bundle["records"]
    _admission_joins(records, admissions, item_id)
    _validate(records, store.repo_id, workspace_id, item_id)
    if len(records) != len(bundle["row_provenance"]) or any(
        digest(r) != p["projection_row_digest"]
        for r, p in zip(records, bundle["row_provenance"])
    ):
        raise ValueError("wrong projected row digest")
    # Import's public legacy transaction ends by committing. Private deferral
    # keeps that existing implementation and the native closure in one commit.
    store.conn.rollback()  # only our fixture-guard reads; caller had to be idle
    with store.conn.transaction():
        for table in (
            *pg._EXPORT_TABLES,
            *NATIVE,
            "work_effect_attempt",
            "work_effect_attempt_event",
            "work_idempotency_ledger",
            "run_predecessor",
            "session_note",
        ):
            store.conn.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
            if store.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]:
                raise ValueError("disposable target must be entirely empty")
        for record in records:
            columns = {
                r["column_name"]
                for r in store.conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                    (record["table"],),
                ).fetchall()
            } - {"repo_id"}
            if set(record["data"]) != columns:
                raise ValueError("owner column shape mismatch")
        base = [r for r in records if r["table"] in pg._EXPORT_TABLES]
        counts = pg.import_ndjson(store, base, _archive_transaction=True)
        with store.conn.cursor() as cur:
            for table in NATIVE:
                rows = [r for r in records if r["table"] == table]
                for r in rows:
                    data = dict(r["data"])
                    for k, v in data.items():
                        if isinstance(v, (dict, list)):
                            data[k] = json.dumps(v)
                    pg._import_row(cur, table, store.repo_id, data)
                counts[table] = len(rows)
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return counts
