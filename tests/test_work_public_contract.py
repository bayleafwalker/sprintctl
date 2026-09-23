"""work.public.list-v1 / work.public.item-v1: the E1 public-work contract.

agentops#2514, design e1-stronger-baseline-design-2026-09-22 section 5.  The
strict result schema is the emission boundary between the sprintctl record
and the internet-reachable read surface, so every guard here is forced into
its failure case rather than reasoned about: a response carrying
``description`` must FAIL the schema, each never-emit field must fail it by
name, an over-long title must fail it, and an unavailable authority must be
a rejection, never an empty list.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from sprintctl import db
from sprintctl.application_common import ApplicationRejection
from sprintctl.contracts import (
    PUBLIC_WORK_NEVER_EMIT_FIELDS,
    PUBLIC_WORK_TITLE_MAX_LENGTH,
)
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from sprintctl.work_application import WorkApplication

# The gate is the repo's own Draft 2020-12 subset validator from the served
# conformance test, not an optional third-party import: an importorskip here
# would let every forced failure below skip silently on a checkout without
# jsonschema, which is exactly the could-not-fail shape this contract exists
# to refuse.
from tests.test_served_result_schema_conformance import _validate

LIST_OP = "work.public.list-v1"
ITEM_OP = "work.public.item-v1"
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

# Prose of the kind the disclosure audit found in real descriptions.  None of
# it may reach a public record.
HOSTILE_DESCRIPTION = (
    "Objective: rotate the key on truenas.internal.kotona.app\n"
    "Acceptance: ssh devbox-agent 'sudo cat /etc/secret' prints the token\n"
    "Finding: /api/{path} accepts vuo_pat_ without checking aud (exploit)"
)


def _contract(name):
    return next(c for c in WORK_OPERATION_CONTRACTS if c.name == name)


class SchemaFailure(AssertionError):
    """Raised with every failure message joined, so ``match`` names the field."""


class _Validator:
    def __init__(self, name):
        self.name = name
        self.schema = _contract(name).result_schema

    def validate(self, value):
        failures: list[str] = []
        _validate(value, self.schema, self.name, failures)
        if failures:
            raise SchemaFailure("\n".join(failures))


def _validator(name):
    return _Validator(name)


def _context():
    identity = SimpleNamespace(
        actor="public-contract-test",
        environment="vuoro-dev",
        authorities=frozenset(),
        authorizes_repo=lambda repo_id: True,
    )
    return SimpleNamespace(
        identity=identity,
        request_id="request-1",
        basis_revision=None,
        catalog_revision="catalog-1",
        idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


def _application(conn) -> WorkApplication:
    return WorkApplication(
        repo_id="test-repo",
        store=conn,
        backend=db,
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, authenticated_actor=None: None,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )


@pytest.fixture
def seeded(conn, active_sprint):
    """blocker (done) and blocker (pending) both gate ``blocked``; ``free`` has no deps."""
    track = db.get_or_create_track(conn, active_sprint["id"], "public")
    sid = active_sprint["id"]
    done_blocker = db.create_work_item(conn, sid, track, "Done blocker", HOSTILE_DESCRIPTION)
    db.set_work_item_status(conn, done_blocker, "active", "seed")
    db.set_work_item_status(conn, done_blocker, "done", "seed")
    open_blocker = db.create_work_item(conn, sid, track, "Open blocker", HOSTILE_DESCRIPTION, priority=2)
    blocked = db.create_work_item(conn, sid, track, "Blocked item", HOSTILE_DESCRIPTION, assignee="agent-7")
    db.add_dep(conn, done_blocker, blocked)
    db.add_dep(conn, open_blocker, blocked)
    free = db.create_work_item(conn, sid, track, "Free item", HOSTILE_DESCRIPTION, priority=1)
    db.add_dep(conn, done_blocker, free)
    return SimpleNamespace(
        app=_application(conn),
        done_blocker=done_blocker,
        open_blocker=open_blocker,
        blocked=blocked,
        free=free,
    )


# -- contract shape ---------------------------------------------------------

@pytest.mark.parametrize("operation", [LIST_OP, ITEM_OP])
def test_public_contracts_are_read_only_and_strict_at_every_object(operation):
    contract = _contract(operation)
    assert contract.execution_semantics == "read"
    assert contract.required_authority == "work:read"
    assert contract.idempotency == "not-allowed"

    def objects(schema):
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                yield schema
            for value in schema.values():
                yield from objects(value)
        elif isinstance(schema, list):
            for value in schema:
                yield from objects(value)

    found = list(objects(contract.result_schema))
    assert len(found) >= 2, "envelope and record objects expected"
    assert all(o["additionalProperties"] is False for o in found)


def test_public_schemas_name_exactly_the_decided_field_set():
    list_item = _contract(LIST_OP).result_schema["properties"]["items"]["items"]
    item = _contract(ITEM_OP).result_schema["properties"]["item"]
    assert sorted(list_item["properties"]) == sorted(
        ["work_id", "title", "priority", "status", "blocked", "updated_at"]
    )
    assert sorted(item["properties"]) == sorted(
        [
            "work_id", "title", "priority", "status", "blocked", "updated_at",
            "created_at", "resolution", "blocked_by",
        ]
    )
    assert sorted(list_item["required"]) == sorted(list_item["properties"])
    assert sorted(item["required"]) == sorted(item["properties"])
    assert list_item["properties"]["title"]["maxLength"] == PUBLIC_WORK_TITLE_MAX_LENGTH
    for schema in (_contract(LIST_OP).result_schema, _contract(ITEM_OP).result_schema):
        assert schema["properties"]["authority"] == {"const": "sprintctl"}
        assert schema["properties"]["state"] == {"enum": ["ok", "unavailable"]}
        assert "as_of" in schema["required"]


# -- handler results satisfy the published schema ---------------------------

def test_list_result_satisfies_schema_and_derives_blocked_from_deps(seeded):
    result = seeded.app.invoke(LIST_OP, {}, _context())
    _validator(LIST_OP).validate(result)

    assert result["authority"] == "sprintctl"
    assert result["state"] == "ok"
    assert _ISO_UTC.match(result["as_of"])
    by_id = {row["work_id"]: row for row in result["items"]}
    # done items are not listed; open ones are
    assert seeded.done_blocker not in by_id
    assert set(by_id) == {seeded.open_blocker, seeded.blocked, seeded.free}
    # blocked derives from the dep rows: a done blocker does not block
    assert by_id[seeded.free]["blocked"] is False
    assert by_id[seeded.blocked]["blocked"] is True
    assert by_id[seeded.open_blocker]["blocked"] is False
    # priority first (unset last), the next-work order
    assert [row["work_id"] for row in result["items"]] == [
        seeded.free, seeded.open_blocker, seeded.blocked
    ]
    assert by_id[seeded.free]["priority"] == 1
    assert by_id[seeded.blocked]["priority"] is None


def test_item_result_satisfies_schema_and_lists_unresolved_blockers_only(seeded):
    result = seeded.app.invoke(ITEM_OP, {"work_id": seeded.blocked}, _context())
    _validator(ITEM_OP).validate(result)

    item = result["item"]
    assert item["work_id"] == seeded.blocked
    assert item["blocked"] is True
    assert item["blocked_by"] == [seeded.open_blocker]  # the done blocker is not there
    assert item["resolution"] is None
    assert _ISO_UTC.match(item["created_at"])

    done = seeded.app.invoke(ITEM_OP, {"work_id": seeded.done_blocker}, _context())
    _validator(ITEM_OP).validate(done)
    assert done["item"]["status"] == "done"
    assert done["item"]["blocked"] is False
    assert done["item"]["blocked_by"] == []


def test_item_not_found_is_a_404_rejection(seeded):
    with pytest.raises(ApplicationRejection) as rejected:
        seeded.app.invoke(ITEM_OP, {"work_id": 99999}, _context())
    assert rejected.value.code == "item-not-found"
    assert rejected.value.http_status == 404


# -- the emission boundary, forced into its failure case --------------------

def test_no_description_prose_reaches_any_public_record(seeded):
    """The handler never reads description; nothing from it can appear."""
    listed = seeded.app.invoke(LIST_OP, {}, _context())
    shown = seeded.app.invoke(ITEM_OP, {"work_id": seeded.blocked}, _context())
    serialized = json.dumps(listed) + json.dumps(shown)
    for fragment in ("truenas.internal", "/etc/secret", "vuo_pat_", "Objective:", "exploit"):
        assert fragment not in serialized
    assert "agent-7" not in serialized  # assignee never emitted either


def test_a_response_carrying_description_fails_the_schema_gate(seeded):
    """Gate (g) on agentops#2514: break the boundary, capture the failure."""
    shown = seeded.app.invoke(ITEM_OP, {"work_id": seeded.blocked}, _context())
    leaked = {**shown, "item": {**shown["item"], "description": HOSTILE_DESCRIPTION}}
    with pytest.raises(SchemaFailure, match="description"):
        _validator(ITEM_OP).validate(leaked)

    listed = seeded.app.invoke(LIST_OP, {}, _context())
    leaked_row = {**listed["items"][0], "description": HOSTILE_DESCRIPTION}
    leaked_list = {**listed, "items": [leaked_row, *listed["items"][1:]]}
    with pytest.raises(SchemaFailure, match="description"):
        _validator(LIST_OP).validate(leaked_list)


