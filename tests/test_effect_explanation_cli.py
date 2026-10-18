"""Explain observed owner facts without manufacturing a complete acceptance chain."""

import copy
import json
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import served
from sprintctl.cli import cli
from sprintctl.commands import inbox
from sprintctl.effect_explanation import render_effect_explanation
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

INTENT = "intent_" + "A" * 26
ARGS = ["effect", "explain", "--intent-id", INTENT, "--revision", "1",
        "--canonical-intent-digest", "a" * 64, "--artifact-sha256", "sha256:" + "b" * 64]


@pytest.fixture
def owner_response():
    result = {"repo_id": "sprintctl", "snapshot": {
        "schema": "sprintctl-effect-acceptance-snapshot/v1", "snapshot_digest": "sha256:" + "0" * 64,
        "authorizes_acceptance": False, "requested_basis_matches": True, "artifact_digest_matches": True,
        "intent": {"intent_id": INTENT, "revision": 1, "canonical_intent_digest": "a" * 64,
                   "artifact_sha256": "sha256:" + "b" * 64, "state": "accepted", "item_id": 7,
                   "release_digest": "c" * 64},
        "current_item": {"item_id": 7, "status": "done", "work_edit_revision": "edit:1", "status_revision": "done"},
        "current_release": {"release_digest": "c" * 64, "item_revision": "edit:1",
                            "effect_verification_required": True, "effect_check_revisions": None,
                            "revision_currentness": "unknown"},
        "decision_facts": [{"id": 9, "kind": "accept", "release_digest": "c" * 64, "evidence_digests": []}],
        "decision_count": 1, "decision_digest": "sha256:" + "d" * 64, "decision_facts_complete": True,
        "reference_count": 0, "reference_set_digest": "sha256:" + "e" * 64, "reference_facts_complete": True,
        "evidence_obligations": [{"label": "Check patch", "label_truncated": False,
                                  "label_sha256": "sha256:" + "f" * 64, "mapping": "unknown"}],
        "evidence_obligation_count": 1, "evidence_obligations_complete": True,
        "receipt": {"status": "not-supplied", "comparison": "unknown", "evidence_digest": None,
                    "check_revision_comparison": "unknown"},
        "lease_settlement": "unknown",
    }}
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == "work.effect.acceptance-snapshot-v1")
    jsonschema.Draft202012Validator(contract.result_schema).validate(result)
    return result


@pytest.fixture
def calls(monkeypatch, owner_response):
    config = SimpleNamespace(served_profile=object(), repo_id="sprintctl")
    monkeypatch.setattr(inbox, "_served_config_or_none", lambda obj: config)
    recorded = []

    def invoke(profile, operation, arguments, *, repo_id):
        assert profile is config.served_profile and repo_id == "sprintctl"
        recorded.append((operation, arguments))
        return owner_response

    monkeypatch.setattr(served, "invoke_agent_operation", invoke)
    return recorded


def test_explanation_uses_one_read_and_keeps_acceptance_and_target_evidence_distinct(runner, calls):
    result = runner.invoke(cli, ARGS)
    assert result.exit_code == 0, result.output
    assert calls == [("work.effect.acceptance-snapshot-v1", {"intent_id": INTENT, "revision": 1,
        "canonical_intent_digest": "a" * 64, "artifact_sha256": "sha256:" + "b" * 64})]
    assert "Owner-recorded intent state: \"accepted\"" in result.output
    assert "terminal work acceptance is not established" in result.output
    assert "target effect observation is unknown" in result.output
    assert "not a seal of the entire response" in result.output
    assert "authorizes_acceptance: false" in result.output


@pytest.mark.parametrize("field,label", [("requested_basis_matches", "REQUESTED BASIS"),
                                        ("artifact_digest_matches", "REQUESTED ARTIFACT")])
def test_mismatch_remains_visible_even_when_intent_accepted_and_receipt_matches(runner, calls, owner_response, field, label):
    owner_response["snapshot"][field] = False
    owner_response["snapshot"]["receipt"].update(status="owner-record-observed", comparison="matches")
    result = runner.invoke(cli, ARGS)
    assert result.exit_code == 0
    assert f"{label}: MISMATCH" in result.output
    assert "artifact-check comparison: matches" in result.output
    assert len(calls) == 1


