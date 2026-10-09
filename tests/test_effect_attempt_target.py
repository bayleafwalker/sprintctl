"""Discriminate target substitution and wire authority injection."""
import copy

import pytest

from sprintctl.effect_attempt import canonical_target, canonical_target_digest

INTENT = {
    "intent_id": "intent_" + "1" * 26,
    "repository": "fixture/repository",
    "base_commit": "a" * 40,
    "title": "Prepared change",
    "rationale": "Owner-reviewed rationale",
}
PUSH = {"operation": "push_branch", "branch": "custom/nested/" + INTENT["intent_id"],
        "commit_sha": "b" * 40}
PR = {**PUSH, "operation": "open_pull_request", "base_branch": "main"}


def test_configurable_prefix_and_exact_branch_spelling_are_preserved():
    target = canonical_target(INTENT, PUSH)
    assert target["branch"] == PUSH["branch"]
    assert target["repository"] == INTENT["repository"]
    assert target["base_commit"] == INTENT["base_commit"]
    assert canonical_target_digest(target) != canonical_target_digest(canonical_target(INTENT, PR))


@pytest.mark.parametrize("field", ["repository", "base_commit", "title", "rationale"])
def test_every_stored_intent_target_component_changes_binding(field):
    changed = {**INTENT, field: INTENT[field] + "x"}
    assert canonical_target_digest(canonical_target(changed, PR)) != canonical_target_digest(
        canonical_target(INTENT, PR))


@pytest.mark.parametrize("change", [{"commit_sha": "c" * 40}, {"base_branch": "release"},
                                   {"branch": "other/" + INTENT["intent_id"]}])
def test_declared_provider_target_substitution_changes_digest(change):
    assert canonical_target_digest(canonical_target(INTENT, {**PR, **change})) != (
        canonical_target_digest(canonical_target(INTENT, PR)))


@pytest.mark.parametrize("field", ["repository", "base_commit", "title_sha256", "body_sha256",
                                   "trusted", "principal_id", "grant_id", "dispatch_authorized"])
def test_wire_cannot_override_owner_content_or_inject_authority(field):
    with pytest.raises(ValueError):
        canonical_target(INTENT, {**PUSH, field: "forged"})


@pytest.mark.parametrize("branch", ["-option", "../escape", "refs//duplicate", "bad.lock/x",
    "bad@{selector", "bad space/x", "bad\\escape/x", "bad\0/x", "bad\ud800/x",
    "other/intent_" + "2" * 26, INTENT["intent_id"], "/" + INTENT["intent_id"]])
def test_invalid_or_wrong_intent_branch_is_refused(branch):
    with pytest.raises(ValueError):
        canonical_target(INTENT, {**PUSH, "branch": branch})


@pytest.mark.parametrize("value", [None, [], {}, {**PUSH, "operation": "merge"},
    {**PUSH, "operation": []}, {**PUSH, "operation": {}}, {**PUSH, "operation": True},
    {**PUSH, "base_branch": "main"}, {k: v for k, v in PR.items() if k != "base_branch"},
    {**PUSH, "commit_sha": "B" * 40}, {**PUSH, "commit_sha": True},
    {**PR, "base_branch": PR["branch"]}])
def test_closed_operation_union_and_git_commit_are_enforced(value):
    with pytest.raises(ValueError):
        canonical_target(INTENT, value)


def test_digest_is_order_independent_and_inputs_remain_unchanged():
    before = copy.deepcopy(PR)
    target = canonical_target(INTENT, PR)
    assert PR == before
    assert canonical_target_digest(target) == canonical_target_digest(dict(reversed(list(target.items()))))
