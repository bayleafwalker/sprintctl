"""Verify the diagnostic's canonical comparison against real owner storage."""

from contextlib import closing

from sprintctl import authority_config, authority_doctor, outbox
from sprintctl.application import WorkApplication
from tests.pg._shared import PG_MARKS, pg
from tests.test_work_application import _context

pytestmark = PG_MARKS


def test_real_owner_ledger_matches_readonly_producer_snapshot(store, tmp_path, monkeypatch):
    paths = authority_config.authority_command_paths(repo_root=tmp_path)
    with closing(outbox.open_outbox(paths.outbox_path)) as producer:
        record = outbox.append_observation(producer, event_type="work.completed", actor="owner", payload={"item": "temporary-test"})
    pg.ingest_records(store, [record])
    app = WorkApplication.postgres(store)
    monkeypatch.setattr(authority_doctor.served, "identity_current", lambda *a, **k:
                        app.invoke("work.identity.current", {}, _context(actor="owner")))
    monkeypatch.setattr(authority_doctor.served, "read_records", lambda *a, **k:
                        app.invoke("work.read.records", {"after_offset": k["after_offset"], "limit": k["limit"]}, _context()))
    report = authority_doctor.diagnose(paths, None, repo_id=store.repo_id, check_permissions=False)
    assert report["checks"]["server_sequence"] == {
        "status": "pass", "reason": "server-client-committed-prefix-aligned",
        "server_next_origin_seq": 2, "compared_records": 1,
    }
    assert report["checks"]["write_probe"]["status"] == "unknown"
    assert report["authorizes_command_submission"] is False
