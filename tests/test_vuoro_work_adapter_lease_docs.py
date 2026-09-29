"""Documentation contract for the work-lease adapter (agentops#2528, #2529).

docs/reference/vuoro-work-adapter.md is the published reference for the
``work.lease.*`` operations. These checks pin the refusal order for
``work-awaiting-verification``, the lease ending on a release to pending
(``end_reason=item-released-<reason>``) and the heartbeat's repo lock.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "reference" / "vuoro-work-adapter.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _bullet(operation: str) -> str:
    """The top-level list item documenting ``operation`` (up to the next
    top-level bullet or blank-line-separated paragraph)."""
    text = _text()
    start = text.find(f"- `{operation}`")
    assert start != -1, f"no bullet for {operation} in {DOC.name}"
    rest = text[start + 2:]
    match = re.search(r"\n- `|\n\n(?!\s)", rest)
    return rest[: match.start()] if match else rest


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_acquire_documents_work_awaiting_verification_before_lease_held():
    acquire = _flat(_bullet("work.lease.acquire-v1"))
    assert "work-awaiting-verification" in acquire
    assert "lease-held" in acquire
    assert "in that order" in acquire
    assert acquire.index("work-awaiting-verification") < acquire.index("`lease-held`")


def test_release_to_pending_ending_the_lease_is_documented():
    paragraphs = [_flat(p) for p in re.split(r"\n\s*\n|\n- ", _text())]
    matching = [p for p in paragraphs if "item-released-" in p]
    assert matching, "docs must name end_reason item-released-<reason>"
    assert any(
        "pending" in p and "released" in p.replace("item-released-", "") for p in matching
    ), "the item-released-<reason> text must say a release to pending ends the lease (state released)"


def test_heartbeat_documents_the_repo_lock():
    heartbeat = _flat(_bullet("work.lease.heartbeat-v1")).lower()
    sentences = re.split(r"(?<=[.;])\s", heartbeat)
    assert any(re.search(r"\brepo", s) and re.search(r"\block\b", s) for s in sentences), (
        "the heartbeat bullet must say the heartbeat takes the repo (claims) lock"
    )


def test_parked_disposition_is_documented_as_work_level_not_lease_state():
    text = _flat(_text())
    acquire = _flat(_bullet("work.lease.acquire-v1"))
    report = _flat(_bullet("work.lease.report-outcome-v1"))
    assert "work-parked" in acquire and "work-parked" in report
    for needed in ('disposition: "parked"', "reason_ref", "work.parked", "reported-parked"):
        assert needed in report, f"the report-outcome bullet must document {needed}"
    assert "released to pending" in report, "the report bullet must say what lifts a parking"
    assert "There is no `parked` lease state" in text
