"""Unit tests for sprintctl.served, the SPRINTCTL_BACKEND=served invocation facade.

``vuoro_client`` is not installed in this environment (it is an optional
extra, pinned by commit SHA to an in-tree package -- see pyproject.toml).
These tests inject a fake ``vuoro_client`` module into ``sys.modules``
before calling into ``sprintctl.served``, which lazily does
``from vuoro_client import ...`` inside each coroutine; Python's import
machinery consults ``sys.modules`` first, so the fake is picked up without
needing the real package. This also means these tests exercise the exact
shape served.py sends over the wire without any network or service process.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from sprintctl import served
from sprintctl.backend import ServedProfile
from sprintctl.served_routes import routes_for
from sprintctl.vuoro_credentials import resolve_file_credential


def _profile(**overrides) -> ServedProfile:
    defaults = dict(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref="file:~/.config/vuoro/credentials/workstation",
        expected_environment="vuoro-shared",
        source_path=Path("/tmp/profile.json"),
    )
    defaults.update(overrides)
    return ServedProfile(**defaults)


class _FakeAsyncVuoroClient:
    """Records every construction and invocation; never touches the network."""

    instances: list["_FakeAsyncVuoroClient"] = []

    def __init__(self, profile, credential_resolver):
        self.profile = profile
        self.credential_resolver = credential_resolver
        self.invocations: list[tuple[str, dict, dict]] = []
        self.catalog_calls = 0
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return None

    async def invoke(self, operation_name, arguments, **kwargs):
        self.invocations.append((operation_name, dict(arguments), dict(kwargs)))
        return {"operation": operation_name, "arguments": arguments}

    async def catalog(self):
        self.catalog_calls += 1
        return {
            "operations": [
                {"name": "work.read.sprints"},
                {"name": "work.read.item"},
            ]
        }


class _FakeProfile:
    def __init__(self, name, endpoint, credential_ref, expected_environment=None):
        self.name = name
        self.endpoint = endpoint
        self.credential_ref = credential_ref
        self.expected_environment = expected_environment


@pytest.fixture
def fake_vuoro_client(monkeypatch):
    _FakeAsyncVuoroClient.instances = []
    module = types.ModuleType("vuoro_client")
    module.AsyncVuoroClient = _FakeAsyncVuoroClient
    module.Profile = _FakeProfile
    monkeypatch.setitem(sys.modules, "vuoro_client", module)
    return _FakeAsyncVuoroClient


def test_read_sprints_shapes_arguments(fake_vuoro_client):
    profile = _profile()
    result = served.read_sprints(
        profile,
        repo_id="repo-x",
        include_backlog=True,
        include_archive=False,
        active_only=False,
    )
    assert result == {
        "operation": "work.read.sprints",
        "arguments": {
            "include_backlog": True,
            "include_archive": False,
            "active_only": False,
        },
    }


def test_read_item_sends_only_item_id(fake_vuoro_client):
    profile = _profile()
    result = served.read_item(profile, repo_id="repo-x", item_id=42)
    assert result["operation"] == "work.read.item"
    assert result["arguments"] == {"item_id": 42}


def test_read_next_work_and_project_next_work_send_sprint_id(fake_vuoro_client):
    profile = _profile()
    single = served.read_next_work(profile, repo_id="repo-x", sprint_id=7)
    assert single["operation"] == "work.read.next-work"
    assert single["arguments"] == {"sprint_id": 7}

    project = served.project_next_work(profile)
    assert project["operation"] == "work.project.next-work"
    assert project["arguments"] == {"sprint_id": None}


def test_read_events_sends_sprint_id_filter_and_pagination(fake_vuoro_client):
    profile = _profile()
    result = served.read_events(
        profile,
        repo_id="repo-x",
        sprint_id=9,
        work_item_id=3,
        after_offset=1,
        limit=10,
    )
    assert result["operation"] == "work.read.events"
    assert result["arguments"] == {
        "sprint_id": 9,
        "work_item_id": 3,
        "after_offset": 1,
        "limit": 10,
    }
    client = fake_vuoro_client.instances[-1]
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs == {"repo_id": "repo-x"}


def test_read_events_defaults_omit_work_item_filter_and_pagination(fake_vuoro_client):
    profile = _profile()
    result = served.read_events(profile, repo_id="repo-x", sprint_id=9)
    assert result["arguments"] == {
        "sprint_id": 9,
        "work_item_id": None,
        "after_offset": 0,
        "limit": None,
    }


def test_event_add_and_item_create_never_send_a_client_actor(fake_vuoro_client):
    profile = _profile()
    event = served.event_add(
        profile, repo_id="repo-x", sprint_id=9, event_type="decision",
        work_item_id=3, source_type="daemon", payload={"summary": "s"},
    )
    assert event["operation"] == "work.event.add"
    assert event["arguments"] == {
        "sprint_id": 9, "event_type": "decision", "work_item_id": 3,
        "source_type": "daemon", "payload": {"summary": "s"},
    }
    created = served.item_create(
        profile, repo_id="repo-x", sprint_id=9, track_name="served", title="T",
    )
    assert created["operation"] == "work.item.create"
    assert "actor" not in created["arguments"]
    sprint = served.read_sprint(profile, repo_id="repo-x")
    assert sprint["operation"] == "work.read.sprint"
    assert sprint["arguments"] == {"sprint_id": None}


def test_claim_start_sends_full_shape_and_never_an_actor_field(fake_vuoro_client):
    profile = _profile()
    result = served.claim_start(
        profile,
        repo_id="repo-x",
        item_id=5,
        ttl_seconds=120,
        branch="feat/x",
        worktree_path=None,
        commit_sha=None,
        pr_ref=None,
        runtime_session_id="rs-1",
        instance_id="inst-1",
        hostname="host-1",
        pid=999,
    )
    assert result["operation"] == "work.claim.start"
    args = result["arguments"]
    assert set(args) == {
        "item_id",
        "ttl_seconds",
        "branch",
        "worktree_path",
        "commit_sha",
        "pr_ref",
        "runtime_session_id",
        "instance_id",
        "hostname",
        "pid",
    }
    assert "actor" not in args
    assert "agent" not in args
    assert args["item_id"] == 5
    assert args["ttl_seconds"] == 120
    assert args["branch"] == "feat/x"


def test_claim_start_defaults_ttl_and_omits_no_keys(fake_vuoro_client):
    profile = _profile()
    served.claim_start(profile, repo_id="repo-x", item_id=1)
    client = fake_vuoro_client.instances[-1]
    _operation, arguments, _kwargs = client.invocations[0]
    assert arguments["ttl_seconds"] == 300
    assert arguments["branch"] is None


def test_claim_start_never_sends_an_idempotency_key_or_retries(fake_vuoro_client):
    profile = _profile()
    served.claim_start(profile, repo_id="repo-x", item_id=1)
    client = fake_vuoro_client.instances[-1]
    assert len(client.invocations) == 1, "claim start must invoke exactly once, no retry"


def test_item_note_sends_full_shape_and_never_an_actor_field(fake_vuoro_client):
    profile = _profile()
    result = served.item_note(
        profile,
        repo_id="repo-x",
        item_id=7,
        note_type="decision",
        summary="Chose served",
        detail="Extra context",
        tags=["a", "b"],
        evidence_item_id=None,
        evidence_event_id=None,
        git_branch=None,
        git_sha=None,
        git_worktree=None,
    )
    assert result["operation"] == "work.item.note"
    args = result["arguments"]
    assert set(args) == {
        "item_id",
        "note_type",
        "summary",
        "detail",
        "tags",
        "evidence_item_id",
        "evidence_event_id",
        "git_branch",
        "git_sha",
        "git_worktree",
    }
    assert "actor" not in args
    assert args["item_id"] == 7
    assert args["note_type"] == "decision"
    assert args["tags"] == ["a", "b"]


def test_item_note_never_sends_an_idempotency_key_or_retries(fake_vuoro_client):
    profile = _profile()
    served.item_note(profile, repo_id="repo-x", item_id=1, note_type="decision", summary="s")
    client = fake_vuoro_client.instances[-1]
    assert len(client.invocations) == 1, "item note must invoke exactly once, no retry"
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs == {"repo_id": "repo-x"}
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs.get("idempotency_key") is None


def _sample_lifecycle_record(**overrides) -> dict:
    record = {
        "origin_stream_id": "11111111-1111-1111-1111-111111111111",
        "origin_seq": 1,
        "event_id": "22222222-2222-2222-2222-222222222222",
        "schema_version": 1,
        "record_class": "authority-command",
        "event_type": "item.transition",
        "actor": "worker",
        "runtime_session_id": None,
        "occurred_at": "2026-07-23T00:00:00Z",
        "basis_revision": "item:33333333-3333-3333-3333-333333333333@status:pending",
        "correlation_id": "22222222-2222-2222-2222-222222222222",
        "causation_id": None,
        "payload": {"to_status": "active"},
        "payload_sha256": "a" * 64,
        "created_at": "2026-07-23T00:00:00Z",
    }
    record.update(overrides)
    return record


def test_lifecycle_arbitrate_sends_the_record_and_matching_idempotency_and_basis(
    fake_vuoro_client,
):
    profile = _profile()
    record = _sample_lifecycle_record()
    result = served.lifecycle_arbitrate(profile, repo_id="repo-x", record=record)
    assert result["operation"] == "work.lifecycle.arbitrate"
    assert result["arguments"] == {"record": record}
    client = fake_vuoro_client.instances[-1]
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs["idempotency_key"] == record["event_id"]
    assert kwargs["basis_revision"] == record["basis_revision"]


def test_claim_context_sends_only_claim_id_with_no_credentials(fake_vuoro_client):
    profile = _profile()
    result = served.claim_context(profile, repo_id="repo-x", claim_id=9)
    assert result["operation"] == "work.claim.context"
    assert result["arguments"] == {"claim_id": 9}
    client = fake_vuoro_client.instances[-1]
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs == {"repo_id": "repo-x"}


def _sample_claim_record(**overrides) -> dict:
    record = {
        "origin_stream_id": "11111111-1111-1111-1111-111111111111",
        "origin_seq": 1,
        "event_id": "44444444-4444-4444-4444-444444444444",
        "schema_version": 1,
        "record_class": "authority-command",
        "event_type": "claim.renew",
        "actor": "worker",
        "runtime_session_id": None,
        "occurred_at": "2026-07-23T00:00:00Z",
        "basis_revision": "claim:9@sha256:" + "b" * 64,
        "correlation_id": "44444444-4444-4444-4444-444444444444",
        "causation_id": None,
        "payload": {"claim_id": 9, "ttl_seconds": 300},
        "payload_sha256": "a" * 64,
        "created_at": "2026-07-23T00:00:00Z",
    }
    record.update(overrides)
    return record


def test_claim_arbitrate_sends_the_record_and_transient_credentials(fake_vuoro_client):
    profile = _profile()
    record = _sample_claim_record()
    credentials = {"sha256:" + "c" * 64: "secret-proof"}
    result = served.claim_arbitrate(profile, repo_id="repo-x", record=record, transient_credentials=credentials)
    assert result["operation"] == "work.claim.arbitrate"
    assert result["arguments"] == {"record": record}
    client = fake_vuoro_client.instances[-1]
    _operation, _arguments, kwargs = client.invocations[0]
    assert kwargs["idempotency_key"] == record["event_id"]
    assert kwargs["basis_revision"] == record["basis_revision"]
    assert kwargs["transient_credentials"] == credentials


def test_claim_arbitrate_uses_a_fresh_client_per_call(fake_vuoro_client):
    profile = _profile()
    served.claim_arbitrate(
        profile, repo_id="repo-x", record=_sample_claim_record(), transient_credentials={}
    )
    served.claim_arbitrate(
        profile, repo_id="repo-x", record=_sample_claim_record(), transient_credentials={}
    )
    assert len(fake_vuoro_client.instances) == 2
    first, second = fake_vuoro_client.instances
    assert first is not second
    assert first.closed and second.closed


def test_lifecycle_arbitrate_uses_a_fresh_client_per_call(fake_vuoro_client):
    profile = _profile()
    served.lifecycle_arbitrate(profile, repo_id="repo-x", record=_sample_lifecycle_record())
    served.lifecycle_arbitrate(profile, repo_id="repo-x", record=_sample_lifecycle_record())
    assert len(fake_vuoro_client.instances) == 2
    first, second = fake_vuoro_client.instances
    assert first is not second
    assert first.closed and second.closed


def test_credential_resolver_passed_to_client_is_resolve_file_credential(fake_vuoro_client):
    profile = _profile()
    served.read_item(profile, repo_id="repo-x", item_id=1)
    client = fake_vuoro_client.instances[-1]
    assert client.credential_resolver is resolve_file_credential


@pytest.mark.parametrize(
    "call",
    [
        lambda profile: served.read_sprints(profile, repo_id="repo-x"),
        lambda profile: served.read_item(profile, repo_id="repo-x", item_id=1),
        lambda profile: served.read_next_work(profile, repo_id="repo-x"),
        lambda profile: served.project_next_work(profile),
        lambda profile: served.claim_start(profile, repo_id="repo-x", item_id=1),
        lambda profile: served.read_events(profile, repo_id="repo-x", sprint_id=1),
        lambda profile: served.read_sprint(profile, repo_id="repo-x"),
        lambda profile: served.event_add(profile, repo_id="repo-x", sprint_id=1, event_type="update"),
        lambda profile: served.item_create(profile, repo_id="repo-x", sprint_id=1, track_name="t", title="T"),
    ],
)
def test_each_operation_constructs_a_fresh_client_per_call(fake_vuoro_client, call):
    profile = _profile()
    call(profile)
    call(profile)
    assert len(fake_vuoro_client.instances) == 2
    first, second = fake_vuoro_client.instances
    assert first is not second
    assert first.closed and second.closed


def test_client_profile_carries_the_served_profile_fields(fake_vuoro_client):
    profile = _profile(
        name="n", endpoint="https://e/", credential_ref="file:/x", expected_environment="env"
    )
    served.read_item(profile, repo_id="repo-x", item_id=1)
    client = fake_vuoro_client.instances[-1]
    assert client.profile.name == "n"
    assert client.profile.endpoint == "https://e/"
    assert client.profile.credential_ref == "file:/x"
    assert client.profile.expected_environment == "env"


def test_catalog_operation_names_uses_a_fresh_client_and_returns_names(fake_vuoro_client):
    profile = _profile()
    names = served.catalog_operation_names(profile)
    assert names == {"work.read.sprints", "work.read.item"}
    client = fake_vuoro_client.instances[-1]
    assert client.catalog_calls == 1
    assert client.invocations == [], "catalog discovery must not invoke an operation"


def test_expected_operations_matches_all_served_cli_command_paths():
    expected = {
        route.operation
        for path in (
            "sprint.list",
            "item.show",
            "next-work",
            "claim.start",
            "item.status",
            "sprint.status",
            "claim.heartbeat",
            "claim.handoff",
            "claim.release",
            "item.note",
            "pilot.cutover-evidence",
            "authority.sync",
            "event.list",
            "event.add",
            "item.add",
            "sprint.show",
        )
        for route in routes_for(path)
    }
    assert served.EXPECTED_OPERATIONS == expected
    assert len(served.EXPECTED_OPERATIONS) == 14
    assert served.EXPECTED_OPERATIONS == {
        "work.read.sprints",
        "work.read.item",
        "work.read.next-work",
        "work.project.next-work",
        "work.claim.start",
        "work.lifecycle.arbitrate",
        "work.claim.arbitrate",
        "work.item.note",
        "work.pilot.cutover-evidence",
        "work.batch.apply",
        "work.read.events",
        "work.event.add",
        "work.item.create",
        "work.read.sprint",
    }


def test_expected_operations_would_fail_if_event_list_route_were_omitted_from_probe():
    """sprintctl#1247: the doctor probe's command-path allowlist must be
    updated in the same change that adds a new served route -- the #1195
    postmortem found this list silently drifted out of sync twice before
    (missing `claim.handoff`, then `pilot.cutover-evidence`). This test
    fails if `event.list` (and therefore `work.read.events`) is missing from
    `_DOCTOR_PROBE_COMMAND_PATHS`/`EXPECTED_OPERATIONS`, independent of the
    exact-set assertion above."""

    assert "event.list" in served._DOCTOR_PROBE_COMMAND_PATHS
    assert "work.read.events" in served.EXPECTED_OPERATIONS


def test_served_module_imports_without_vuoro_client_installed():
    """served.py must be importable without the 'served' extra: only calling
    a facade function should need vuoro_client. This test runs in a fresh
    subprocess with an unmodified sys.modules to prove it, independent of
    whatever the fake-client fixture above may have injected."""
    result = subprocess.run(
        [sys.executable, "-c", "import sprintctl.served; print('OK')"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert "vuoro_client" not in result.stderr


def test_served_and_its_optional_dependencies_never_import_postgres_modules():
    script = (
        "import sys\n"
        "import sprintctl.served\n"
        "import sprintctl.backend\n"
        "import sprintctl.vuoro_credentials\n"
        "import sprintctl.served_routes\n"
        "assert 'psycopg' not in sys.modules, sorted(sys.modules)\n"
        "assert 'sprintctl.pg' not in sys.modules, sorted(sys.modules)\n"
        "assert 'sprintctl.pg_migrations' not in sys.modules, sorted(sys.modules)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
