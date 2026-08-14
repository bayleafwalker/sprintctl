"""Tests for the served-mode item/sprint status routes (#1195 Step 2, Group B)
and the shared authority-command record-construction helper (#1195 Step 1).

``vuoro_client`` is not installed in this environment (see the module
docstring in ``tests/test_served.py``), so these CLI-level tests monkeypatch
``sprintctl.cli._served``'s facade functions directly rather than faking the
transport layer -- the same pattern ``tests/test_authority_cli.py`` uses for
``cli_module._authority.arbitrate_command``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import sprintctl.cli as cli_module
from sprintctl import db, outbox
from sprintctl.cli import cli

_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason=(
        "served mode requires Python 3.12+; this test exercises behavior only "
        "reachable past that version gate"
    ),
)


def _configure_served_repo(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    (tmp_path / "sprintctl.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": str(uuid4())}),
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "vuoro-client-profile/v1",
                "id": "workstation-vuoro-shared",
                "target": {
                    "environment_id": "vuoro-shared",
                    "environment_class": "production",
                    "endpoint": "https://vuoro-shared.example/",
                },
                "credential_ref": "file:~/.config/vuoro/credentials/workstation",
                "production_endpoint_denied": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile_path))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)
    monkeypatch.setattr(
        cli_module._served,
        "identity_current",
        lambda profile, *, repo_id=None: {
            "repo_id": repo_id,
            "actor": "served-actor",
        },
    )


def _manifest_repo_uuid(tmp_path) -> str:
    return json.loads((tmp_path / "sprintctl.dispatch.json").read_text(encoding="utf-8"))["repo_id"]


def _outbox_records(tmp_path):
    producer = outbox.open_outbox(tmp_path / ".sprintctl" / "authority-command-outbox.db")
    try:
        return outbox.list_records(producer)
    finally:
        producer.close()


@_requires_312
@pytest.mark.parametrize(
    "argv",
    [
        ["session", "resume"],
    ],
)
def test_unavailable_served_p0_commands_fail_closed_before_opening_store(
    runner, tmp_path, monkeypatch, argv
):
    """Unimplemented catalog routes must not fall into the PG/store path."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store")
    )

    result = runner.invoke(cli, argv)

    assert result.exit_code == 1, result.output
    assert "served-operation-unavailable" in result.output
    assert "PostgreSQL" not in result.output


def _served_claim_effect(*, claim_id=19, claim_type="coordinate"):
    return {
        "claim_id": claim_id,
        "work_item_id": 3,
        "actor": "served-actor",
        "claim_type": claim_type,
        "exclusive": True,
        "heartbeat": "2026-08-02T00:00:00Z",
        "expires_at": "2026-08-02T00:30:00Z",
        "status": "active",
        "lease_epoch": 1,
        "runtime_session_id": "session-1",
        "instance_id": "instance-1",
    }


def _deployed_served_claim_effect(*, claim_id=19, claim_type="coordinate"):
    """Public claim-row shape returned by deployed adapter revisions."""
    effect = _served_claim_effect(claim_id=claim_id, claim_type=claim_type)
    effect["id"] = effect.pop("claim_id")
    effect["agent"] = effect.pop("actor")
    return effect


@_requires_312
def test_served_claim_create_uses_authenticated_identity_and_existing_arbitration(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda *a, **k: {
            "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "pending"},
            "refs": [],
        },
    )
    captured = {}
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: captured.update(k) or {
            "outcome": "accepted", "effect": _served_claim_effect()
        },
    )

    result = runner.invoke(cli, [
        "claim", "create", "--item-id", "3", "--actor", "impersonator",
        "--type", "coordinate", "--ttl", "1800", "--runtime-session-id", "session-1",
        "--instance-id", "instance-1", "--json",
    ])

    assert result.exit_code == 0, result.output
    assert "authenticated identity (served-actor)" in result.output
    record = captured["record"]
    command = record["payload"]
    assert record["event_type"] == "claim.acquire"
    assert command["actor"] == "served-actor"
    assert command["payload"]["agent"] == "served-actor"
    assert command["payload"]["claim_type"] == "coordinate"
    assert command["payload"]["exclusive"] is True
    proposed_ref = command["payload"]["credential_ref"]
    assert set(captured["transient_credentials"]) == {proposed_ref}
    sidecar = tmp_path / "claim-recovery" / "claim-19.json"
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert json.loads(sidecar.read_text())["claim_token"] == captured["transient_credentials"][proposed_ref]
    assert [(r.record_class, r.event_type) for r in _outbox_records(tmp_path)] == [
        ("authority-command", "claim.acquire")
    ]


@_requires_312
def test_served_claim_create_passes_coordinate_proof_only_transiently(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {
        "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "active"}, "refs": [],
    })
    captured = {}
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: captured.update(k) or {
            "outcome": "accepted", "effect": _served_claim_effect(claim_type="review")
        },
    )
    result = runner.invoke(cli, [
        "claim", "create", "--item-id", "3", "--actor", "served-actor",
        "--type", "review", "--coordinate-claim-id", "7",
        "--coordinate-claim-token", "coordinate-secret", "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = captured["record"]["payload"]["payload"]
    assert payload["coordinate_claim_id"] == 7
    assert "coordinate-secret" not in json.dumps(captured["record"])
    assert captured["transient_credentials"][payload["coordinate_credential_ref"]] == "coordinate-secret"


@_requires_312
@pytest.mark.parametrize("nested", [False, True])
def test_served_claim_create_recovers_deployed_accepted_effect_shape(
    runner, tmp_path, monkeypatch, nested
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {
        "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "pending"},
        "refs": [],
    })
    effect = _deployed_served_claim_effect(claim_type="execute")
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: {
            "outcome": "accepted",
            "effect": {"claim": effect} if nested else effect,
        },
    )

    result = runner.invoke(cli, [
        "claim", "create", "--item-id", "3", "--actor", "served-actor",
        "--type", "execute", "--json",
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claim_id"] == 19
    assert payload["actor"] == "served-actor"
    sidecar = tmp_path / "claim-recovery" / "claim-19.json"
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert json.loads(sidecar.read_text())["claim_token"] == payload["claim_token"]


@pytest.mark.parametrize("nested", [False, True])
def test_served_claim_recovery_projection_accepts_matching_dual_aliases(nested):
    claim = _served_claim_effect(claim_type="execute")
    claim.update({"id": claim["claim_id"], "agent": claim["actor"]})
    effect = {"claim": claim} if nested else claim

    projection = cli_module._served_claim_recovery_projection(
        effect, item_id=3, actor="served-actor", claim_type="execute",
        claim_token="private-proof",
    )

    assert projection is not None
    assert projection["claim_id"] == projection["id"] == 19
    assert projection["actor"] == projection["agent"] == "served-actor"
    assert projection["claim_token"] == "private-proof"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 20},
        {"id": "19"},
        {"agent": "other-actor"},
        {"agent": 7},
    ],
)
def test_served_claim_recovery_projection_rejects_conflicting_or_malformed_aliases(changes):
    effect = _served_claim_effect(claim_type="execute")
    effect.update(changes)

    projection = cli_module._served_claim_recovery_projection(
        effect, item_id=3, actor="served-actor", claim_type="execute",
        claim_token="private-proof",
    )

    assert projection is None


@_requires_312
def test_served_claim_create_replays_one_request_after_unknown_outcome(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {
        "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "pending"}, "refs": [],
    })
    calls = []
    def lost(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("response lost after commit")
    monkeypatch.setattr(cli_module._served, "claim_arbitrate", lost)
    argv = ["claim", "create", "--item-id", "3", "--actor", "served-actor", "--json"]
    first = runner.invoke(cli, argv)
    assert first.exit_code == 1
    event_id = calls[0]["record"]["event_id"]
    assert len(_outbox_records(tmp_path)) == 1
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: calls.append(k) or {
            "outcome": "accepted", "duplicate": True,
            "effect": _served_claim_effect(claim_type="execute"),
        },
    )
    retry = runner.invoke(cli, argv)
    assert retry.exit_code == 0, retry.output
    assert calls[1]["record"]["event_id"] == event_id
    assert calls[1]["transient_credentials"] == calls[0]["transient_credentials"]
    assert len(_outbox_records(tmp_path)) == 1


@_requires_312
def test_served_claim_create_keeps_accepted_request_replayable_until_recovery_is_durable(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {
        "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "pending"}, "refs": [],
    })
    calls = []
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: calls.append(k) or {
            "outcome": "accepted", "duplicate": len(calls) > 1,
            "effect": {"claim": _deployed_served_claim_effect(claim_type="coordinate")},
        },
    )
    writes = []
    real_writer = cli_module._write_claim_recovery_record
    def fail_once(claim):
        writes.append(claim)
        return None if len(writes) == 1 else real_writer(claim)
    monkeypatch.setattr(cli_module, "_write_claim_recovery_record", fail_once)
    argv = [
        "claim", "create", "--item-id", "3", "--actor", "served-actor",
        "--type", "coordinate", "--json",
    ]

    first = runner.invoke(cli, argv)
    assert first.exit_code == 1
    assert "accepted but its local recovery proof could not be persisted" in first.output
    assert "claim_token" not in first.output
    records = _outbox_records(tmp_path)
    assert len(records) == 1
    event_id = records[0].event_id
    credentials = calls[0]["transient_credentials"]
    assert list((tmp_path / ".sprintctl" / "authority-credentials").glob("*"))
    terminal = tmp_path / ".sprintctl" / "authority-terminal-decisions" / f"{event_id}.json"
    assert not terminal.exists()

    retry = runner.invoke(cli, argv)
    assert retry.exit_code == 0, retry.output
    assert len(calls) == 2
    assert calls[1]["record"]["event_id"] == event_id
    assert calls[1]["transient_credentials"] == credentials
    assert len(_outbox_records(tmp_path)) == 1
    assert not list((tmp_path / ".sprintctl" / "authority-credentials").glob("*"))
    assert terminal.exists()
    sidecar = tmp_path / "claim-recovery" / "claim-19.json"
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert json.loads(sidecar.read_text())["claim_token"] == next(
        token for ref, token in credentials.items()
        if ref == calls[1]["record"]["payload"]["payload"]["credential_ref"]
    )


