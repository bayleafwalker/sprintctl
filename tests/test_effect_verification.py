import pytest

from sprintctl import effect_verification
from sprintctl.effect_intent import canonical_intent_digest


def _fixture(checks):
    intent = {
        "intent_id": "intent_00000000000000000000000000",
        "revision": 1,
        "item_id": 7,
        "repository": "owner/project",
        "base_commit": "a" * 40,
        "title": "change",
        "rationale": "verify exact bytes",
        "unified_diff": "diff --git a/a b/a\n",
        "release_digest": "b" * 64,
    }
    intent["canonical_intent_digest"] = canonical_intent_digest(intent)
    detail = {
        "schema": effect_verification.SCHEMA,
        "intent_id": intent["intent_id"],
        "intent_revision": intent["revision"],
        "canonical_intent_digest": intent["canonical_intent_digest"],
        "release_digest": intent["release_digest"],
        "artifact": {
            "domain": effect_verification.ARTIFACT_DOMAIN,
            "digest": effect_verification.artifact_digest(intent["unified_diff"]),
        },
        "checks": checks,
    }
    item = {
        "kind": effect_verification.KIND,
        "digest": effect_verification.receipt_digest(detail),
        "claims": [{"claim_type": "observation", "subject": intent["intent_id"],
                    "grant_id": None, "confirms": None, "detail": detail}],
    }
    return item, intent


def _check(name="unit", revision="sha256:" + "a" * 64, status="passed"):
    return {"name": name, "revision": revision, "status": status}


def test_legacy_receipt_validation_keeps_existing_behavior():
    item, intent = _fixture([_check()])
    assert effect_verification.validate_receipt(item, intent, intent["release_digest"])["checks"] == [_check()]


def test_required_check_revision_must_match_the_same_named_passing_check():
    item, intent = _fixture([_check(), _check("integration", "sha256:" + "b" * 64)])
    detail = effect_verification.validate_receipt(
        item, intent, intent["release_digest"],
        {"unit": "sha256:" + "a" * 64, "integration": "sha256:" + "b" * 64},
    )
    assert len(detail["checks"]) == 2
    for checks in (
        [_check("unit", "sha256:" + "c" * 64)],
        [_check("unit", status="failed")],
        [_check("other")],
    ):
        bad_item, bad_intent = _fixture(checks)
        with pytest.raises(ValueError):
            effect_verification.validate_receipt(
                bad_item, bad_intent, bad_intent["release_digest"],
                {"unit": "sha256:" + "a" * 64},
            )


def test_effect_revision_requirement_map_is_defensive():
    item, intent = _fixture([_check()])
    for invalid in ({}, {"unit": "a" * 64}, {"": "sha256:" + "a" * 64}):
        with pytest.raises(ValueError, match="frozen effect check revision"):
            effect_verification.validate_receipt(item, intent, intent["release_digest"], invalid)
