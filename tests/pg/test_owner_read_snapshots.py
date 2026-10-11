"""PostgreSQL owner reads are atomic, bounded and never grant permission."""
from types import SimpleNamespace
import uuid

import pytest

from sprintctl import effect_verification, pg
from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS, _PG_URL, dict_row, psycopg
from tests.pg.test_effect_intent import PROPOSER, ACCEPTOR, _context, _app, _run, _items, _propose

pytestmark = PG_MARKS

CHECKER = _context(ACCEPTOR.identity.principal_id,
                   set(ACCEPTOR.identity.authorities) | {"work:evidence"})


def _owned_context(base, *, grant_id=None):
    identity = SimpleNamespace(**vars(base.identity))
    identity.grant_id = grant_id
    return SimpleNamespace(**{**vars(base), "identity": identity})


@pytest.fixture(autouse=True)
def snapshot_connection_factory(store):
    store.connection_factory = lambda: psycopg.connect(_PG_URL, row_factory=dict_row)


def _acceptance_snapshot(store, intent, *, ref=None, context=CHECKER):
    args = {
        "intent_id": intent["intent_id"], "revision": intent["revision"],
        "canonical_intent_digest": intent["canonical_intent_digest"],
        "artifact_sha256": effect_verification.artifact_digest(intent["unified_diff"]),
    }
    if ref is not None:
        args["verification_ref"] = ref
    return _app(store).invoke("work.effect.acceptance-snapshot-v1", args, context)["snapshot"]


def _continuation(app, run_id, item_id, context, **kwargs):
    return app.invoke("work.run.continuation-delta-v1", {
        "run_id": run_id, "item_id": item_id, **kwargs,
    }, context)["delta"]


def test_acceptance_snapshot_is_owner_scoped_atomic_and_never_authorizes(store):
    item_id = _items(store)[0]
    release = pg.reserve(store, item_id, actor="operator", session_id=uuid.uuid4().hex,
        role="execution", acceptance_contract={"effect_check_revisions": {
            "patch-check": "sha256:" + "d" * 64}})
    intent = _propose(store, item_id, _run(store, PROPOSER))
    snapshot = _acceptance_snapshot(store, intent)
    assert snapshot["schema"] == "sprintctl-effect-acceptance-snapshot/v1"
    assert snapshot["authorizes_acceptance"] is False
    assert snapshot["requested_basis_matches"] is True
    assert snapshot["artifact_digest_matches"] is True
    assert snapshot["current_item"]["item_id"] == item_id
    assert snapshot["current_release"]["release_digest"] == release["release_digest"]
    assert snapshot["current_release"]["effect_verification_required"] is True
    assert snapshot["current_release"]["revision_currentness"] == "unknown"
    assert snapshot["receipt"]["status"] == "not-supplied"
    assert snapshot["lease_settlement"] == "unknown"
    assert "unified_diff" not in snapshot["intent"]


def test_acceptance_snapshot_requires_both_effect_and_work_read_authorities(store):
    item_id = _items(store)[0]
    pg.reserve(store, item_id, actor="operator", session_id=uuid.uuid4().hex, role="execution")
    intent = _propose(store, item_id, _run(store, PROPOSER))
    no_effect_get = _context(CHECKER.identity.principal_id, {"work:read"})
    with pytest.raises(ApplicationRejection) as denied:
        _acceptance_snapshot(store, intent, context=no_effect_get)
    assert denied.value.code == "authority-required"
    no_work_read = _context(CHECKER.identity.principal_id, {"work.effect.get"})
    with pytest.raises(ApplicationRejection) as denied:
        _acceptance_snapshot(store, intent, context=no_work_read)
    assert denied.value.code == "authority-required"