@_requires_312
@pytest.mark.parametrize(
    "mismatch",
    [
        "item", "actor", "type", "ambiguous", "malformed",
        "conflicting-id-alias", "malformed-id-alias",
        "conflicting-actor-alias", "malformed-actor-alias",
    ],
)
def test_served_claim_create_replay_fails_closed_for_invalid_accepted_effect(
    runner, tmp_path, monkeypatch, mismatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    aggregate_uuid = str(uuid4())
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {
        "item": {"id": 3, "aggregate_uuid": aggregate_uuid, "status": "pending"},
        "refs": [],
    })
    deployed = _deployed_served_claim_effect(claim_type="execute")
    if mismatch == "item":
        deployed["work_item_id"] = 4
    elif mismatch == "actor":
        deployed["agent"] = "other-actor"
    elif mismatch == "type":
        deployed["claim_type"] = "review"
    elif mismatch == "malformed":
        deployed["id"] = "19"
    elif mismatch == "conflicting-id-alias":
        deployed["claim_id"] = 20
    elif mismatch == "malformed-id-alias":
        deployed["claim_id"] = "19"
    elif mismatch == "conflicting-actor-alias":
        deployed["actor"] = "other-actor"
    elif mismatch == "malformed-actor-alias":
        deployed["actor"] = 7
    effect = {"claim": deployed}
    if mismatch == "ambiguous":
        effect.update(_served_claim_effect(claim_id=20, claim_type="execute"))
    calls = []
    monkeypatch.setattr(
        cli_module._served, "claim_arbitrate",
        lambda *a, **k: calls.append(k) or {
            "outcome": "accepted", "duplicate": len(calls) > 1, "effect": effect,
        },
    )
    argv = [
        "claim", "create", "--item-id", "3", "--actor", "served-actor",
        "--type", "execute", "--json",
    ]

    first = runner.invoke(cli, argv)
    retry = runner.invoke(cli, argv)

    assert first.exit_code == retry.exit_code == 1
    assert "claim_token" not in first.output + retry.output
    assert len(calls) == 2
    assert calls[0]["record"]["event_id"] == calls[1]["record"]["event_id"]
    assert calls[0]["transient_credentials"] == calls[1]["transient_credentials"]
    assert len(_outbox_records(tmp_path)) == 1
    event_id = calls[0]["record"]["event_id"]
    terminal = tmp_path / ".sprintctl" / "authority-terminal-decisions" / f"{event_id}.json"
    assert not terminal.exists()
    assert list((tmp_path / ".sprintctl" / "authority-credentials").glob("*"))
    assert not list((tmp_path / "claim-recovery").glob("claim-*.json"))

    assert len(_outbox_records(tmp_path)) == 2


@_requires_312
def test_served_usage_context_uses_atomic_aggregate_without_opening_store(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store")
    )
    snapshot = {
        "contract_version": "1",
        "sprint": {"id": 3, "name": "served", "goal": "goal", "status": "active", "start_date": None, "end_date": None},
        "summary": {"total": 0, "done": 0, "active": 0, "pending": 0, "blocked": 0, "stale": 0, "ready": 0, "waiting_on_dependencies": 0, "active_claims": 0, "active_unclaimed": 0},
        "active_claims": [], "active_unclaimed_items": [], "conflicts": [],
        "ready_items": [], "blocked_items": [], "stale_items": [],
        "recent_decisions": [],
        "next_action": {"kind": "no-action", "summary": "Nothing", "reason": "Nothing"},
    }
    monkeypatch.setattr(cli_module._served, "read_context", lambda *args, **kwargs: snapshot)

    result = runner.invoke(cli, ["usage", "--context", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == snapshot


@_requires_312
def test_served_context_candidates_uses_catalog_without_opening_store(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    packet = {
        "contract_version": "1",
        "explicit_target": {"item_id": 3, "found": True},
        "bound": 5,
        "truncated": False,
        "watermark": None,
        "candidates": [{"item_id": 3, "rank": 1, "claim_eligible": True, "title": "Target"}],
        "sprint": {"id": 7, "name": "served"},
        "projection": {"enabled": False, "source": "backend", "fallback_reason": "served-authority", "watermark_offset": None, "watermark_age_seconds": None, "schema_version": None},
    }
    captured = {}
    def context_candidates(*args, **kwargs):
        captured.update(kwargs)
        return packet
    monkeypatch.setattr(cli_module._served, "context_candidates", context_candidates)

    result = runner.invoke(cli, ["context-candidates", "--sprint-id", "7", "--item-id", "3", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == packet
    assert captured["repo_id"] == tmp_path.name
    assert captured["item_id"] == 3


@_requires_312
def test_served_next_work_explain_uses_atomic_aggregate_without_opening_store(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    snapshot = {
        "contract_version": "1", "sprint": {"id": 3, "name": "served", "status": "active"},
        "summary": {"pending_total": 0, "ready": 0, "waiting_on_dependencies": 0, "active_claims": 0, "active_unclaimed": 0},
        "ready_items": [], "dependency_waiting_items": [], "active_claims": [], "active_unclaimed_items": [], "conflicts": [],
        "next_action": {"kind": "no-action", "summary": "Nothing", "reason": "Nothing"},
        "recommended_commands": [], "recommended_command_bundle": {"bundle_version": "1", "next_action_kind": "no-action", "steps": []},
    }
    monkeypatch.setattr(cli_module._served, "read_next_work_explain", lambda *args, **kwargs: snapshot)

    result = runner.invoke(cli, ["next-work", "--json", "--explain"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == snapshot


@_requires_312
def test_served_project_next_work_explain_retains_unavailable_guard(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))

    result = runner.invoke(cli, ["next-work", "--project", "--explain"])

    assert result.exit_code == 1, result.output
    assert "served-operation-unavailable" in result.output
    assert "PostgreSQL" not in result.output


@_requires_312
def test_served_project_aggregates_do_not_open_store_and_keep_sprint_list_json_array(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    monkeypatch.setattr(
        cli_module._project,
        "load_project",
        lambda *_args, **_kwargs: pytest.fail("served command read client project.toml"),
    )
    project = {
        "project_id": "workspace",
        "display_name": "Workspace",
        "home_repo": "agentops",
        "backlog_repos": ["agentops", "sprintctl"],
    }
    monkeypatch.setattr(
        cli_module._served,
        "project_sprints",
        lambda profile, **kwargs: {
            "contract_version": "project-1",
            "project": project,
            "sprints": [{"id": 8, "name": "Member", "status": "active", "kind": "active_sprint", "origin_repo": "agentops"}],
            "repositories": [
                {"origin_repo": "agentops", "status": "ok", "sprints": []},
                {"origin_repo": "sprintctl", "status": "unavailable", "reason_code": "sprint-not-found", "message": "No sprint."},
            ],
        },
    )
    result = runner.invoke(cli, ["sprint", "list", "--project", "ignored.toml", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"id": 8, "name": "Member", "status": "active", "kind": "active_sprint", "origin_repo": "agentops"}
    ]

    monkeypatch.setattr(
        cli_module._served,
        "project_context",
        lambda profile, **kwargs: {
            "contract_version": "project-1", "project": project,
            "summary": {}, "sprints": [], "active_claims": [], "active_unclaimed_items": [],
            "conflicts": [], "ready_items": [], "blocked_items": [], "stale_items": [],
            "recent_decisions": [], "next_actions": [], "repositories": [],
        },
    )
    context = runner.invoke(cli, ["usage", "--context", "--project", "ignored.toml", "--json"])
    assert context.exit_code == 0, context.output
    assert json.loads(context.output)["project"] == project


# ---------------------------------------------------------------------------
# claim recover (served-mode sidecar recovery with identity match)
# ---------------------------------------------------------------------------


def _write_served_sidecar(tmp_path, filename_claim_id, **overrides) -> Path:
    """Write a claim recovery sidecar file under ``tmp_path`` and return its path."""
    recovery_dir = tmp_path / ".sprintctl" / "claim-recovery"
    recovery_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    recovery_dir.chmod(0o700)
    sidecar = {
        "claim_id": filename_claim_id,
        "work_item_id": 5,
        "actor": "served-actor",
        "claim_type": "execute",
        "claim_token": "recovered-secret",
        "runtime_session_id": None,
        "instance_id": None,
        "written_at": "2026-07-27T00:00:00Z",
    }
    sidecar.update(overrides)
    path = recovery_dir / f"claim-{filename_claim_id}.json"
    path.write_text(json.dumps(sidecar) + "\n")
    path.chmod(0o600)
    return path


def _patch_served_sidecar_db_path(monkeypatch, tmp_path):
    """Point _db.get_db_path so sidecar resolution uses tmp_path."""
    monkeypatch.setattr(
        cli_module._db, "get_db_path",
        lambda: tmp_path / ".sprintctl" / "sprintctl.db",
    )


@_requires_312
def test_served_claim_recover_reads_served_claim_and_local_sidecar_without_store(
    runner, tmp_path, monkeypatch
):
    """Successful recovery by --id: never opens a store, returns token on identity match."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store")
    )
    monkeypatch.setattr(
        cli_module, "_get_conn", lambda _obj: pytest.fail("served command opened SQLite")
    )
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claim_token"] == "recovered-secret"
    assert payload["claim"]["claim_id"] == 3


@_requires_312
def test_served_claim_recover_by_item_id_returns_token(
    runner, tmp_path, monkeypatch
):
    """Successful recovery by --item-id: resolves single active claim."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store")
    )
    monkeypatch.setattr(
        cli_module._served, "read_claims",
        lambda profile, *, repo_id=None, item_id, **kw: {
            "claims": [
                {
                    "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                    "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
                },
            ],
        },
    )
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claim_token"] == "recovered-secret"
    assert payload["claim"]["claim_id"] == 3


@_requires_312
def test_served_claim_recover_rejects_inactive_claim_by_id(
    runner, tmp_path, monkeypatch
):
    """--id rejects a claim whose status is not 'active'."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 4, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "done",
            },
        },
    )
    _write_served_sidecar(tmp_path, 4)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "4", "--json"])

    assert result.exit_code == 1, result.output
    assert "not active" in result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_rejects_inactive_claim_by_item_id(
    runner, tmp_path, monkeypatch
):
    """--item-id independently verifies a catalog response is still active."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claims",
        lambda profile, *, repo_id=None, item_id, **kw: {
            "claims": [{
                "claim_id": 4, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "done",
            }],
        },
    )
    _write_served_sidecar(tmp_path, 4)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])

    assert result.exit_code == 1, result.output
    assert "not active" in result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_rejects_misrouted_claim_response(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claim", lambda *a, **kw: {"claim": {
        "claim_id": 4, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": "2099-01-01T00:00:00Z",
    }})
    _write_served_sidecar(tmp_path, 4)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)
    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])
    assert result.exit_code == 1, result.output
    assert "does not match requested claim" in result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_rejects_misrouted_item_response(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claims", lambda *a, **kw: {"claims": [{
        "claim_id": 3, "work_item_id": 6, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": "2099-01-01T00:00:00Z",
    }]})
    _write_served_sidecar(tmp_path, 3, work_item_id=6)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)
    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])
    assert result.exit_code == 1, result.output
    assert "does not match requested item" in result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_by_id_allows_expired_claim_for_cleanup(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claim", lambda *a, **kw: {"claim": {
        "claim_id": 3, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": "2000-01-01T00:00:00Z",
    }})
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["claim_token"] == "recovered-secret"