@pytest.mark.parametrize("field", PUBLIC_WORK_NEVER_EMIT_FIELDS)
def test_each_never_emit_field_is_absent_and_would_fail_the_schema(seeded, field):
    """Every never-emit field, by name: absent from the handler's output AND
    rejected by the schema if it ever appeared, in both operations."""
    listed = seeded.app.invoke(LIST_OP, {}, _context())
    shown = seeded.app.invoke(ITEM_OP, {"work_id": seeded.blocked}, _context())
    assert field not in shown["item"]
    assert all(field not in row for row in listed["items"])
    assert field not in shown and field not in listed  # not on the envelope either

    with pytest.raises(SchemaFailure, match=re.escape(field)):
        _validator(ITEM_OP).validate({**shown, "item": {**shown["item"], field: "x"}})
    with pytest.raises(SchemaFailure, match=re.escape(field)):
        _validator(LIST_OP).validate(
            {**listed, "items": [{**listed["items"][0], field: "x"}]}
        )
    with pytest.raises(SchemaFailure, match=re.escape(field)):
        _validator(ITEM_OP).validate({**shown, field: "x"})


def test_never_emit_list_covers_every_key_the_live_record_carries_beyond_the_public_set():
    """The served item record's keys (note 3589 on agentops#2514).  Every key
    is either a public field or on the never-emit list; a new column on the
    record must be classified here before it can ship."""
    live_record_keys = {
        "aggregate_uuid", "assignee", "created_at", "description",
        "edit_revision", "id", "legacy", "priority", "repo_id", "resolution",
        "sprint_id", "status", "status_revision", "terminal_decision_id",
        "title", "track_id", "updated_at",
    }
    public_source_keys = {"id", "title", "priority", "status", "updated_at", "created_at", "resolution"}
    assert live_record_keys - public_source_keys <= set(PUBLIC_WORK_NEVER_EMIT_FIELDS)