def test_acceptance_snapshot_uses_shared_receipt_predicate_and_caller_run_binding(store):
    from tests.pg.test_protected_artifact_acceptance import receipt

    item_id = _items(store)[0]
    expected = "sha256:" + "d" * 64
    reservation = pg.reserve(store, item_id, actor="operator", session_id=uuid.uuid4().hex,
        role="execution", acceptance_contract={"effect_check_revisions": {"patch-check": expected}})
    release = pg.get_release(store, reservation["release_digest"])
    intent = _propose(store, item_id, _run(store, PROPOSER))
    ref, _, _ = receipt(store, intent, release)
    matching = _acceptance_snapshot(store, intent, ref=ref)
    assert matching["receipt"]["comparison"] == "matches"
    assert matching["receipt"]["check_revision_comparison"] == "matches"
    foreign = _acceptance_snapshot(store, intent, ref=ref,
                                   context=_owned_context(CHECKER, grant_id="different-grant"))
    assert foreign["receipt"]["status"] == "not-found"
    assert foreign["receipt"]["comparison"] == "unknown"
    without_evidence = _context(CHECKER.identity.principal_id, {"work:read", "work.effect.get"})
    hidden = _acceptance_snapshot(store, intent, ref=ref, context=without_evidence)
    assert hidden["receipt"]["status"] == "unknown-no-evidence-authority"
    assert hidden["receipt"]["evidence_digest"] is None
    assert hidden["receipt"]["comparison"] == "unknown"


def test_continuation_requires_current_owned_run_and_explicit_item(store):
    app = _app(store)
    item_id = _items(store)[0]
    run_id = _run(store, PROPOSER)
    delta = _continuation(app, run_id, item_id, PROPOSER)
    assert delta["authorizes_action"] is False
    assert delta["authorization_scope"] == {"repo_id": store.repo_id, "run_id": run_id, "run_owned": True}
    assert set(delta["comparison"].values()) == {"unknown"}
    assert delta["source_bytes"] == delta["external_missing_content"] == "unknown"
    foreign = _context("another-principal", set(PROPOSER.identity.authorities))
    with pytest.raises(ApplicationRejection) as denied:
        _continuation(app, run_id, item_id, foreign)
    assert denied.value.code == "run-not-found"
    with pytest.raises(ApplicationRejection) as denied:
        app.invoke("work.run.continuation-delta-v1", {"run_id": run_id}, PROPOSER)
    assert denied.value.code == "invalid-arguments"


def test_continuation_compares_checkpoint_and_refuses_stale_keyset_cursor(store):
    app = _app(store)
    item_id = _items(store)[0]
    run_id = _run(store, PROPOSER)
    pg.add_ref(store, item_id, "issue", "https://example.test/1", "one")
    pg.add_ref(store, item_id, "issue", "https://example.test/2", "two")
    initial = _continuation(app, run_id, item_id, PROPOSER)
    checkpoint = initial["current"]
    first_page = _continuation(app, run_id, item_id, PROPOSER, limit=1, checkpoint=checkpoint)
    cursor = first_page["next_reference_cursor"]
    assert cursor is not None
    second = _continuation(app, run_id, item_id, PROPOSER, limit=1,
                           checkpoint=checkpoint, cursor=cursor)
    assert second["reference_page_complete"] is True
    assert second["comparison"]["current_reference_set_digest"] == "unchanged"
    pg.add_ref(store, item_id, "issue", "https://example.test/3", "three")
    with pytest.raises(ApplicationRejection) as stale:
        _continuation(app, run_id, item_id, PROPOSER, limit=1,
                      checkpoint=checkpoint, cursor=cursor)
    assert stale.value.code == "stale-cursor"


def test_continuation_overflow_keeps_large_population_digest_unknown(store):
    app = _app(store)
    item_id = _items(store)[0]
    run_id = _run(store, PROPOSER)
    for index in range(65):
        pg.add_ref(store, item_id, "issue", f"https://example.test/{index}", str(index))
    first = _continuation(app, run_id, item_id, PROPOSER, limit=64)
    assert first["current"]["current_reference_set_digest"] is None
    assert first["comparison"]["current_reference_set_digest"] == "unknown"
    assert first["reference_page_complete"] is False
    assert first["next_reference_cursor"] is not None
    second = _continuation(app, run_id, item_id, PROPOSER, limit=64,
                           cursor=first["next_reference_cursor"])
    assert len(second["reference_page"]) == 1
    assert second["reference_page_complete"] is True


def test_continuation_schema_is_read_only_and_bounded():
    contract = next(c for c in WORK_OPERATION_CONTRACTS
                    if c.name == "work.run.continuation-delta-v1")
    assert contract.required_authority == "work:read"
    assert contract.execution_semantics == "read"
    assert contract.input_schema["properties"]["limit"]["maximum"] == 64
