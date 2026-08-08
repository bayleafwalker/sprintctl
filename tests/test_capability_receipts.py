import hashlib
import json
from datetime import datetime, timezone

import pytest

from sprintctl import contracts, db, maintain
from sprintctl.cli import cli


RECEIPT_ID = "sprintctl.2026-07-13.close-boundary"
RECEIPT_SHA256 = "a" * 64


def _draft_payload(**overrides):
    project = overrides.get("project", "sprintctl")
    receipt_id = overrides.get("receipt_id", RECEIPT_ID)
    payload = {
        "project": project,
        "receipt_id": receipt_id,
        "receipt_path": (
            f"/projects/dev/_artifacts/{project}/capability/receipts/{receipt_id}.json"
        ),
        "receipt_sha256": RECEIPT_SHA256,
    }
    payload.update(overrides)
    return payload


def _receipt_bytes(
    sprint_id,
    boundary_event_id,
    *,
    project="sprintctl",
    receipt_id=RECEIPT_ID,
    **overrides,
):
    receipt = {
        "schema_version": "capability-receipt/v1",
        "id": receipt_id,
        "project": project,
        "status": "draft",
        "publication": "private",
        "boundary": {
            "kind": "sprint-close",
            "ref": {
                "kind": "sprint-event",
                "source": f"sprintctl:{project}:sprint:{sprint_id}",
                "revision": f"event:{boundary_event_id}",
            },
        },
    }
    receipt.update(overrides)
    return json.dumps(receipt, sort_keys=True).encode()


def _payload_for_bytes(receipt_bytes, **overrides):
    return _draft_payload(
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        **overrides,
    )


def _close_and_prepare_receipt(conn, sprint_id, monkeypatch, *, project="sprintctl"):
    boundary_event_id = db.close_sprint_with_boundary_event(conn, sprint_id, "operator")
    receipt_bytes = _receipt_bytes(
        sprint_id,
        boundary_event_id,
        project=project,
        receipt_id=f"{project}.2026-07-13.close-boundary",
    )
    monkeypatch.setattr(
        contracts,
        "_read_capability_receipt_bytes",
        lambda receipt_path: receipt_bytes,
    )
    return boundary_event_id, _payload_for_bytes(
        receipt_bytes,
        project=project,
        receipt_id=f"{project}.2026-07-13.close-boundary",
    )


