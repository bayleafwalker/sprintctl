"""PostgreSQL integration tests: Ref, Dep.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    pg,
    _uid,
    PG_MARKS,
)

pytestmark = PG_MARKS


class TestRef:
    def test_add_list_remove(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "pr",
                         "https://github.com/org/repo/pull/1")
        refs = pg.list_refs(store, work_item_id)
        assert any(r["id"] == rid for r in refs)
        pg.remove_ref(store, rid, work_item_id)
        assert not any(r["id"] == rid for r in pg.list_refs(store, work_item_id))

    def test_invalid_ref_type_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_ref(store, work_item_id, "tweet", "https://example.com")

    def test_scope_ref_round_trip(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "glob", "src/**/*.py", "Python sources")
        ref = next(ref for ref in pg.list_refs(store, work_item_id) if ref["id"] == rid)
        assert ref["url"] == "src/**/*.py"
        assert ref["scope"] == {"kind": "glob", "value": "src/**/*.py"}


# ---------------------------------------------------------------------------
# Dep
# ---------------------------------------------------------------------------

class TestDep:
    def test_add_and_list(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Da-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Db-{_uid()}")
        pg.add_dep(store, a, b)
        assert any(d["item_id"] == a for d in pg.list_deps_blocking(store, b))
        assert any(d["blocked_item_id"] == b for d in pg.list_deps_blocked_by(store, a))

    def test_remove_dep(self, store, sprint_id, track_id):
        x = pg.create_work_item(store, sprint_id, track_id, f"Dx-{_uid()}")
        y = pg.create_work_item(store, sprint_id, track_id, f"Dy-{_uid()}")
        dep_id = pg.add_dep(store, x, y)
        pg.remove_dep(store, dep_id, x)
        assert pg.list_deps_blocking(store, y) == []

    def test_get_ready_items(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Ra-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Rb-{_uid()}")
        pg.add_dep(store, a, b)
        ready_ids = {i["id"] for i in pg.get_ready_items(store, sprint_id)}
        assert a in ready_ids
        assert b not in ready_ids

    def test_self_dep_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_dep(store, work_item_id, work_item_id)


# ---------------------------------------------------------------------------
# Recovery snapshot (Postgres → recovery SQLite, ID-preserving)
# ---------------------------------------------------------------------------
