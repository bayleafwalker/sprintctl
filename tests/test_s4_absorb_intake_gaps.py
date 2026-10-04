"""Example-tested S4 missing gate; never an operational absorption receipt."""

import json
import subprocess
import sys

import pytest
from tests.test_served_authority_sync import (
    _append_observation,
    _configure_served_repo,
    _item_refs,
    _mint_command,
    _open_producer,
)

import sprintctl.cli as cli_module
from sprintctl import outbox, release_trailers
from sprintctl.cli import cli

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="served orchestration requires Python3.12+"
)

S4_OPERATIONS = (
    "work.reservation.reserve",
    "work.evidence.append-v1",
    "work.effect.propose-v1",
)


class FakeAuthority:
    """Only receipt plumbing is simulated; no domain acceptance is implemented."""

    def __init__(self):
        self.online = False
        self.observations = {}
        self.lost_reply = False
        self.batch_keys = []

    def identity(self, *args, **kwargs):
        if not self.online:
            raise ConnectionError("isolated fixture authority stopped")
        return {"actor": "fixture-producer"}

    def apply(self, profile, *, repo_id, records, idempotency_key):
        assert self.online
        self.batch_keys.append(idempotency_key)
        results = []
        for record in records:
            assert record["record_class"] == outbox.OBSERVATION
            event_id = record["event_id"]
            duplicate = event_id in self.observations
            self.observations[event_id] = record
            results.append(
                {
                    "kind": "record",
                    "event_id": event_id,
                    "event_type": record["event_type"],
                    "ingest_offset": len(self.observations),
                    "duplicate": duplicate,
                }
            )
        if self.lost_reply:
            self.lost_reply = False
            raise ConnectionError("fixture commit completed, reply lost")
        return {"repo_id": repo_id, "results": results}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_offline_reservation_is_not_a_durable_pending_request(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    authority = FakeAuthority()
    monkeypatch.setattr(cli_module._served, "identity_current", authority.identity)

    def forbidden(*args, **kwargs):
        pytest.fail("offline served request attempted local authority or mutation")

    monkeypatch.setattr(cli_module._served, "reservation_operation", forbidden)
    monkeypatch.setattr(cli_module._db, "reserve", forbidden)
    result = runner.invoke(cli, ["reservation", "reserve", "--item-id", "7", "--json"])
    assert result.exit_code != 0
    producer = _open_producer(tmp_path)
    try:
        assert outbox.list_records(producer) == []
    finally:
        producer.close()


@pytest.mark.parametrize("operation", S4_OPERATIONS)
def test_s4_native_operations_cannot_be_relabelled_as_outbox_commands(
    tmp_path, monkeypatch, operation
):
    _configure_served_repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="not classified"):
        _mint_command(
            tmp_path,
            record_type=operation,
            refs=_item_refs(7),
            payload={"fixture": True},
            actor="fixture-producer",
        )
    producer = _open_producer(tmp_path)
    try:
        assert outbox.list_records(producer) == []
    finally:
        producer.close()


@pytest.mark.parametrize("lose_reply", [False, True])
def test_restart_syncs_observations_but_cannot_complete_s4_gate(
    runner, tmp_path, monkeypatch, lose_reply
):
    _configure_served_repo(tmp_path, monkeypatch)
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "S4 fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    authority = FakeAuthority()
    monkeypatch.setattr(cli_module._served, "identity_current", authority.identity)
    reserve = runner.invoke(cli, ["reservation", "reserve", "--item-id", "7", "--json"])
    assert reserve.exit_code != 0
    digest = "a" * 64
    git(
        tmp_path,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "synthetic fixture only\n\nVuoro-Release: " + digest,
    )
    for operation in S4_OPERATIONS:
        with pytest.raises(ValueError, match="not classified"):
            _mint_command(
                tmp_path,
                record_type=operation,
                refs=_item_refs(7),
                payload={"fixture": True},
                actor="fixture-producer",
            )
    monkeypatch.setattr(
        cli_module._served,
        "batch_record_types",
        lambda *args: [release_trailers.EVENT_TYPE],
    )
    monkeypatch.setattr(cli_module._served, "batch_apply", authority.apply)
    # This is an ordinary local observation, NOT run-bound evidence or a proposal.
    observation = _append_observation(
        tmp_path,
        actor="fixture-producer",
        payload={"text": "harmless offline observation"},
    )
    authority.online = True
    authority.lost_reply = lose_reply
    first = runner.invoke(cli, ["authority", "sync", "--json"])
    assert first.exit_code == (1 if lose_reply else 0), first.output
    replay = runner.invoke(cli, ["authority", "sync", "--json"])
    assert replay.exit_code == 0, replay.output
    payload = json.loads(replay.output)
    assert payload["decisions"] == []
    assert payload["unsupported_command_event_ids"] == []
    assert payload["pending_command_event_ids"] == []
    assert len(authority.observations) == 2
    assert observation.event_id in authority.observations
    trailer = next(
        r
        for r in authority.observations.values()
        if r["event_type"] == release_trailers.EVENT_TYPE
    )
    assert digest in json.dumps(trailer)
    assert authority.batch_keys[0] == authority.batch_keys[1]
    # A green sync with no pending commands is NOT a green S4 gate: none of
    # its three required operations has a durable request or owner receipt.
    producer = _open_producer(tmp_path)
    try:
        requests = outbox.list_records(producer)
        assert all(r.record_class == outbox.OBSERVATION for r in requests)
        assert not any(r.event_type in S4_OPERATIONS for r in requests)
    finally:
        producer.close()