@pytest.mark.parametrize("expires_at", ["not-a-time", "2099-01-01T00:00:00"])
@_requires_312
def test_served_claim_recover_by_id_rejects_invalid_expiry(runner, tmp_path, monkeypatch, expires_at):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claim", lambda *a, **kw: {"claim": {
        "claim_id": 3, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": expires_at,
    }})
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)
    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])
    assert result.exit_code == 1, result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_by_item_id_rejects_expired_claim(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claims", lambda *a, **kw: {"claims": [{
        "claim_id": 3, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": "2000-01-01T00:00:00Z",
    }]})
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])

    assert result.exit_code == 1, result.output
    assert "is expired" in result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_served_claim_recover_rejects_broad_or_symlinked_sidecar(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module._served, "read_claim", lambda *a, **kw: {"claim": {
        "claim_id": 3, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "status": "active", "expires_at": "2099-01-01T00:00:00Z",
    }})
    sidecar = _write_served_sidecar(tmp_path, 3)
    sidecar.chmod(0o644)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)
    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])
    assert result.exit_code == 1, result.output
    assert "recovered-secret" not in result.output
    sidecar.chmod(0o600)
    target = tmp_path / "outside.json"
    target.write_text('{"claim_token":"recovered-secret"}')
    sidecar.unlink(); sidecar.symlink_to(target)
    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])
    assert result.exit_code == 1, result.output
    assert "recovered-secret" not in result.output


@_requires_312
def test_claim_recovery_writer_uses_private_directory_and_file_modes(tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)
    path = cli_module._write_claim_recovery_record({
        "claim_id": 3, "work_item_id": 5, "actor": "served-actor", "claim_type": "execute",
        "claim_token": "recovered-secret",
    })
    assert path is not None
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600


@_requires_312
def test_served_claim_recover_rejects_missing_sidecar(
    runner, tmp_path, monkeypatch
):
    """--id rejects when no sidecar file exists."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "No local recovery token file" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_rejects_empty_token_sidecar(
    runner, tmp_path, monkeypatch
):
    """--id rejects a sidecar with an empty claim_token."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3, claim_token="")
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "malformed" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_rejects_claim_id_mismatch(
    runner, tmp_path, monkeypatch
):
    """Sidecar claim_id differs from the served active claim."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3, claim_id=99)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "Identity mismatch" in result.output
    assert "claim_id" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_rejects_work_item_id_mismatch(
    runner, tmp_path, monkeypatch
):
    """Sidecar work_item_id differs from the served active claim."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3, work_item_id=99)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "Identity mismatch" in result.output
    assert "work_item_id" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_rejects_actor_mismatch(
    runner, tmp_path, monkeypatch
):
    """Sidecar actor differs from the served active claim."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3, actor="different-actor")
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "Identity mismatch" in result.output
    assert "actor" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_rejects_claim_type_mismatch(
    runner, tmp_path, monkeypatch
):
    """Sidecar claim_type differs from the served active claim."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3, claim_type="observe")
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3", "--json"])

    assert result.exit_code == 1, result.output
    assert "Identity mismatch" in result.output
    assert "claim_type" in result.output
    payload = json.loads(result.output)
    assert payload.get("claim_token") is None


@_requires_312
def test_served_claim_recover_text_output_reports_context(
    runner, tmp_path, monkeypatch
):
    """Text output includes claim details and resolved context."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store")
    )
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {
            "claim": {
                "claim_id": 3, "work_item_id": 5, "actor": "served-actor",
                "claim_type": "execute", "status": "active", "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    _write_served_sidecar(tmp_path, 3)
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "3"])

    assert result.exit_code == 0, result.output
    assert "Claim #3 recovered for item #5 (execute)" in result.output
    assert "Claim token: recovered-secret" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_claim_recover_rejects_multiple_active_claims(
    runner, tmp_path, monkeypatch
):
    """--item-id rejects when multiple active claims exist."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claims",
        lambda profile, *, repo_id=None, item_id, **kw: {
            "claims": [
                {"claim_id": 3, "work_item_id": 5, "actor": "a", "claim_type": "execute", "status": "active"},
                {"claim_id": 4, "work_item_id": 5, "actor": "b", "claim_type": "execute", "status": "active"},
            ],
        },
    )
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])

    assert result.exit_code == 1, result.output
    assert "Multiple active claims" in result.output
    assert "--id" in result.output


@_requires_312
def test_served_claim_recover_rejects_no_active_claims(
    runner, tmp_path, monkeypatch
):
    """--item-id rejects when no active claims exist."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claims",
        lambda profile, *, repo_id=None, item_id, **kw: {"claims": []},
    )
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--item-id", "5", "--json"])

    assert result.exit_code == 1, result.output
    assert "No active claims found" in result.output


@_requires_312
def test_served_claim_recover_rejects_claim_not_found(
    runner, tmp_path, monkeypatch
):
    """--id rejects a claim ID that does not exist."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_claim",
        lambda profile, *, repo_id=None, claim_id: {"claim": {}},
    )
    _patch_served_sidecar_db_path(monkeypatch, tmp_path)

    result = runner.invoke(cli, ["claim", "recover", "--id", "42", "--json"])

    assert result.exit_code == 1, result.output
    assert "not found" in result.output