class TestCapabilityReceiptDraftedPayload:
    def test_canonical_payload_contains_only_typed_pointer_fields(self):
        payload = _draft_payload(boundary_summary="Explicit sprint close")

        result = contracts.canonicalize_event_payload(
            contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            payload,
        )

        assert result == payload
        assert list(result) == [
            "project",
            "receipt_id",
            "receipt_path",
            "receipt_sha256",
            "boundary_summary",
        ]

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (_draft_payload(project="../sprintctl"), "project must be a portable identifier"),
            (_draft_payload(receipt_id="other.receipt"), "must start with '<project>.'"),
            (_draft_payload(receipt_path="/tmp/private.json"), "receipt_path must be exactly"),
            (_draft_payload(receipt_sha256="A" * 64), "64 lowercase hexadecimal"),
            (_draft_payload(receipt_sha256="a" * 63), "64 lowercase hexadecimal"),
            (_draft_payload(receipt_body={"before": "private"}), "unknown fields: receipt_body"),
            (_draft_payload(model_output="private prose"), "unknown fields: model_output"),
            (_draft_payload(boundary_summary="first\nsecond"), "must be one line"),
        ],
    )
    def test_rejects_invalid_or_privacy_expanding_payloads(self, payload, message):
        with pytest.raises(ValueError, match=message):
            contracts.canonicalize_event_payload(
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload,
            )

    def test_rejects_missing_required_pointer_field(self):
        payload = _draft_payload()
        del payload["receipt_sha256"]

        with pytest.raises(ValueError, match="missing fields: receipt_sha256"):
            contracts.canonicalize_event_payload(
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload,
            )

    def test_sqlite_event_write_uses_shared_canonicalizer(
        self,
        conn,
        active_sprint,
        monkeypatch,
    ):
        _, payload = _close_and_prepare_receipt(
            conn,
            active_sprint["id"],
            monkeypatch,
        )
        event_id = db.create_event(
            conn,
            active_sprint["id"],
            "drafting-agent",
            contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            payload=payload,
        )

        event = next(
            event
            for event in db.list_events(conn, active_sprint["id"])
            if event["id"] == event_id
        )
        assert json.loads(event["payload"]) == payload

    def test_cli_records_pointer_but_never_receipt_body(
        self,
        runner,
        conn,
        active_sprint,
        db_path,
        monkeypatch,
    ):
        _, payload = _close_and_prepare_receipt(
            conn,
            active_sprint["id"],
            monkeypatch,
        )
        result = runner.invoke(
            cli,
            [
                "event",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--type",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                "--actor",
                "drafting-agent",
                "--payload",
                json.dumps(payload),
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        event = next(
            event
            for event in db.list_events(conn, active_sprint["id"])
            if event["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
        )
        assert json.loads(event["payload"]) == payload

        private_payload = {**payload, "receipt_body": {"before": "private"}}
        rejected = runner.invoke(
            cli,
            [
                "event",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--type",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                "--actor",
                "drafting-agent",
                "--payload",
                json.dumps(private_payload),
            ],
        )

        assert rejected.exit_code == 1
        assert "unknown fields: receipt_body" in rejected.output
        assert len(db.list_events(conn, active_sprint["id"])) == 2


class TestCapabilityReceiptAuthority:
    def test_generic_api_and_cli_cannot_forge_close_boundary(
        self,
        runner,
        conn,
        active_sprint,
        db_path,
    ):
        payload = {"previous_status": "active", "status": "closed"}
        with pytest.raises(ValueError, match="reserved; use the atomic sprint close"):
            db.create_event(
                conn,
                active_sprint["id"],
                "forger",
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                payload=payload,
            )

        result = runner.invoke(
            cli,
            [
                "event",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--type",
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                "--actor",
                "forger",
                "--payload",
                json.dumps(payload),
            ],
        )

        assert result.exit_code == 1
        assert "reserved; use the atomic sprint close" in result.output
        assert db.list_events(conn, active_sprint["id"]) == []

    @pytest.mark.parametrize(
        "event_type",
        [
            "capability-receipt-drafed",
            "capability-receipt-ratified",
            contracts.CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE,
        ],
    )
    def test_generic_api_rejects_reserved_lifecycle_names(self, conn, active_sprint, event_type):
        with pytest.raises(ValueError, match="reserved"):
            db.create_event(
                conn,
                active_sprint["id"],
                "agent",
                event_type,
                payload={},
            )

    def test_receipt_requires_closed_sprint(self, conn, active_sprint):
        with pytest.raises(ValueError, match="requires a closed sprint"):
            db.create_event(
                conn,
                active_sprint["id"],
                "agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_draft_payload(),
            )

    def test_receipt_requires_exactly_one_local_boundary(self, conn):
        sprint_id = db.create_sprint(conn, "Closed without boundary", status="closed")

        with pytest.raises(ValueError, match="exactly one local sprint-close-boundary"):
            db.create_event(
                conn,
                sprint_id,
                "agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_draft_payload(),
            )

    def test_receipt_rejects_duplicate_local_boundaries(self, conn, monkeypatch):
        sprint_id = db.create_sprint(conn, "Duplicate boundary", status="active")
        boundary_event_id, payload = _close_and_prepare_receipt(
            conn,
            sprint_id,
            monkeypatch,
        )
        db._insert_event(
            conn,
            sprint_id,
            "internal-test",
            contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
            payload={"previous_status": "active", "status": "closed"},
        )
        conn.commit()

        with pytest.raises(ValueError, match="exactly one local sprint-close-boundary"):
            db.create_event(
                conn,
                sprint_id,
                "agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )

        assert boundary_event_id > 0

    def test_sqlite_optional_project_binding_rejects_mismatch(
        self,
        conn,
        monkeypatch,
    ):
        sprint_id = db.create_sprint(conn, "Project binding", status="active")
        _, payload = _close_and_prepare_receipt(conn, sprint_id, monkeypatch)

        with pytest.raises(ValueError, match="match the owning repository 'other'"):
            db.create_event(
                conn,
                sprint_id,
                "agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
                expected_project="other",
            )


class TestCapabilityReceiptFileVerification:
    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"schema_version": "capability-receipt/v0"}, "schema_version"),
            ({"id": "sprintctl.other"}, "receipt id"),
            ({"project": "other"}, "receipt project"),
            ({"status": "ratified"}, "receipt status"),
            ({"publication": "candidate"}, "receipt publication"),
            ({"boundary": {"kind": "release", "ref": {}}}, "boundary.kind"),
            (
                {
                    "boundary": {
                        "kind": "sprint-close",
                        "ref": {
                            "kind": "sprint-event",
                            "source": "sprintctl:other:sprint:7",
                            "revision": "event:11",
                        },
                    },
                },
                "boundary.ref.source",
            ),
            (
                {
                    "boundary": {
                        "kind": "sprint-close",
                        "ref": {
                            "kind": "sprint-event",
                            "source": "sprintctl:sprintctl:sprint:7",
                            "revision": "event:12",
                        },
                    },
                },
                "boundary.ref.revision",
            ),
        ],
    )
    def test_minimal_receipt_facts_must_match(
        self,
        monkeypatch,
        overrides,
        message,
    ):
        receipt_bytes = _receipt_bytes(7, 11, **overrides)
        payload = _payload_for_bytes(receipt_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: receipt_bytes,
        )

        with pytest.raises(ValueError, match=message):
            contracts.verify_capability_receipt_draft_pointer(
                payload,
                sprint_id=7,
                boundary_event_id=11,
            )

    def test_digest_must_match_exact_file_bytes(self, monkeypatch):
        receipt_bytes = _receipt_bytes(7, 11)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: receipt_bytes,
        )

        with pytest.raises(ValueError, match="does not match the referenced file"):
            contracts.verify_capability_receipt_draft_pointer(
                _draft_payload(receipt_sha256="0" * 64),
                sprint_id=7,
                boundary_event_id=11,
            )

    def test_file_must_exist_and_contain_json(self, monkeypatch):
        def missing(receipt_path):
            raise ValueError(f"capability receipt file does not exist: {receipt_path}")

        monkeypatch.setattr(contracts, "_read_capability_receipt_bytes", missing)
        with pytest.raises(ValueError, match="file does not exist"):
            contracts.verify_capability_receipt_draft_pointer(
                _draft_payload(),
                sprint_id=7,
                boundary_event_id=11,
            )

        invalid = b"not json"
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: invalid,
        )
        with pytest.raises(ValueError, match="valid UTF-8 JSON"):
            contracts.verify_capability_receipt_draft_pointer(
                _payload_for_bytes(invalid),
                sprint_id=7,
                boundary_event_id=11,
            )


