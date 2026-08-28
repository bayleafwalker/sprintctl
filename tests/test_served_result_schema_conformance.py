"""Every served read must satisfy the result schema it publishes.

``test_served_operation_surface`` pins which operations exist; its own
docstring admits it "cannot catch a schema-shape change inside one
operation".  That gap is not hypothetical.  On 2026-08-28 every served
``next-work`` call in every repository failed with ``adapter-result-invalid``
because the handler had gained three advisory dispatch fields that its
result schema still forbade -- the project variant's contract was updated in
the same change and this one was not.  Nothing caught it, because the only
check that validates a real result against a real schema lives in the Vuoro
integration module that skips itself unless ``vuoro_service`` is installed.

This module closes the class, not the instance: it invokes the argument-free
served reads against a real sqlite store and validates each result against
the operation's own published result schema.  A handler that grows a field,
or a contract that forbids one, fails here on an ordinary checkout.

The validator covers the Draft 2020-12 subset the catalog actually uses.
Adding a schema keyword the catalog needs means teaching ``_validate`` about
it -- never loosening it to make a red test pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sprintctl import db
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from sprintctl.work_application import WorkApplication


# Reads that need no arguments beyond an optional sprint_id, so they can be
# invoked against a seeded store without inventing identifiers.
ARGUMENT_FREE_READS = (
    "work.read.next-work",
    "work.read.next-work-explain",
    "work.read.context",
    "work.read.sprints",
    "work.read.items",
    "work.read.reservations",
    "work.identity.current",
)

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _matches_type(value, expected) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        python_type = _TYPES[name]
        if name == "integer" and isinstance(value, bool):
            continue
        if name in {"number", "integer"} and isinstance(value, bool):
            continue
        if isinstance(value, python_type):
            return True
    return False


def _validate(value, schema, path: str, failures: list[str]) -> None:
    if "const" in schema and value != schema["const"]:
        failures.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
        return
    if "enum" in schema and value not in schema["enum"]:
        failures.append(f"{path}: {value!r} not in {schema['enum']!r}")
        return
    if "type" in schema and not _matches_type(value, schema["type"]):
        failures.append(f"{path}: expected type {schema['type']!r}, got {type(value).__name__}")
        return
    if isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                failures.append(f"{path}.{name}: required but missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    failures.append(f"{path}.{name}: not permitted by the result schema")
        for name, subschema in properties.items():
            if name in value:
                _validate(value[name], subschema, f"{path}.{name}", failures)
    elif isinstance(value, list) and "items" in schema:
        for index, entry in enumerate(value):
            _validate(entry, schema["items"], f"{path}[{index}]", failures)


def _context():
    identity = SimpleNamespace(
        actor="conformance-test",
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


@pytest.fixture
def application(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "conformance")
    db.create_work_item(conn, active_sprint["id"], track, "Ready item")
    return WorkApplication(
        repo_id="test-repo",
        store=conn,
        backend=db,
        ingest_records=lambda records: [],
        arbitrate_command=lambda record, authenticated_actor=None: None,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )


@pytest.mark.parametrize("operation", ARGUMENT_FREE_READS)
def test_served_read_result_satisfies_its_published_schema(application, operation):
    contract = next(
        item for item in WORK_OPERATION_CONTRACTS if item.name == operation
    )
    result = application.invoke(operation, {}, _context())

    failures: list[str] = []
    _validate(result, contract.result_schema, operation, failures)

    assert not failures, "\n".join(failures)


def test_validator_rejects_an_undeclared_field():
    # Guards the guard: the 2026-08-28 failure was an extra key, so the
    # validator must reject one even when every declared field is present.
    schema = {
        "type": "object",
        "required": ["declared"],
        "properties": {"declared": {"type": "string"}},
        "additionalProperties": False,
    }
    failures: list[str] = []
    _validate({"declared": "ok", "undeclared": 1}, schema, "sample", failures)
    assert failures == ["sample.undeclared: not permitted by the result schema"]
