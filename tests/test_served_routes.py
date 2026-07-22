from sprintctl.served_routes import SERVED_COMMAND_ROUTES, routes_for
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

_KNOWN_OPERATIONS = {contract.name for contract in WORK_OPERATION_CONTRACTS}


def test_every_route_targets_a_published_operation():
    for route in SERVED_COMMAND_ROUTES:
        assert route.operation in _KNOWN_OPERATIONS, route


def test_next_work_has_two_preconditioned_routes():
    routes = routes_for("next-work")
    assert {route.operation for route in routes} == {
        "work.read.next-work",
        "work.project.next-work",
    }
    for route in routes:
        assert route.precondition, "next-work routes must be preconditioned"


def test_single_route_commands_have_no_ambiguity():
    for route in SERVED_COMMAND_ROUTES:
        if route.command_path == "next-work":
            continue
        assert len(routes_for(route.command_path)) == 1


def test_unknown_command_has_no_routes():
    assert routes_for("db.vacuum") == ()