@_requires_312
def test_served_handoff_fetches_writes_then_records_without_opening_store(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    bundle = {
        "bundle_type": "handoff", "bundle_version": "1", "generated_at": "now",
        "generated_from": {"events_limit": 50},
        "sprint": {"id": 3, "name": "served", "status": "active", "goal": "goal"},
        "summary": {"total": 0, "done": 0, "active": 0, "pending": 0, "blocked": 0},
        "active_claims": [], "conflicts": [],
        "work": {"active_items": [], "ready_items": [], "blocked_items": [], "stale_items": []},
        "recent_decisions": [], "recent_events": [], "next_action": {"kind": "no-action", "summary": "Nothing"},
        "agent_shutdown_protocol": {"required_before_termination": []}, "resume_instructions": [],
    }
    calls = []
    monkeypatch.setattr(cli_module._served, "read_handoff", lambda *a, **k: calls.append(("read", k)) or bundle)
    monkeypatch.setattr(cli_module._served, "handoff_record", lambda *a, **k: calls.append(("record", k)) or {"event_id": 8, "actor": "served-agent"})

    result = runner.invoke(cli, ["handoff", "--sprint-id", "3", "--output", "-"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == bundle
    assert result.stderr == ""
    assert [name for name, _ in calls] == ["read", "record"]
    assert calls[0][1]["git_context"] is None
    assert calls[1][1]["bundle"] == bundle


@_requires_312
def test_served_handoff_reports_unconfirmed_recording_after_output(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    bundle = {"bundle_type": "handoff", "bundle_version": "1", "generated_from": {"events_limit": 50}, "sprint": {"id": 3, "name": "served", "status": "active", "goal": "goal"}, "summary": {"total": 0, "done": 0, "active": 0, "pending": 0, "blocked": 0}, "active_claims": [], "conflicts": [], "work": {"active_items": [], "ready_items": [], "blocked_items": [], "stale_items": []}, "recent_decisions": [], "recent_events": [], "next_action": {"kind": "no-action", "summary": "Nothing"}, "agent_shutdown_protocol": {"required_before_termination": []}, "resume_instructions": []}
    monkeypatch.setattr(cli_module._served, "read_handoff", lambda *a, **k: bundle)
    monkeypatch.setattr(cli_module._served, "handoff_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network lost")))

    output_path = tmp_path / "handoff.json"
    result = runner.invoke(cli, ["handoff", "--sprint-id", "3", "--output", str(output_path)])

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "served recording is unconfirmed: network lost" in result.stderr
    assert json.loads(output_path.read_text(encoding="utf-8")) == bundle


@_requires_312
def test_served_blind_loop_lists_and_links_use_catalog_facade(runner, tmp_path, monkeypatch):
    """New P0 reads/writes never open a store and preserve their CLI shapes."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    monkeypatch.setattr(cli_module._served, "read_items", lambda *a, **k: {"items": [{"id": 3, "status": "pending", "priority": 3, "track_name": "core", "assignee": None, "title": "Work"}]})
    monkeypatch.setattr(cli_module._served, "read_claims", lambda *a, **k: {"claims": [{"claim_id": 4, "work_item_id": 3, "actor": "agent", "claim_type": "execute", "exclusive": True, "status": "active", "lease_epoch": 1, "identity_status": "verified", "expires_at": "later", "heartbeat": "now"}]})
    monkeypatch.setattr(cli_module._served, "item_ref_add", lambda *a, **k: {"ref_id": 8})
    monkeypatch.setattr(cli_module._served, "item_dep_add", lambda *a, **k: {"dep_id": 9})
    monkeypatch.setattr(cli_module._served, "read_item", lambda *a, **k: {"refs": [], "deps": {"blocked_by": [], "blocks": []}})
    monkeypatch.setattr(cli_module._served, "item_ref_remove", lambda *a, **k: {})
    monkeypatch.setattr(cli_module._served, "item_dep_remove", lambda *a, **k: {})

    for argv in (
        ["item", "list", "--json"], ["claim", "list", "--item-id", "3", "--json"],
        ["claim", "resume", "--instance-id", "instance", "--json"],
        ["item", "ref", "add", "--id", "3", "--type", "doc", "--url", "docs/x.md"],
        ["item", "ref", "list", "--id", "3"], ["item", "ref", "remove", "--id", "3", "--ref-id", "8"],
        ["item", "dep", "add", "--id", "3", "--blocks-item-id", "4"], ["item", "dep", "list", "--id", "3"],
        ["item", "dep", "remove", "--id", "3", "--dep-id", "9"],
    ):
        result = runner.invoke(cli, argv)
        assert result.exit_code == 0, result.output


@_requires_312
def test_served_claim_show_inspects_without_token_or_store(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    monkeypatch.setattr(cli_module._served, "read_claim", lambda *a, **k: {"claim": {
        "claim_id": 3, "actor": "agent", "claim_type": "execute", "status": "active",
        "lease_epoch": 1, "expires_at": "later", "identity_status": "verified",
    }})
    result = runner.invoke(cli, ["claim", "show", "--id", "3", "--json"])
    assert result.exit_code == 0, result.output
    assert "claim_token" not in json.loads(result.output)


@_requires_312
def test_served_item_show_accepts_scoped_reference_and_reports_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda profile, *, repo_id=None, item_id: captured.update(repo_id=repo_id, item_id=item_id) or {
            "item": {"id": item_id, "status": "pending", "title": "Scoped", "sprint_id": 7, "updated_at": "now"},
            "events": [], "active_claims": [], "refs": [], "deps": {"blocked_by": [], "blocks": []},
        },
    )

    result = runner.invoke(cli, ["item", "show", "--id", f"{tmp_path.name}#12", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert captured == {"repo_id": tmp_path.name, "item_id": 12}
    assert payload["resolved_context"]["repo_id"] == tmp_path.name
    assert payload["resolved_context"]["repo_source"] == "flag"
    assert payload["resolved_context"]["backend"] == "served"
    assert payload["resolved_context"]["target"] == "https://vuoro-shared.example/"


@_requires_312
def test_served_item_show_error_reports_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda profile, **kwargs: (_ for _ in ()).throw(ValueError("Item #12 not found.")),
    )

    result = runner.invoke(cli, ["item", "show", "--id", "12"])

    assert result.exit_code == 1
    assert "Item #12 not found." in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_item_show_text_reports_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda profile, **kwargs: {
            "item": {
                "id": 12,
                "status": "pending",
                "title": "Contextual",
                "sprint_id": 7,
                "updated_at": "now",
            },
            "events": [],
            "active_claims": [],
            "refs": [],
            "deps": {"blocked_by": [], "blocks": []},
        },
    )

    result = runner.invoke(cli, ["item", "show", "--id", "12"])

    assert result.exit_code == 0, result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


def test_item_show_rejects_conflicting_reference_and_global_scope(runner, db_path):
    result = runner.invoke(
        cli,
        ["--repo-id", "one", "item", "show", "--id", "two#12"],
    )

    assert result.exit_code == 1
    assert "repo scope mismatch" in result.output


# ---------------------------------------------------------------------------
# event list
# ---------------------------------------------------------------------------


@_requires_312
def test_served_event_list_preserves_knowledge_filter_limit_and_json_parity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_read_events(
        profile, *, repo_id=None, sprint_id, work_item_id=None, after_offset=0, limit=None
    ):
        captured.update(
            repo_id=repo_id,
            sprint_id=sprint_id,
            work_item_id=work_item_id,
            after_offset=after_offset,
            limit=limit,
        )
        return {
            "events": [
                {
                    "id": 1,
                    "work_item_id": 3,
                    "event_type": "update",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:00:00Z",
                    "payload": "{\"summary\": \"not knowledge\"}",
                },
                {
                    "id": 2,
                    "work_item_id": 3,
                    "event_type": "decision",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:01:00Z",
                    "payload": "{\"summary\": \"older knowledge\"}",
                },
                {
                    "id": 3,
                    "work_item_id": 4,
                    "event_type": "risk-accepted",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:02:00Z",
                    "payload": "{\"summary\": \"other item\"}",
                },
                {
                    "id": 4,
                    "work_item_id": 3,
                    "event_type": "risk-accepted",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:03:00Z",
                    "payload": "{\"summary\": \"newest knowledge\"}",
                },
            ]
        }

    monkeypatch.setattr(cli_module._served, "read_events", fake_read_events)

    result = runner.invoke(
        cli,
        [
            "event", "list", "--sprint-id", "11", "--item-id", "3",
            "--knowledge", "--limit", "1", "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["sprint_id"] == 11
    assert captured["work_item_id"] == 3
    assert captured["after_offset"] == 0
    assert captured["limit"] is None
    assert json.loads(result.output) == [
        {
            "id": 4,
            "work_item_id": 3,
            "event_type": "risk-accepted",
            "actor": "worker",
            "created_at": "2026-07-26T10:03:00Z",
            "payload": {"summary": "newest knowledge"},
        }
    ]


@_requires_312
def test_served_event_list_preserves_type_filter_and_text_output(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)

    def fake_read_events(
        profile, *, repo_id=None, sprint_id, work_item_id=None, after_offset=0, limit=None
    ):
        assert sprint_id == 11
        assert work_item_id is None
        assert after_offset == 0
        assert limit is None
        return {
            "events": [
                {
                    "id": 1,
                    "event_type": "decision",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:00:00Z",
                },
                {
                    "id": 2,
                    "event_type": "update",
                    "actor": "worker",
                    "created_at": "2026-07-26T10:01:00Z",
                },
            ]
        }

    monkeypatch.setattr(cli_module._served, "read_events", fake_read_events)

    result = runner.invoke(cli, ["event", "list", "--sprint-id", "11", "--type", "update"])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "#2  [update]  worker  2026-07-26T10:01:00Z\n"
        f"Context: repo={tmp_path.name} (source=marker) backend=served "
        "target=https://vuoro-shared.example/\n"
    )


@_requires_312
def test_served_event_list_empty_and_error_report_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_events",
        lambda profile, **kwargs: {"events": []},
    )

    empty = runner.invoke(cli, ["event", "list", "--sprint-id", "11"])

    assert empty.exit_code == 0, empty.output
    assert "No events found." in empty.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in empty.output

    monkeypatch.setattr(
        cli_module._served,
        "read_events",
        lambda profile, **kwargs: (_ for _ in ()).throw(ValueError("Sprint #11 not found.")),
    )
    missing = runner.invoke(cli, ["event", "list", "--sprint-id", "11"])

    assert missing.exit_code == 1
    assert "Sprint #11 not found." in missing.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in missing.output

    monkeypatch.setattr(
        cli_module._served,
        "project_next_work",
        lambda profile, *, sprint_id=None: {
            "project_id": "workspace",
            "ready_items": [],
            "repositories": [
                {
                    "origin_repo": "member",
                    "sprint": {"id": 8, "name": "Member"},
                    "ready_items": [],
                }
            ],
        },
    )
    project = runner.invoke(cli, ["next-work", "--project"])

    assert project.exit_code == 0, project.output
    assert "Project workspace" in project.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in project.output

    monkeypatch.setattr(
        cli_module._served,
        "project_items",
        lambda profile, **kwargs: {
            "items": [
                {
                    "id": 9,
                    "status": "pending",
                    "priority": 2,
                    "track_name": "build",
                    "assignee": None,
                    "title": "Cross-repo item",
                    "origin_repo": "member",
                }
            ]
        },
    )
    project_items = runner.invoke(cli, ["item", "list", "--project", "--json"])

    assert project_items.exit_code == 0, project_items.output
    assert json.loads(project_items.output)[0]["origin_repo"] == "member"


# ---------------------------------------------------------------------------
# next-work / event add / item add / sprint show
# ---------------------------------------------------------------------------


@_requires_312
def test_served_next_work_text_and_errors_report_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_next_work",
        lambda profile, *, repo_id=None, sprint_id=None: {
            "sprint": {"id": 11, "name": "Current"},
            "ready_items": [
                {"id": 3, "priority": 2, "track_name": "build", "assignee": None, "title": "Ready"}
            ],
        },
    )

    ready = runner.invoke(cli, ["next-work", "--sprint-id", "11"])

    assert ready.exit_code == 0, ready.output
    assert "Ready to start in sprint #11 (Current):" in ready.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in ready.output

    monkeypatch.setattr(
        cli_module._served,
        "read_next_work",
        lambda profile, **kwargs: {"sprint": {"id": 11, "name": "Current"}, "ready_items": []},
    )
    empty = runner.invoke(cli, ["next-work", "--sprint-id", "11"])

    assert empty.exit_code == 0, empty.output
    assert "No pending items ready to start" in empty.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in empty.output

    monkeypatch.setattr(
        cli_module._served,
        "read_next_work",
        lambda profile, **kwargs: (_ for _ in ()).throw(ValueError("Sprint #11 not found.")),
    )
    missing = runner.invoke(cli, ["next-work", "--sprint-id", "11"])

    assert missing.exit_code == 1
    assert "Sprint #11 not found." in missing.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in missing.output


@_requires_312
def test_served_event_add_uses_facade_and_ignores_client_actor(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_event_add(profile, **kwargs):
        captured.update(kwargs)
        return {"event_id": 9, "sprint_id": 11, "item_id": 3, "type": "decision", "actor": "authenticated", "source": "actor"}

    monkeypatch.setattr(cli_module._served, "event_add", fake_event_add)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    result = runner.invoke(cli, ["event", "add", "--sprint-id", f"{tmp_path.name}#11", "--type", "decision", "--item-id", f"{tmp_path.name}#3", "--payload", '{"summary":"x"}', "--json"])
    assert result.exit_code == 0, result.output
    assert captured["repo_id"] == tmp_path.name
    assert captured["payload"] == {"summary": "x"}
    assert "actor" not in captured
    assert json.loads(result.output)["actor"] == "authenticated"


@_requires_312
def test_served_item_add_uses_facade_without_store(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "item_create",
        lambda profile, **kwargs: {"item": {"id": 12, "title": kwargs["title"], "priority": kwargs["priority"]}, "track_name": kwargs["track_name"]},
    )
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    result = runner.invoke(cli, ["item", "add", "--sprint-id", f"{tmp_path.name}#11", "--track", "served", "--title", "Created", "--priority", "2", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"id": 12, "title": "Created", "priority": 2, "track_name": "served"}


@_requires_312
def test_served_sprint_create_uses_facade_without_store(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_sprint_create(profile, **kwargs):
        captured.update(kwargs)
        return {"repo_id": kwargs["repo_id"], "sprint": {"id": 12, "name": kwargs["name"], "status": kwargs["status"]}}

    monkeypatch.setattr(cli_module._served, "sprint_create", fake_sprint_create)
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    result = runner.invoke(cli, ["sprint", "create", "--name", "Dispatch", "--status", "active", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["repo_id"] == tmp_path.name
    assert captured["status"] == "active"
    assert json.loads(result.output) == {"id": 12, "name": "Dispatch", "status": "active"}


@_requires_312
def test_served_write_text_output_and_error_report_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "item_create",
        lambda profile, **kwargs: {
            "item": {"id": 12, "title": kwargs["title"]},
            "track_name": kwargs["track_name"],
        },
    )
    item_result = runner.invoke(
        cli,
        ["item", "add", "--sprint-id", "11", "--track", "served", "--title", "Created"],
    )
    assert item_result.exit_code == 0, item_result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in item_result.output

    monkeypatch.setattr(
        cli_module._served,
        "event_add",
        lambda profile, **kwargs: (_ for _ in ()).throw(ValueError("Sprint #11 not found.")),
    )
    event_result = runner.invoke(
        cli,
        ["event", "add", "--sprint-id", "11", "--type", "decision"],
    )
    assert event_result.exit_code == 1
    assert "Sprint #11 not found." in event_result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in event_result.output


@_requires_312
def test_served_claim_start_text_reports_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "claim_start",
        lambda profile, **kwargs: {
            "operation": "claim_start",
            "claim_id": 9,
            "claim_token": "secret",
            "claim": {"actor": "authenticated"},
            "item_id": kwargs["item_id"],
            "item_status_before": "pending",
            "item_status_after": "active",
            "status_transition_applied": True,
            "refs": [],
        },
    )

    result = runner.invoke(
        cli, ["claim", "start", "--item-id", "12", "--actor", "worker"]
    )

    assert result.exit_code == 0, result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_sprint_show_reads_basic_and_server_aggregate_detail(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        cli_module._served, "read_sprint",
        lambda profile, **kwargs: calls.append(kwargs) or {"sprint": {"id": 11, "name": "Sprint", "goal": "G", "start_date": None, "end_date": None, "status": "active", "kind": "active_sprint"}},
    )
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: pytest.fail("served command opened store"))
    result = runner.invoke(cli, ["sprint", "show", "--id", f"{tmp_path.name}#11", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == 11
    assert calls == [{"repo_id": tmp_path.name, "sprint_id": 11}]
    detail_calls = []
    detail_payload = {
        "id": 11, "name": "Sprint", "goal": "G", "start_date": None,
        "end_date": None, "status": "active", "kind": "active_sprint",
        "detail": {
            "risk": {"overdue": False, "at_risk": False, "date_bound": False, "active_items": 0},
            "stale_count": 0, "track_health": {}, "takeup": {"active_count": 0, "active": []},
        },
    }
    monkeypatch.setattr(
        cli_module._served, "read_sprint_detail",
        lambda profile, **kwargs: detail_calls.append(kwargs) or {"sprint": detail_payload},
    )
    detail = runner.invoke(cli, ["sprint", "show", "--detail", "--json"])
    assert detail.exit_code == 0, detail.output
    assert json.loads(detail.output) == detail_payload
    assert detail_calls == [{"repo_id": tmp_path.name, "sprint_id": None}]


@_requires_312
def test_served_sprint_show_text_reports_resolved_context(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_sprint",
        lambda profile, **kwargs: {
            "sprint": {
                "id": 11, "name": "Sprint", "goal": "G", "start_date": None,
                "end_date": None, "status": "active", "kind": "active_sprint",
            }
        },
    )

    result = runner.invoke(cli, ["sprint", "show", "--id", "11"])

    assert result.exit_code == 0, result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_sprint_list_text_and_empty_output_report_resolved_context(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda profile, **kwargs: {
            "sprints": [
                {
                    "id": 11, "name": "Sprint", "status": "active",
                    "kind": "active_sprint", "start_date": None, "end_date": None,
                }
            ]
        },
    )

    listed = runner.invoke(cli, ["sprint", "list"])

    assert listed.exit_code == 0, listed.output
    assert "Sprint" in listed.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in listed.output

    monkeypatch.setattr(cli_module._served, "read_sprints", lambda profile, **kwargs: {"sprints": []})
    empty = runner.invoke(cli, ["sprint", "list"])

    assert empty.exit_code == 0, empty.output
    assert "No sprints found." in empty.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in empty.output


@_requires_312
def test_served_sprint_show_watch_polls_facade_client_side(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        cli_module._served, "read_sprint",
        lambda profile, **kwargs: calls.append(kwargs) or {"sprint": {"id": 11, "name": "Sprint", "goal": "G", "start_date": None, "end_date": None, "status": "active", "kind": "active_sprint"}},
    )
    monkeypatch.setattr(cli_module, "_clear_terminal_for_watch", lambda: False)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = runner.invoke(cli, ["sprint", "show", "--watch", "--interval", "0.01"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "Watch mode stopped." in result.output


# ---------------------------------------------------------------------------
# item status
# ---------------------------------------------------------------------------


@_requires_312
def test_served_item_status_active_to_done_appends_item_done_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 7, "aggregate_uuid": str(uuid4()), "status": "active"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, repo_id=None, item_id: {"item": item}
    )

    captured = {}

    def fake_lifecycle_arbitrate(profile, *, repo_id=None, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "reason_code": None,
            "reason_detail": None,
            "effect": {"item_id": 7, "previous_status": "active", "status": "done"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli,
        ["item", "status", "--id", f"{tmp_path.name}#7", "--status", "done", "--actor", "served-actor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"item_id": 7, "previous": "active", "status": "done"}

    assert captured["record"]["event_type"] == "item.done"
    assert captured["record"]["payload"]["payload"]["to_status"] == "done"
    assert captured["record"]["actor"] == "served-actor"
    assert captured["record"]["basis_revision"] == f"item:{item['aggregate_uuid']}@status:active"

    records = _outbox_records(tmp_path)
    assert [(r.record_class, r.event_type) for r in records] == [
        ("authority-command", "item.done")
    ]
    assert records[0].event_id == captured["record"]["event_id"]


@_requires_312
def test_served_item_status_pending_to_active_uses_item_transition_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 3, "aggregate_uuid": str(uuid4()), "status": "pending"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, repo_id=None, item_id: {"item": item}
    )
    captured = {}

    def fake_lifecycle_arbitrate(profile, *, repo_id=None, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"item_id": 3, "previous_status": "pending", "status": "active"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli, ["item", "status", "--id", "3", "--status", "active", "--actor", "served-actor"]
    )
    assert result.exit_code == 0, result.output
    assert "pending -> active" in result.output
    assert captured["record"]["event_type"] == "item.transition"
    assert captured["record"]["payload"]["payload"] == {"to_status": "active"}


@_requires_312
def test_served_item_status_sends_only_claim_ref_and_transient_proof(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 1, "aggregate_uuid": str(uuid4()), "status": "pending"}
    monkeypatch.setattr(
        cli_module._served, "read_item",
        lambda profile, *, repo_id=None, item_id: {"item": item},
    )
    captured = {}

    def arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured.update(record=record, credentials=transient_credentials)
        return {"outcome": "accepted", "effect": {"status": "active"}}

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", arbitrate)

    result = runner.invoke(
        cli,
        [
            "item", "status", "--id", "1", "--status", "active", "--actor", "worker",
            "--claim-id", "9", "--claim-token", "secret-token",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = captured["record"]["payload"]["payload"]
    ref = payload["credential_ref"]
    assert payload == {"to_status": "active", "claim_id": 9, "credential_ref": ref}
    assert captured["credentials"] == {ref: "secret-token"}
    assert "secret-token" not in json.dumps(captured["record"])
    assert "secret-token" not in result.output


@_requires_312
def test_served_item_status_surfaces_a_rejected_decision(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 4, "aggregate_uuid": str(uuid4()), "status": "done"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, repo_id=None, item_id: {"item": item}
    )
    monkeypatch.setattr(
        cli_module._served,
        "lifecycle_arbitrate",
        lambda profile, *, repo_id=None, record: {
            "outcome": "rejected",
            "reason_code": "invalid-transition",
            "reason_detail": "cannot transition done -> active",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["item", "status", "--id", "4", "--status", "active", "--actor", "served-actor"]
    )
    assert result.exit_code != 0
    assert "invalid-transition" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_item_status_response_loss_replays_exact_event(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    item = {"id": 5, "aggregate_uuid": str(uuid4()), "status": "active"}
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, repo_id=None, item_id: {"item": item}
    )

    def failed_arbitration(profile, *, repo_id=None, record, transient_credentials):
        assert list(transient_credentials.values()) == ["original-secret"]
        raise RuntimeError("expected sequence 3, received 4")

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", failed_arbitration)
    first = runner.invoke(
        cli, ["item", "status", "--id", "5", "--status", "done", "--actor", "served-actor",
              "--claim-id", "9", "--claim-token", "original-secret"]
    )
    assert first.exit_code != 0
    assert "preserving durable authority request" in first.output
    assert "origin stream" in first.output
    assert "sequence 1" in first.output
    assert "Retry this exact command" in first.output

    captured = []
    monkeypatch.setattr(
        cli_module._served,
        "lifecycle_arbitrate",
        lambda *args, **kwargs: captured.append(kwargs)
        or {"outcome": "accepted", "effect": {"status": "done"}},
    )
    mismatch = runner.invoke(
        cli, ["item", "status", "--id", "5", "--status", "done", "--actor", "served-actor",
              "--claim-id", "9", "--claim-token", "wrong-secret"]
    )
    assert mismatch.exit_code != 0
    assert "requires the original claim proof" in mismatch.output
    assert captured == []
    retry = runner.invoke(
        cli, ["item", "status", "--id", "5", "--status", "done", "--actor", "served-actor",
              "--claim-id", "9", "--claim-token", "original-secret"]
    )
    assert retry.exit_code == 0, retry.output
    records = _outbox_records(tmp_path)
    assert len(records) == 1
    assert records[0].origin_seq == 1
    assert captured[0]["record"]["event_id"] == records[0].event_id
    assert list(captured[0]["transient_credentials"].values()) == ["original-secret"]


# ---------------------------------------------------------------------------
# item edit
# ---------------------------------------------------------------------------


@_requires_312
def test_served_item_edit_reads_revision_then_calls_catalog_not_store(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "_get_store",
        lambda _obj: pytest.fail("served item edit opened a direct store"),
    )
    revision = "item:uuid@description:v3@sha256:" + "a" * 64
    monkeypatch.setattr(
        cli_module._served,
        "read_item",
        lambda profile, **kwargs: {
            "item": {"id": kwargs["item_id"], "edit_revision": revision}
        },
    )
    captured = {}

    def fake_item_edit(profile, **kwargs):
        captured.update(kwargs)
        return {
            "repo_id": tmp_path.name,
            "item": {
                "id": kwargs["item_id"],
                "description": kwargs["description"],
            },
            "event_id": 42,
            "previous_revision": kwargs["expected_revision"],
            "revision": "item:uuid@description:v4@sha256:" + "b" * 64,
        }

    monkeypatch.setattr(cli_module._served, "item_edit", fake_item_edit)

    result = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            "7",
            "--description",
            "Corrected served scope",
            "--actor",
            "ignored-local-actor",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == 7
    assert payload["description"] == "Corrected served scope"
    assert payload["edit_revision"].endswith("b" * 64)
    assert captured["item_id"] == 7
    assert captured["description"] == "Corrected served scope"
    assert captured["expected_revision"] == revision
    assert "actor" not in captured


@_requires_312
def test_served_item_edit_distinguishes_operation_absence_from_missing_item(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)

    def unavailable(profile, **kwargs):
        raise RuntimeError("served-operation-unavailable: work.item.edit")

    monkeypatch.setattr(cli_module._served, "item_edit", unavailable)
    absent = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            "7",
            "--description",
            "Corrected",
            "--expected-revision",
            "known-revision",
        ],
    )
    assert absent.exit_code != 0
    assert "served-operation-unavailable" in absent.output
    assert "item-not-found" not in absent.output

    def missing(profile, **kwargs):
        raise RuntimeError("item-not-found: Item #7 not found")

    monkeypatch.setattr(cli_module._served, "read_item", missing)
    missing_result = runner.invoke(
        cli,
        ["item", "edit", "--id", "7", "--description", "Corrected"],
    )
    assert missing_result.exit_code != 0
    assert "item-not-found" in missing_result.output
    assert "served-operation-unavailable" not in missing_result.output


@_requires_312
def test_item_edit_local_and_served_json_and_text_are_shape_compatible(
    runner, conn, active_sprint, tmp_path, monkeypatch
):
    track = db.get_or_create_track(conn, active_sprint["id"], "edit-parity")
    text_item = db.create_work_item(
        conn, active_sprint["id"], track, "Text parity", "Old text scope"
    )
    json_item = db.create_work_item(
        conn, active_sprint["id"], track, "JSON parity", "Old JSON scope"
    )
    text_before = db.get_work_item_with_edit_revision(conn, text_item)
    json_before = db.get_work_item_with_edit_revision(conn, json_item)
    assert text_before is not None and json_before is not None

    local_text = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            str(text_item),
            "--description",
            "New text scope",
        ],
    )
    local_json_result = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            str(json_item),
            "--description",
            "New JSON scope",
            "--json",
        ],
    )
    assert local_text.exit_code == 0, local_text.output
    assert local_json_result.exit_code == 0, local_json_result.output
    local_json = json.loads(local_json_result.output)

    text_after = db.get_work_item_with_edit_revision(conn, text_item)
    json_after = db.get_work_item_with_edit_revision(conn, json_item)
    assert text_after is not None and json_after is not None
    final = {
        text_item: (text_after[0], text_before[1], text_after[1]),
        json_item: (json_after[0], json_before[1], json_after[1]),
    }

    _configure_served_repo(tmp_path, monkeypatch)

    def fake_item_edit(profile, **kwargs):
        item, previous_revision, revision = final[kwargs["item_id"]]
        assert kwargs["expected_revision"] == previous_revision
        return {
            "repo_id": tmp_path.name,
            "item": item,
            "event_id": 99,
            "previous_revision": previous_revision,
            "revision": revision,
        }

    monkeypatch.setattr(cli_module._served, "item_edit", fake_item_edit)
    served_text = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            str(text_item),
            "--description",
            "New text scope",
            "--expected-revision",
            text_before[1],
        ],
    )
    served_json_result = runner.invoke(
        cli,
        [
            "item",
            "edit",
            "--id",
            str(json_item),
            "--description",
            "New JSON scope",
            "--expected-revision",
            json_before[1],
            "--json",
        ],
    )
    assert served_text.exit_code == 0, served_text.output
    assert served_json_result.exit_code == 0, served_json_result.output
    assert served_text.output.splitlines()[0] == local_text.output.strip()
    assert json.loads(served_json_result.output) == local_json


# ---------------------------------------------------------------------------
# item note
# ---------------------------------------------------------------------------


@_requires_312
def test_served_item_note_calls_work_item_note_not_get_store(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_item_note(profile, **kwargs):
        captured.update(kwargs)
        return {
            "event_id": 42,
            "item_id": kwargs["item_id"],
            "note_type": kwargs["note_type"],
            "summary": kwargs["summary"],
        }

    monkeypatch.setattr(cli_module._served, "item_note", fake_item_note)

    result = runner.invoke(
        cli,
        [
            "item", "note", "--id", "7", "--type", "decision",
            "--summary", "Chose served", "--detail", "extra",
            "--tags", "a, b", "--actor", "ignored-locally",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded note #42 (decision) on item #7: Chose served" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output
    assert captured["item_id"] == 7
    assert captured["note_type"] == "decision"
    assert captured["summary"] == "Chose served"
    assert captured["detail"] == "extra"
    assert captured["tags"] == ["a", "b"]


@_requires_312
def test_served_item_note_surfaces_a_rejection(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)

    def fake_item_note(profile, **kwargs):
        raise RuntimeError("item-not-found: Item #7 not found")

    monkeypatch.setattr(cli_module._served, "item_note", fake_item_note)

    result = runner.invoke(
        cli,
        ["item", "note", "--id", "7", "--type", "decision", "--summary", "x"],
    )
    assert result.exit_code != 0
    assert "item-not-found" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


# ---------------------------------------------------------------------------
# sprint status
# ---------------------------------------------------------------------------


@_requires_312
def test_served_sprint_status_activate_appends_sprint_activate_record(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    sprint = {"id": 11, "aggregate_uuid": str(uuid4()), "status": "planned"}
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda profile, **kwargs: {"sprints": [sprint]},
    )
    captured = {}

    def fake_lifecycle_arbitrate(profile, *, repo_id=None, record):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"sprint_id": 11, "previous_status": "planned", "status": "active"},
        }

    monkeypatch.setattr(cli_module._served, "lifecycle_arbitrate", fake_lifecycle_arbitrate)

    result = runner.invoke(
        cli,
        ["sprint", "status", "--id", "11", "--status", "active", "--actor", "served-actor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"sprint_id": 11, "previous": "planned", "status": "active"}
    assert captured["record"]["event_type"] == "sprint.activate"
    assert captured["record"]["payload"]["payload"] == {}
    assert captured["record"]["basis_revision"] == f"sprint:{sprint['aggregate_uuid']}@status:planned"

    records = _outbox_records(tmp_path)
    assert [(r.record_class, r.event_type) for r in records] == [
        ("authority-command", "sprint.activate")
    ]


@_requires_312
def test_served_sprint_status_close_surfaces_boundary_event(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    sprint = {"id": 12, "aggregate_uuid": str(uuid4()), "status": "active"}
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda profile, **kwargs: {"sprints": [sprint]},
    )
    monkeypatch.setattr(
        cli_module._served,
        "lifecycle_arbitrate",
        lambda profile, *, repo_id=None, record: {
            "outcome": "accepted",
            "effect": {
                "sprint_id": 12,
                "previous_status": "active",
                "status": "closed",
                "boundary_event_id": 99,
                "boundary_revision": "event:99",
            },
        },
    )

    result = runner.invoke(
        cli,
        ["sprint", "status", "--id", "12", "--status", "closed", "--actor", "served-actor", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["boundary_event_id"] == 99
    assert payload["boundary_revision"] == "event:99"


@_requires_312
def test_served_sprint_status_rejects_planned_target_with_no_served_call(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "read_sprints",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli, ["sprint", "status", "--id", "1", "--status", "planned", "--actor", "operator"]
    )
    assert result.exit_code != 0
    assert "no work.lifecycle.arbitrate mapping" in result.output
    assert _outbox_records(tmp_path) == []


@_requires_312
def test_served_sprint_status_not_found(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served, "read_sprints", lambda profile, **kwargs: {"sprints": []}
    )

    result = runner.invoke(
        cli, ["sprint", "status", "--id", "42", "--status", "active", "--actor", "served-actor"]
    )
    assert result.exit_code != 0
    assert "Sprint #42 not found" in result.output


# ---------------------------------------------------------------------------
# Step 1 helper: _mint_authority_command_record / _served_record_argument
# ---------------------------------------------------------------------------


def test_mint_authority_command_record_appends_and_returns_a_durable_record(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli_module, "_detect_runtime_session_id", lambda explicit: "rs-fallback")
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="item.transition",
        actor="worker",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "item",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 5,
        },
        payload={"to_status": "active"},
        basis_revision=f"item:{aggregate_uuid}@status:pending",
        outbox_path=outbox_path,
    )
    assert durable.event_type == "item.transition"
    assert durable.record_class == "authority-command"
    assert durable.origin_seq == 1
    assert durable.runtime_session_id == "rs-fallback"
    assert durable.payload["payload"] == {"to_status": "active"}

    producer = outbox.open_outbox(outbox_path)
    try:
        records = outbox.list_records(producer)
    finally:
        producer.close()
    assert len(records) == 1
    assert records[0].event_id == durable.event_id


def test_mint_authority_command_record_independent_event_and_correlation_ids_when_omitted(
    tmp_path,
):
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="sprint.activate",
        actor="operator",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "sprint",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 1,
        },
        payload={},
        basis_revision=f"sprint:{aggregate_uuid}@status:planned",
        outbox_path=outbox_path,
    )
    # Matches authority_submit's pre-existing behavior when --event-id is
    # omitted: event_id and correlation_id are two independently generated
    # UUIDs, not the same value.
    assert durable.event_id != durable.correlation_id


def test_served_record_argument_matches_record_definition_field_set(tmp_path):
    outbox_path = tmp_path / "outbox.db"
    aggregate_uuid = str(uuid4())
    durable = cli_module._mint_authority_command_record(
        record_type="sprint.close",
        actor="operator",
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "sprint",
            "aggregate_uuid": aggregate_uuid,
            "aggregate_id": 2,
        },
        payload={},
        basis_revision=f"sprint:{aggregate_uuid}@status:active",
        outbox_path=outbox_path,
    )
    record = cli_module._served_record_argument(durable)
    assert set(record) == {
        "origin_stream_id",
        "origin_seq",
        "event_id",
        "schema_version",
        "record_class",
        "event_type",
        "actor",
        "runtime_session_id",
        "occurred_at",
        "basis_revision",
        "correlation_id",
        "causation_id",
        "payload",
        "payload_sha256",
        "created_at",
    }
    assert record["event_id"] == durable.event_id
    assert record["event_type"] == "sprint.close"


# ---------------------------------------------------------------------------
# claim heartbeat / claim release (#1195 Group A, Build A2)
# ---------------------------------------------------------------------------


def _credential_dir(tmp_path):
    return tmp_path / ".sprintctl" / "authority-credentials"


def _stub_claim_context(monkeypatch, *, claim_id, actor, authority_repo_uuid, claim_revision):
    def fake_claim_context(profile, *, repo_id=None, claim_id: int):
        return {
            "repo_id": "repo-x",
            "authority_repo_uuid": authority_repo_uuid,
            "actor": actor,
            "claim": {"claim_id": claim_id, "work_item_id": 3, "actor": actor},
            "claim_revision": claim_revision,
        }

    monkeypatch.setattr(cli_module._served, "claim_context", fake_claim_context)


@_requires_312
def test_served_claim_heartbeat_mints_claim_renew_with_metadata_parity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    authority_repo_uuid = str(uuid4())
    _stub_claim_context(
        monkeypatch,
        claim_id=9,
        actor="worker-1",
        authority_repo_uuid=authority_repo_uuid,
        claim_revision="claim:9@sha256:" + "a" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = transient_credentials
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 9,
                "work_item_id": 3,
                "actor": "worker-1",
                "expires_at": "2026-07-23T01:05:00Z",
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "9", "--claim-token", "secret-proof",
            "--ttl", "600", "--branch", "feat/x", "--worktree", "/wt",
            "--commit-sha", "abc123", "--pr-ref", "org/repo#1",
            "--runtime-session-id", "rs-1", "--instance-id", "inst-1",
            "--hostname", "host-1", "--pid", "4242", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    record = captured["record"]
    assert record["event_type"] == "claim.renew"
    assert record["actor"] == "worker-1"
    assert record["basis_revision"] == "claim:9@sha256:" + "a" * 64

    payload = record["payload"]["payload"]
    assert payload["claim_id"] == 9
    assert payload["ttl_seconds"] == 600
    assert payload["metadata"] == {
        "runtime_session_id": "rs-1",
        "instance_id": "inst-1",
        "branch": "feat/x",
        "worktree_path": "/wt",
        "commit_sha": "abc123",
        "pr_ref": "org/repo#1",
        "hostname": "host-1",
        "pid": 4242,
    }
    ref = payload["credential_ref"]
    assert captured["transient_credentials"] == {ref: "secret-proof"}

    refs = record["payload"]["refs"]
    assert refs["repo_id"] == authority_repo_uuid
    assert refs["aggregate_type"] == "claim"
    assert refs["claim_id"] == 9

    payload_json = json.loads(result.output)
    assert payload_json["heartbeat_ttl_seconds"] == 600
    assert payload_json["expires_at"] == "2026-07-23T01:05:00Z"

    # Terminal accepted decision clears the retry sidecar.
    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_heartbeat_omits_metadata_when_all_fields_are_none(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=1,
        actor="worker-1",
        authority_repo_uuid=None,
        claim_revision="claim:1@sha256:" + "b" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 1, "expires_at": "2026-07-23T01:05:00Z"},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)
    monkeypatch.setattr(cli_module, "_detect_runtime_session_id", lambda explicit: None)
    monkeypatch.setattr(cli_module, "_detect_hostname", lambda explicit: "detected-host")
    monkeypatch.setattr(cli_module, "_detect_instance_id", lambda explicit: "detected-instance")
    monkeypatch.setattr(cli_module, "_detect_pid", lambda explicit: 1)

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "1", "--claim-token", "secret"]
    )
    assert result.exit_code == 0, result.output
    payload = captured["record"]["payload"]["payload"]
    assert "runtime_session_id" not in payload.get("metadata", {})
    assert "branch" not in payload.get("metadata", {})
    assert captured["record"]["payload"]["refs"]["repo_id"] == _manifest_repo_uuid(tmp_path)
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_claim_heartbeat_warns_before_expiry(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=2,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:2@sha256:" + "c" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, repo_id=None, record, transient_credentials: {
            "outcome": "accepted",
            "effect": {"claim_id": 2, "expires_at": "2026-07-23T00:00:30Z"},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "2", "--claim-token", "secret",
            "--ttl", "30", "--warn-before-expiry", "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "heartbeat refreshed (ttl=30s, expires=2026-07-23T00:00:30Z)" in result.output
    assert "Warning: claim #2 expires in 30s" in result.output


@_requires_312
def test_served_claim_heartbeat_ignores_mismatched_advisory_actor(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=3,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:3@sha256:" + "d" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        return {"outcome": "accepted", "effect": {"claim_id": 3, "expires_at": "x"}}

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "heartbeat", "--id", "3", "--claim-token", "secret",
            "--actor", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


@_requires_312
def test_served_claim_heartbeat_surfaces_a_rejected_decision_and_clears_sidecar(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=4,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:4@sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, repo_id=None, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "invalid-claim-proof",
            "reason_detail": "claim proof is invalid",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "4", "--claim-token", "wrong-secret"]
    )
    assert result.exit_code != 0
    assert "invalid-claim-proof" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output
    # A rejected decision is terminal too: the sidecar is cleared, not kept.
    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_heartbeat_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=5,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:5@sha256:" + "f" * 64,
    )

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "heartbeat", "--id", "5", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "connection reset" in result.output
    # Unknown/transport failure: the sidecar is retained for a retry.
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


@_requires_312
def test_served_claim_release_clears_sidecar_on_accepted_decision(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=6,
        actor="worker-1",
        authority_repo_uuid=None,
        claim_revision="claim:6@sha256:" + "1" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = transient_credentials
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 6, "released": True},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "release", "--id", "6", "--claim-token", "secret-proof"]
    )
    assert result.exit_code == 0, result.output
    assert "Claim #6 released." in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output

    record = captured["record"]
    assert record["event_type"] == "claim.release"
    assert record["actor"] == "worker-1"
    assert record["payload"]["refs"]["repo_id"] == _manifest_repo_uuid(tmp_path)
    payload = record["payload"]["payload"]
    assert set(payload) == {"claim_id", "credential_ref"}
    assert payload["claim_id"] == 6
    ref = payload["credential_ref"]
    assert captured["transient_credentials"] == {ref: "secret-proof"}

    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_release_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=7,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:7@sha256:" + "2" * 64,
    )

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        raise RuntimeError("timeout")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli, ["claim", "release", "--id", "7", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "timeout" in result.output
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


@_requires_312
def test_served_claim_release_surfaces_a_rejected_decision(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=8,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:8@sha256:" + "3" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, repo_id=None, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "expired-grant",
            "reason_detail": "claim grant has expired",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli, ["claim", "release", "--id", "8", "--claim-token", "secret-proof"]
    )
    assert result.exit_code != 0
    assert "expired-grant" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_claim_release_ignores_mismatched_advisory_actor(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=10,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:10@sha256:" + "4" * 64,
    )
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        return {"outcome": "accepted", "effect": {"claim_id": 10, "released": True}}

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "release", "--id", "10", "--claim-token", "secret",
            "--actor", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


# ---------------------------------------------------------------------------
# claim handoff (#1195 Group A, Build A3)
# ---------------------------------------------------------------------------


def _stub_read_item(monkeypatch, *, item):
    monkeypatch.setattr(
        cli_module._served, "read_item", lambda profile, *, repo_id=None, item_id: {"item": item}
    )


@_requires_312
def test_served_claim_handoff_rotate_mints_new_token_and_bumps_lease_epoch(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=20,
        actor="worker-1",
        authority_repo_uuid=None,
        claim_revision="claim:20@sha256:" + "5" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 3, "sprint_id": 55, "title": "Do the thing"})
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = dict(transient_credentials)
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 20,
                "work_item_id": 3,
                "actor": "recipient-actor",
                "claim_type": "work",
                "exclusive": True,
                "heartbeat": "2026-07-23T01:00:00Z",
                "expires_at": "2026-07-23T01:05:00Z",
                "status": "active",
                "lease_epoch": 2,
                "runtime_session_id": None,
                "instance_id": None,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "20", "--claim-token", "old-secret",
            "--actor", "recipient-actor", "--mode", "rotate", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    record = captured["record"]
    assert record["event_type"] == "claim.handoff"
    assert record["actor"] == "worker-1"  # authenticated actor, not the recipient
    assert record["basis_revision"] == "claim:20@sha256:" + "5" * 64
    assert record["payload"]["refs"]["repo_id"] == _manifest_repo_uuid(tmp_path)

    payload = record["payload"]["payload"]
    assert payload["claim_id"] == 20
    assert payload["to_actor"] == "recipient-actor"
    assert payload["mode"] == "rotate"
    old_ref = payload["credential_ref"]
    proposed_ref = payload["proposed_credential_ref"]
    assert old_ref != proposed_ref

    creds = captured["transient_credentials"]
    assert set(creds) == {old_ref, proposed_ref}
    assert creds[old_ref] == "old-secret"
    new_token = creds[proposed_ref]
    assert new_token != "old-secret"

    bundle = json.loads(result.output)
    assert bundle["mode"] == "rotate"
    assert bundle["claim"]["claim_token"] == new_token
    assert bundle["claim"]["lease_epoch"] == 2
    assert bundle["item"]["id"] == 3
    assert bundle["sprint_id"] == 55
    assert bundle["performed_by"] == "worker-1"

    # Terminal accepted decision clears the retry sidecar.
    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_handoff_transfer_keeps_token_unchanged(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=21,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:21@sha256:" + "6" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 4, "sprint_id": 56})
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        captured["transient_credentials"] = dict(transient_credentials)
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 21,
                "work_item_id": 4,
                "actor": "recipient-actor",
                "lease_epoch": 1,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "21", "--claim-token", "shared-secret",
            "--actor", "recipient-actor", "--mode", "transfer", "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = captured["record"]["payload"]["payload"]
    assert payload["mode"] == "transfer"
    assert "proposed_credential_ref" not in payload
    # Only one credential binding for transfer mode -- no new token minted.
    assert len(captured["transient_credentials"]) == 1

    bundle = json.loads(result.output)
    assert bundle["claim"]["claim_token"] == "shared-secret"


@_requires_312
def test_served_claim_handoff_rejects_allow_legacy_adopt(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "claim_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "22", "--claim-token", "secret",
            "--actor", "recipient-actor", "--allow-legacy-adopt",
        ],
    )
    assert result.exit_code != 0
    assert "--allow-legacy-adopt is not supported in served mode" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output
    assert _outbox_records(tmp_path) == []


@_requires_312
def test_served_claim_handoff_requires_claim_token(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "claim_context",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = runner.invoke(
        cli,
        ["claim", "handoff", "--id", "23", "--actor", "recipient-actor"],
    )
    assert result.exit_code != 0
    assert "--claim-token is required in served mode" in result.output
    assert _outbox_records(tmp_path) == []


@_requires_312
def test_served_claim_handoff_rejects_wrong_claim_token(runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=24,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:24@sha256:" + "7" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, repo_id=None, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "invalid-claim-proof",
            "reason_detail": "claim proof is invalid",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "24", "--claim-token", "wrong-secret",
            "--actor", "recipient-actor",
        ],
    )
    assert result.exit_code != 0
    assert "invalid-claim-proof" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output
    # A rejected decision is terminal too: the sidecar is cleared, not kept.
    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_handoff_surfaces_credential_conflict(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=25,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:25@sha256:" + "8" * 64,
    )
    monkeypatch.setattr(
        cli_module._served,
        "claim_arbitrate",
        lambda profile, *, repo_id=None, record, transient_credentials: {
            "outcome": "rejected",
            "reason_code": "credential-conflict",
            "reason_detail": "proposed claim proof is already in use",
            "effect": {},
        },
    )

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "25", "--claim-token", "secret",
            "--actor", "recipient-actor", "--mode", "rotate",
        ],
    )
    assert result.exit_code != 0
    assert "credential-conflict" in result.output
    assert list(_credential_dir(tmp_path).glob("*")) == []


@_requires_312
def test_served_claim_handoff_keeps_sidecar_on_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=26,
        actor="worker-1",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:26@sha256:" + "9" * 64,
    )

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "26", "--claim-token", "secret",
            "--actor", "recipient-actor",
        ],
    )
    assert result.exit_code != 0
    assert "connection reset" in result.output
    # Unknown/transport failure: the sidecar is retained for a retry.
    sidecars = list(_credential_dir(tmp_path).glob("*"))
    assert len(sidecars) == 1


@_requires_312
def test_served_claim_handoff_ignores_mismatched_performed_by(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=27,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:27@sha256:" + "0" * 64,
    )
    _stub_read_item(monkeypatch, item={"id": 9, "sprint_id": None})
    captured = {}

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        captured["record"] = record
        return {
            "outcome": "accepted",
            "effect": {"claim_id": 27, "work_item_id": 9, "actor": "recipient-actor"},
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "27", "--claim-token", "secret",
            "--actor", "recipient-actor", "--performed-by", "someone-else",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "authenticated-actor" in result.output
    assert "'someone-else' was not sent and is ignored" in result.output
    assert captured["record"]["actor"] == "authenticated-actor"


@_requires_312
def test_served_claim_handoff_degrades_bundle_when_item_fetch_fails(
    runner, tmp_path, monkeypatch
):
    """An already-accepted handoff must not be reported as a failure just
    because the post-acceptance item-detail fetch for the bundle breaks."""
    _configure_served_repo(tmp_path, monkeypatch)
    _stub_claim_context(
        monkeypatch,
        claim_id=31,
        actor="authenticated-actor",
        authority_repo_uuid=str(uuid4()),
        claim_revision="claim:31@sha256:" + "1" * 64,
    )

    def fake_read_item(profile, *, repo_id=None, item_id):
        raise RuntimeError("transport blip")

    monkeypatch.setattr(cli_module._served, "read_item", fake_read_item)

    def fake_claim_arbitrate(profile, *, repo_id=None, record, transient_credentials):
        return {
            "outcome": "accepted",
            "effect": {
                "claim_id": 31,
                "work_item_id": 9,
                "actor": "recipient-actor",
                "claim_type": "work",
                "exclusive": True,
                "heartbeat": "2026-07-23T01:00:00Z",
                "expires_at": "2026-07-23T01:05:00Z",
                "status": "active",
                "lease_epoch": 2,
                "runtime_session_id": None,
                "instance_id": None,
            },
        }

    monkeypatch.setattr(cli_module._served, "claim_arbitrate", fake_claim_arbitrate)

    result = runner.invoke(
        cli,
        [
            "claim", "handoff", "--id", "31", "--claim-token", "secret",
            "--actor", "recipient-actor", "--mode", "transfer", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "transport blip" in result.output
    assert "handoff succeeded" in result.output

    # stdout mixes the warning (stderr in real usage, merged here by the
    # runner) with the JSON bundle -- parse just the JSON line.
    json_line = [line for line in result.output.splitlines() if line.startswith("{")][0]
    bundle_start = result.output.index(json_line)
    bundle = json.loads(result.output[bundle_start:])
    assert bundle["item"] is None
    assert bundle["sprint_id"] is None
    assert bundle["claim"]["claim_token"] == "secret"

    # The handoff itself was accepted, so the retry sidecar is still cleared.
    assert list(_credential_dir(tmp_path).glob("*")) == []


# ---------------------------------------------------------------------------
# pilot cutover-evidence (#1211)
# ---------------------------------------------------------------------------


def _fake_cutover_payload(**overrides) -> dict:
    payload = {
        "contract_version": "1",
        "config": {
            "pilot_enabled": True,
            "authority_command_mode": "shadow",
            "projection_reads_enabled": False,
        },
        "parity": None,
        "watermark": {
            "healthy": True,
            "fallback_reason": None,
            "age_seconds": 5,
            "max_age_seconds": 300,
        },
        "stale_tools": {"status": "ok", "incidents": [], "findings": []},
        "rollback_rehearsal": {"rollback_ok": True},
        "promotable": False,
        "blockers": ["parity-not-evaluated"],
    }
    payload.update(overrides)
    return payload


@_requires_312
def test_served_cutover_evidence_skip_parity_invokes_operation_with_none_parity(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: (_ for _ in ()).throw(
            AssertionError("should not be called when --skip-parity is set")
        ),
    )
    captured = {}

    def fake_cutover_evidence(profile, *, repo_id=None, parity, max_watermark_age_seconds, rehearse):
        captured["parity"] = parity
        captured["max_watermark_age_seconds"] = max_watermark_age_seconds
        captured["rehearse"] = rehearse
        return _fake_cutover_payload(parity=None, promotable=True, blockers=[])

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(
        cli, ["pilot", "cutover-evidence", "--skip-parity", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parity"] is None
    assert payload["promotable"] is True

    assert captured["parity"] is None
    assert captured["max_watermark_age_seconds"] == cli_module._cutover.DEFAULT_MAX_WATERMARK_AGE_SECONDS
    assert captured["rehearse"] is True


@_requires_312
def test_served_cutover_evidence_pilot_disabled_passes_none_parity_without_error(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: SimpleNamespace(enabled=False),
    )
    captured = {}

    def fake_cutover_evidence(profile, *, repo_id=None, parity, max_watermark_age_seconds, rehearse):
        captured["parity"] = parity
        return _fake_cutover_payload(parity=None)

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["parity"] is None


@_requires_312
def test_served_cutover_evidence_fails_closed_when_pilot_enabled_and_parity_requested(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._pilot,
        "shadow_pilot_status",
        lambda *, cwd=None, repo_root=None: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        cli_module._served,
        "cutover_evidence",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not be called: no served parity source exists")
        ),
    )

    result = runner.invoke(cli, ["pilot", "cutover-evidence"])
    assert result.exit_code != 0
    assert "cannot compute parity" in result.output
    assert "--skip-parity" in result.output


@_requires_312
def test_served_cutover_evidence_passes_max_watermark_age_and_skip_rollback_rehearsal(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    captured = {}

    def fake_cutover_evidence(profile, *, repo_id=None, parity, max_watermark_age_seconds, rehearse):
        captured["max_watermark_age_seconds"] = max_watermark_age_seconds
        captured["rehearse"] = rehearse
        return _fake_cutover_payload(parity=None, rollback_rehearsal=None)

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(
        cli,
        [
            "pilot", "cutover-evidence", "--skip-parity",
            "--max-watermark-age-seconds", "60",
            "--skip-rollback-rehearsal", "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_watermark_age_seconds"] == 60
    assert captured["rehearse"] is False
    assert json.loads(result.output)["rollback_rehearsal"] is None


@_requires_312
def test_served_cutover_evidence_text_output_matches_local_shape(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module._served,
        "cutover_evidence",
        lambda *a, **k: _fake_cutover_payload(promotable=True, blockers=[]),
    )

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--skip-parity"])
    assert result.exit_code == 0, result.output
    assert "Cutover dogfood evidence (contract v1):" in result.output
    assert "Parity: not evaluated" in result.output
    assert "Promotable: True" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output


@_requires_312
def test_served_cutover_evidence_surfaces_a_transport_failure(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)

    def fake_cutover_evidence(profile, *, repo_id=None, parity, max_watermark_age_seconds, rehearse):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cli_module._served, "cutover_evidence", fake_cutover_evidence)

    result = runner.invoke(cli, ["pilot", "cutover-evidence", "--skip-parity"])
    assert result.exit_code != 0
    assert "connection reset" in result.output
    assert f"Context: repo={tmp_path.name} (source=marker) backend=served" in result.output
