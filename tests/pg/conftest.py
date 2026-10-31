"""Registers tests/pg/_shared.py's module-scoped fixtures for this directory.

Fixture functions must be visible as names in a conftest.py (or the test
module itself) for pytest to discover them; importing them here re-exports
tests/pg/_shared.py's fixtures to every test file in this package.
"""
from tests.pg._shared import (  # noqa: F401
    pg_test_scope,
    store,
    sprint_id,
    track_id,
    work_item_id,
)


def pytest_ignore_collect(collection_path, config):
    """Keep the opt-in experiment out of the ordinary zero-skip PG gate."""
    import os

    if collection_path.name == 'test_outcome_trigger_faults.py':
        return not bool(os.environ.get('SPRINTCTL_TEST_FK_FAULT_ADMIN_URL'))
    if collection_path.name == 'test_independent_reader_trial.py':
        return not bool(os.environ.get('RESUME_COLD_TRIAL_OUTPUT'))
    if collection_path.name == 'test_e4_edge_composition.py':
        return not bool(os.environ.get('SPRINTCTL_TEST_E4_EDGE_COMPOSITION'))
    return None