def test_json_keeps_every_owner_field_and_passes_optional_receipt_reference(runner, calls, owner_response, tmp_path):
    ref = {"run_id": "run_caller", "item_id": "evidence-1"}
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(ref))
    result = runner.invoke(cli, [*ARGS, "--verification-ref", str(path), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == owner_response
    assert calls[0][1]["verification_ref"] == ref


@pytest.mark.parametrize("status,expected", [("not-supplied", "not-supplied"),
    ("unknown-no-evidence-authority", "unknown-no-evidence-authority"),
    ("not-found", "not found/visible for this caller")])
def test_receipt_visibility_preserves_unknown_and_caller_scope(owner_response, status, expected):
    owner_response["snapshot"]["receipt"]["status"] = status
    text = render_effect_explanation(owner_response)
    assert expected in text and "artifact-check comparison: unknown" in text


def test_applied_lifecycle_and_done_work_do_not_imply_observed_effect(owner_response):
    owner_response["snapshot"]["intent"]["state"] = "applied"
    before = copy.deepcopy(owner_response)
    text = render_effect_explanation(owner_response)
    assert 'Owner-recorded intent state: "applied"' in text
    assert "does not establish an observed external effect" in text
    assert "accepting identity/time and attempt history are not included" in text
    assert owner_response == before


def test_absent_release_check_revision_and_overflow_remain_unknown(owner_response):
    snapshot = owner_response["snapshot"]
    snapshot.update(current_release=None, decision_facts_complete=False, decision_digest=None,
                    reference_facts_complete=False, reference_set_digest=None,
                    evidence_obligations_complete=False, evidence_obligation_count=65)
    snapshot["evidence_obligations"][0]["label_truncated"] = True
    text = render_effect_explanation(owner_response)
    assert "Intent/current Release comparison: unknown" in text
    assert "Check revision comparison: unknown" in text
    assert "Decision collection digest: unknown" in text
    assert "Reference collection digest: unknown" in text
    assert "complete: False" in text and "[label truncated]" in text


def test_old_release_and_receipt_mismatch_are_not_check_revision_mismatch(owner_response):
    owner_response["snapshot"]["intent"]["release_digest"] = "0" * 64
    owner_response["snapshot"]["receipt"]["comparison"] = "mismatch"
    text = render_effect_explanation(owner_response)
    assert "Intent/current Release comparison: mismatch" in text
    assert "artifact-check comparison: mismatch" in text
    assert "Check revision comparison: unknown" in text


@pytest.mark.parametrize("contents", ["[]", "{}", '{"run_id":"r","item_id":"e","extra":"x"}', "not json"])
def test_invalid_receipt_reference_fails_before_owner_call(runner, calls, tmp_path, contents):
    path = tmp_path / "ref.json"
    path.write_text(contents)
    result = runner.invoke(cli, [*ARGS, "--verification-ref", str(path)])
    assert result.exit_code != 0
    assert calls == []


def test_invalid_digest_fails_before_owner_call(runner, calls):
    result = runner.invoke(cli, [*ARGS, "--artifact-sha256", "sha256:bad"])
    assert result.exit_code != 0
    assert calls == []


def test_explanation_refuses_local_backend_instead_of_opening_a_store(runner, monkeypatch):
    monkeypatch.setattr(inbox, "_served_config_or_none", lambda obj: None)
    result = runner.invoke(cli, ARGS)
    assert result.exit_code != 0
    assert "SPRINTCTL_BACKEND=served" in result.output


def test_owner_authority_refusal_remains_explicit(runner, calls, monkeypatch):
    errors = pytest.importorskip("vuoro_client.errors")

    def refuse(*args, **kwargs):
        raise errors.InvocationRejectedError("authority-required", "work read is required", status_code=403)

    monkeypatch.setattr(served, "invoke_agent_operation", refuse)
    result = runner.invoke(cli, ARGS)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "authority-required"
