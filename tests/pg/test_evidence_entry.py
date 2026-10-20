"""Real owner invocation: older entries, full binding and unchanged history."""
from dataclasses import replace

import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import PG_MARKS
from tests.pg.test_run_evidence import _append_args, _context, _new_run

pytestmark = PG_MARKS
OPERATION = "work.evidence.entry-v1"


def invoke(app, run, sequence, context=None):
    return app.invoke(OPERATION, {"run_id": run, "chain_seq": sequence}, context or _context())


def snapshot(store):
    with store.conn.cursor() as cur:
        return {
            table: _rows(cur, table, store.repo_id)
            for table in ("run", "evidence_item", "work_idempotency_ledger", "event",
                          "work_lease", "work_outcome_report", "work_item")
        }


def _rows(cursor, table, repo):
    # Table names are the fixed test-owned list above, never caller input.
    cursor.execute(f"SELECT to_jsonb(t) AS value FROM {table} t WHERE repo_id = %s ORDER BY to_jsonb(t)::text", (repo,))
    return cursor.fetchall()


def test_older_entry_resolves_after_later_append_without_changing_owner_records(store):
    app = WorkApplication.postgres(store)
    run = _new_run(app, "entry-older")
    first = app.invoke("work.evidence.append-v1", _append_args(run, "entry-first", item_id="entry-first"), _context())["item"]
    second = app.invoke("work.evidence.append-v1", _append_args(run, "entry-second", item_id="entry-second", seq=1,
                        prev=pg.evidence_entry_digest(first)), _context())["item"]
    before = snapshot(store)
    assert invoke(app, run, 0)["item"] == first
    assert invoke(app, run, 1)["item"] == second
    assert invoke(app, run, 2)["item"] is None
    assert snapshot(store) == before


@pytest.mark.parametrize("changed", [
    {"principal_id": "other"}, {"workspace_id": "other"},
    {"client_id": "other", "grant_id": "grant-a"},
    {"client_id": "client-a", "grant_id": "other"},
])
def test_foreign_full_binding_is_indistinguishable_from_unknown_run(store, changed):
    app = WorkApplication.postgres(store)
    owner = _context(client_id="client-a", grant_id="grant-a")
    run = _new_run(app, "entry-binding-" + next(iter(changed)), context=owner)
    before = snapshot(store)
    foreign = _context(**({"client_id": "client-a", "grant_id": "grant-a"} | changed))
    failures = []
    for target in (run, "run_" + "0" * 26):
        with pytest.raises(ApplicationRejection) as caught:
            invoke(app, target, 0, foreign)
        # The refusal echoes the supplied ID, which is already caller-known.
        # Compare the observable refusal after normalizing that input.
        failures.append((caught.value.code, caught.value.http_status,
                         str(caught.value).replace(target, "<requested-run>")))
    assert failures[0] == failures[1]
    assert failures[0][:2] == ("run-not-found", 404)
    assert snapshot(store) == before


def test_repository_scope_prevents_an_existing_run_from_being_probed(store):
    app = WorkApplication.postgres(store)
    run = _new_run(app, "entry-repository")
    other = WorkApplication.postgres(replace(store, repo_id=store.repo_id + "-other"))
    with pytest.raises(ApplicationRejection) as caught:
        invoke(other, run, 0)
    assert (caught.value.code, caught.value.http_status) == ("run-not-found", 404)


def test_direct_invocation_does_not_bypass_authority_or_argument_validation(store):
    app = WorkApplication.postgres(store)
    run = _new_run(app, "entry-inputs")
    before = snapshot(store)
    context = _context()
    context.identity.authorities = frozenset()
    with pytest.raises(ApplicationRejection) as caught:
        invoke(app, run, 0, context)
    assert caught.value.code == "authority-required"
    for arguments in ({"run_id": run, "chain_seq": True},
                      {"run_id": 42, "chain_seq": 0},
                      {"run_id": run, "chain_seq": 9007199254740992},
                      {"run_id": run, "chain_seq": 0, "principal_id": "foreign"}):
        with pytest.raises(ApplicationRejection) as caught:
            app.invoke(OPERATION, arguments, _context())
        assert caught.value.code == "invalid-arguments"
    assert snapshot(store) == before
    context = _context()
    context.idempotency_key = "not-allowed"
    with pytest.raises(ApplicationRejection) as caught:
        invoke(app, run, 0, context)
    assert caught.value.code == "invalid-arguments"
