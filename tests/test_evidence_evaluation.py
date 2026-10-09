"""Discriminating read-time evaluation histories, without manufactured trust."""

from copy import deepcopy
import pytest
from sprintctl import evidence_evaluation as e

AT = "2026-10-09T12:00:00Z"
BASIS = {"item_id": 1, "expected_revision": "item:x", "release_digest": "a" * 64}
BINDING = {
    "repo_id": "fixture",
    "run_id": "run_" + "A" * 26,
    "principal_id": "fixture:1:0",
    "workspace_id": "fixture",
    "client_id": None,
    "grant_id": None,
}


def item(**window):
    return {
        "item_id": "e1",
        "digest": "sha256:" + "b" * 64,
        "kind": "test",
        "collector": "caller",
        "ref": "local:x",
        "provenance": {},
        "claims": [],
        "chain_seq": 0,
        "chain_prev_digest": None,
        "validity": {
            "basis": "bounded",
            "valid_from": "2026-10-09T11:00:00Z",
            "valid_until": AT,
            "component_digests": {},
            **window,
        },
    }


def evaluate(items, **kwargs):
    return e.evaluate(
        repo_id="fixture",
        run_binding=BINDING,
        subject="effect",
        requested_basis=BASIS,
        current_basis={**BASIS, "release_item_revision": BASIS["expected_revision"]},
        items=items,
        expected_tail=e.chain_tail(items),
        as_of=AT,
        current_input_digests={},
        observed_at=AT,
        **kwargs,
    )


@pytest.mark.parametrize(
    "clock,status",
    [
        ("2026-10-09T10:59:59Z", "not-yet-valid"),
        ("2026-10-09T11:00:00Z", "valid"),
        (AT, "valid"),
        ("2026-10-09T12:00:00.000001Z", "expired"),
    ],
)
def test_inclusive_validity_boundaries(clock, status):
    assert e.validity(item(), e.instant(clock), {})["status"] == status


@pytest.mark.parametrize(
    "inputs,status,missing,changed",
    [
        ({}, "unknown", ["tree"], []),
        ({"tree": "old"}, "valid", [], []),
        ({"tree": "new"}, "changed", [], ["tree"]),
    ],
)
def test_missing_is_not_matching(inputs, status, missing, changed):
    result = e.validity(
        item(
            basis="until_inputs_change",
            valid_until=None,
            component_digests={"tree": "old"},
        ),
        e.instant(AT),
        inputs,
    )
    assert (result["status"], result["missing_inputs"], result["changed_inputs"]) == (
        status,
        missing,
        changed,
    )


@pytest.mark.parametrize(
    "window",
    [
        {"valid_from": "2026-10-09T11:00:00"},
        {"basis": "bounded", "valid_until": None},
        {"basis": "indefinite"},
        {"basis": "until_inputs_change", "valid_until": None},
        {"valid_until": "2026-10-08T12:00:00Z"},
        {"basis": "forged"},
    ],
)
def test_invalid_windows_are_explicit(window):
    assert e.validity(item(**window), e.instant(AT), {})["status"] == "invalid"


def test_authored_accepted_used_terminal_and_trusted_labels_never_establish_execution():
    original = item()
    original["claims"] = [
        {
            "subject": "effect",
            "claim_type": kind,
            "detail": {"trusted": True, "attempt_accepted": True, "grant_used": True},
        }
        for kind in ["effect_completed", "effect_not_invoked", "observation"]
    ]
    original["provenance"] = {"authority": "owner", "trusted": True}
    before = deepcopy(original)
    result = evaluate([original])
    assert original == before
    assert (
        result["authenticated_execution_facts"] == []
        and result["authority_coverage"] == "unsupported"
    )
    assert (
        result["effect_state"] == "unknown" and result["recommendation"] == "reconcile"
    )
    assert (
        not result["authorizes_execution"] and len(result["authored_assertions"]) == 3
    )
    assert all(
        a["authority"] == "authored-assertion" for a in result["authored_assertions"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("claims", [{"subject": "effect", "claim_type": "effect_completed"}]),
        ("provenance", {"source": "other"}),
        ("validity", {"basis": "forged"}),
    ],
)
def test_complete_snapshot_hash_covers_more_than_chain_entry(field, value):
    first = item()
    second = deepcopy(first)
    second[field] = value
    assert e.chain_tail([first]) == e.chain_tail([second])
    assert evaluate([first])["snapshot_digest"] != evaluate([second])["snapshot_digest"]


@pytest.mark.parametrize(
    "mutation",
    [lambda x: x.update(chain_seq=1), lambda x: x.update(chain_prev_digest="bad")],
)
def test_chain_corruption_fails_instead_of_truncating(mutation):
    bad = item()
    mutation(bad)
    with pytest.raises(e.EvaluationError, match="gap, duplicate"):
        e.chain_tail([bad])


def test_exact_tail_and_chain_bound_force_failure(monkeypatch):
    with pytest.raises(e.EvaluationError) as err:
        e.evaluate(
            repo_id="fixture",
            run_binding=BINDING,
            subject="effect",
            requested_basis=BASIS,
            current_basis=None,
            items=[item()],
            expected_tail=None,
            as_of=AT,
            current_input_digests={},
            observed_at=AT,
        )
    assert err.value.code == "evidence-tail-mismatch"
    monkeypatch.setattr(e, "MAX_CHAIN_ITEMS", 0)
    with pytest.raises(e.EvaluationError) as err:
        e.chain_tail([item()])
    assert err.value.code == "evidence-snapshot-too-large"


def test_snapshot_byte_bound_forces_failure(monkeypatch):
    monkeypatch.setattr(e, "MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(e.EvaluationError) as err:
        evaluate([item()])
    assert err.value.code == "evidence-snapshot-too-large"


def test_repeated_fixed_clock_evaluation_is_exact():
    assert evaluate([item()]) == evaluate([item()])
