"""The predecessor and retained checkpoint must use one history input."""

from sprintctl import db, handoff


def test_handoff_uses_one_history_for_predecessor_checkpoint_and_delta(conn, active_sprint):
    git = {"sha": "a" * 40, "branch": "before", "worktree": "/tmp/before", "dirty_files": []}
    initial = handoff.build_handoff_bundle(conn, active_sprint, 50, backend=db, version="test", git_context=git)
    event_id = handoff.record_handoff_generated(conn, active_sprint["id"], initial, backend=db, actor="before")
    old = db.list_events(conn, active_sprint["id"])[-1]
    later = {**old, "id": event_id + 1, "created_at": "2099-01-01T00:00:00Z",
             "payload": {"checkpoint": {"sha": "b" * 40}}}

    class ChangingHistory:
        reads = 0

        def __getattr__(self, name):
            return getattr(db, name)

        def list_events(self, store, sprint_id):
            self.reads += 1
            events = db.list_events(store, sprint_id)
            # Maintenance reads first, then the bundle reads its complete history.
            # A redundant predecessor read must not introduce a later history.
            return events + [later] if self.reads > 2 else events

    backend = ChangingHistory()
    bundle = handoff.build_handoff_bundle(conn, active_sprint, 50, backend=backend, version="test")
    assert bundle["delta_since_last_handoff"]["previous_handoff_at"] == old["created_at"]
    assert bundle["last_checkpoint"]["sha"] == git["sha"]
    assert backend.reads == 2