class TestCapabilityReceiptArchiveImport:
    def test_round_trip_demotes_reserved_events_to_non_authoritative_history(
        self,
        runner,
        conn,
        db_path,
        tmp_path,
        monkeypatch,
    ):
        sprint_id = db.create_sprint(conn, "Receipt archive", status="active")
        boundary_event_id, payload = _close_and_prepare_receipt(
            conn,
            sprint_id,
            monkeypatch,
        )
        receipt_event_id = db.create_event(
            conn,
            sprint_id,
            "drafting-agent",
            contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            payload=payload,
        )
        export_path = tmp_path / "receipt-export.json"

        exported = runner.invoke(
            cli,
            ["export", "--sprint-id", str(sprint_id), "--output", str(export_path)],
        )
        assert exported.exit_code == 0, exported.output
        imported = runner.invoke(cli, ["import", "--file", str(export_path)])

        assert imported.exit_code == 0, imported.output
        assert "non-authoritative imported history" in imported.output
        imported_sprint_id = int(imported.output.split(" as #")[1].split(" ")[0])
        imported_events = db.list_events(conn, imported_sprint_id)
        by_type = {event["event_type"]: event for event in imported_events}
        assert contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE not in by_type
        assert contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE not in by_type
        imported_boundary = json.loads(
            by_type[contracts.SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE]["payload"]
        )
        assert imported_boundary == {
            "source_event_id": boundary_event_id,
            "source_event_type": contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
            "source_payload": {"previous_status": "active", "status": "closed"},
        }
        imported_receipt = json.loads(
            by_type[contracts.CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE]["payload"]
        )
        assert imported_receipt["source_event_id"] == receipt_event_id
        assert imported_receipt["source_payload"] == payload

        with pytest.raises(ValueError, match="exactly one local sprint-close-boundary"):
            db.create_event(
                conn,
                imported_sprint_id,
                "agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )

        second_export_path = tmp_path / "receipt-export-second.json"
        second_export = runner.invoke(
            cli,
            [
                "export",
                "--sprint-id",
                str(imported_sprint_id),
                "--output",
                str(second_export_path),
            ],
        )
        assert second_export.exit_code == 0, second_export.output
        second_import = runner.invoke(cli, ["import", "--file", str(second_export_path)])
        assert second_import.exit_code == 0, second_import.output
        second_sprint_id = int(second_import.output.split(" as #")[1].split(" ")[0])
        second_events = {
            event["event_type"]: json.loads(event["payload"])
            for event in db.list_events(conn, second_sprint_id)
        }
        assert (
            second_events[contracts.SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE]
            == imported_boundary
        )
        assert (
            second_events[contracts.CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE]
            == imported_receipt
        )


class TestExplicitSprintCloseBoundary:
    def test_sqlite_close_and_boundary_event_commit_together(self, conn):
        sprint_id = db.create_sprint(
            conn,
            "Boundary",
            status="active",
        )

        event_id = db.close_sprint_with_boundary_event(conn, sprint_id, "operator")

        assert db.get_sprint(conn, sprint_id)["status"] == "closed"
        event = db.list_events(conn, sprint_id)[0]
        assert event["id"] == event_id
        assert event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
        assert event["actor"] == "operator"
        assert json.loads(event["payload"]) == {
            "previous_status": "active",
            "status": "closed",
        }
        assert f"event:{event_id}" == f"event:{event['id']}"

    def test_sqlite_event_failure_rolls_back_status(self, conn, monkeypatch):
        sprint_id = db.create_sprint(conn, "Rollback", status="active")

        def fail_insert(*args, **kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(db, "_insert_event", fail_insert)
        with pytest.raises(RuntimeError, match="injected event failure"):
            db.close_sprint_with_boundary_event(conn, sprint_id, "operator")

        assert db.get_sprint(conn, sprint_id)["status"] == "active"
        assert db.list_events(conn, sprint_id) == []

    def test_cli_json_returns_boundary_event_id_and_revision(
        self,
        runner,
        conn,
        db_path,
    ):
        sprint_id = db.create_sprint(conn, "CLI boundary", status="active")

        result = runner.invoke(
            cli,
            [
                "sprint",
                "status",
                "--id",
                str(sprint_id),
                "--status",
                "closed",
                "--actor",
                "operator",
                "--expected-revision",
                db.sprint_status_revision(db.get_sprint(conn, sprint_id)),
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "closed"
        assert output["boundary_event_id"] > 0
        assert output["boundary_revision"] == f"event:{output['boundary_event_id']}"
        event = db.list_events(conn, sprint_id)[0]
        assert event["id"] == output["boundary_event_id"]
        assert event["actor"] == "operator"

    def test_auto_close_remains_a_non_boundary_operation(self, conn):
        sprint_id = db.create_sprint(
            conn,
            "Housekeeping",
            start_date="2025-01-01",
            end_date="2025-01-31",
            status="active",
        )

        result = maintain.sweep(
            conn,
            sprint_id,
            datetime.now(timezone.utc),
            auto_close=True,
        )

        assert result["auto_closed"] is True
        event_types = {event["event_type"] for event in db.list_events(conn, sprint_id)}
        assert "auto-closed-overdue" in event_types
        assert contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE not in event_types
