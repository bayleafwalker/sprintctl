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