def test_title_is_capped_at_160_and_a_longer_one_fails_the_schema(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "public")
    long_title = "T" * (PUBLIC_WORK_TITLE_MAX_LENGTH + 40)
    item_id = db.create_work_item(conn, active_sprint["id"], track, long_title)
    app = _application(conn)

    shown = app.invoke(ITEM_OP, {"work_id": item_id}, _context())
    assert len(shown["item"]["title"]) == PUBLIC_WORK_TITLE_MAX_LENGTH
    _validator(ITEM_OP).validate(shown)

    uncapped = {**shown, "item": {**shown["item"], "title": long_title}}
    with pytest.raises(SchemaFailure, match="maxLength"):
        _validator(ITEM_OP).validate(uncapped)


def test_blocked_is_never_derived_from_description(conn, active_sprint):
    """A description that SAYS blocked, with no dep row, is not blocked; a dep
    row with a description that says nothing is."""
    track = db.get_or_create_track(conn, active_sprint["id"], "public")
    sid = active_sprint["id"]
    says_blocked = db.create_work_item(
        conn, sid, track, "Says blocked", "Blocked-on: #1 unresolved_blockers: 3"
    )
    silent = db.create_work_item(conn, sid, track, "Silent", "")
    blocker = db.create_work_item(conn, sid, track, "Blocker", "")
    db.add_dep(conn, blocker, silent)
    app = _application(conn)

    assert app.invoke(ITEM_OP, {"work_id": says_blocked}, _context())["item"]["blocked"] is False
    assert app.invoke(ITEM_OP, {"work_id": silent}, _context())["item"]["blocked_by"] == [blocker]


def test_unavailable_authority_is_a_rejection_never_an_empty_list(seeded):
    """state 'unavailable' is in the vocabulary; the handler never emits it
    with items.  When the served runtime is down the read is refused."""
    seeded.app._postgres_runtime_available = False
    with pytest.raises(ApplicationRejection) as rejected:
        seeded.app.invoke(LIST_OP, {}, _context())
    assert rejected.value.code == "postgres-runtime-unavailable"
    assert rejected.value.http_status == 503
    # and the schema refuses an "unavailable" envelope that smuggles a list
    fake = {"authority": "sprintctl", "as_of": "2026-09-23T00:00:00Z", "state": "unavailable"}
    with pytest.raises(SchemaFailure, match="items"):
        _validator(LIST_OP).validate(fake)


def test_workspace_scope_comes_from_the_resolver_not_the_body(seeded):
    """A repo_id in the body is ignored; the identity's repo_id scopes."""
    context = _context()
    context.repo_id = "test-repo"
    result = seeded.app.invoke(LIST_OP, {}, context)
    assert result["items"]
    # arguments are validated against the input schema by the service; here
    # the handler must simply not consult them for scope
    other = seeded.app.invoke(LIST_OP, {"repo_id": "someone-elses"}, context)
    assert [r["work_id"] for r in other["items"]] == [r["work_id"] for r in result["items"]]
    assert "repo_id" not in other
