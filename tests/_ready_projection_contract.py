"""Shared real-backend characterization oracle for ready graph projection."""
from copy import deepcopy
from dataclasses import dataclass

import pytest


@dataclass
class ReadyDriver:
    api: object
    handle: object
    sprint: int
    independent: object
    stamp: object
    write_description: object
    in_transaction: object

    def item(self, title='item', *, sprint=None, priority=None):
        sprint = self.sprint if sprint is None else sprint
        track = self.api.get_or_create_track(self.handle, sprint, 'ready')
        return self.api.create_work_item(self.handle, sprint, track, title, priority=priority)

    def other_sprint(self):
        return self.api.create_sprint(self.handle, 'other ready scope', status='active')

    def status(self, ident, state):
        if state != 'pending':
            self.api.set_work_item_status(self.handle, ident, 'active')
        if state in ('blocked', 'done'):
            self.api.set_work_item_status(self.handle, ident, state)

    def expected(self, ident, *, priority=None, blockers=0):
        return {**self.api.get_work_item(self.handle, ident), 'track_name': 'ready',
                'blockers_resolved': blockers, 'unresolved_blockers': 0,
                'effective_priority': priority}


class ReadyProjectionCases:
    @pytest.mark.parametrize('unknown', [False, True])
    def test_empty_and_unknown_sprint(self, ready_driver, unknown):
        d = ready_driver
        assert d.api.get_ready_items(d.handle, -1 if unknown else d.sprint) == []

    def test_only_pending_items_in_requested_sprint(self, ready_driver):
        d = ready_driver
        eligible = d.item('eligible')
        for state in ('active', 'blocked', 'done'):
            d.status(d.item(state), state)
        d.item('different sprint', sprint=d.other_sprint())
        assert d.api.get_ready_items(d.handle, d.sprint) == [d.expected(eligible)]

    @pytest.mark.parametrize('state', ['pending', 'active', 'blocked'])
    def test_unresolved_blocker_is_excluded(self, ready_driver, state):
        d = ready_driver
        blocker = d.item('unresolved', sprint=d.other_sprint())
        d.status(blocker, state)
        target, free = d.item('blocked target'), d.item('free')
        d.api.add_dep(d.handle, blocker, target)
        assert d.api.get_ready_items(d.handle, d.sprint) == [d.expected(free)]

    @pytest.mark.parametrize('kind', ['accept', 'reject', 'withdraw', 'supersede'])
    def test_every_done_resolution_resolves_existing_graph_edge(self, ready_driver, kind):
        d = ready_driver
        other = d.other_sprint()
        blocker = d.item('terminal blocker', sprint=other)
        d.status(blocker, 'active')
        kwargs = {'superseded_by_item_id': d.item('successor', sprint=other)} if kind == 'supersede' else {}
        d.api.record_decision(d.handle, blocker, kind, actor='oracle', rationale='characterize existing graph rule', **kwargs)
        target = d.item('eligible target')
        d.api.add_dep(d.handle, blocker, target)
        assert d.api.get_ready_items(d.handle, d.sprint) == [d.expected(target, blockers=1)]

    def test_multiple_resolved_blockers_are_counted(self, ready_driver):
        d = ready_driver
        other = d.other_sprint()
        target = d.item('ready after two')
        for title in ('first', 'second'):
            blocker = d.item(title, sprint=other)
            d.status(blocker, 'done')
            d.api.add_dep(d.handle, blocker, target)
        assert d.api.get_ready_items(d.handle, d.sprint) == [d.expected(target, blockers=2)]

    def test_priority_fallback_override_and_timestamp_id_ties(self, ready_driver):
        d = ready_driver
        late = d.item('late numeric two', priority=2)
        early = d.item('early numeric two', priority=2)
        tie_a = d.item('tie a', priority=2)
        tie_b = d.item('tie b', priority=2)
        title = d.item('[p2] title fallback')
        override = d.item('[p9] explicit one wins', priority=1)
        conflicting = d.item('[p1] explicit five wins', priority=5)
        plain = d.item('unprioritized')
        for ident, stamp in [(late,'2030-01-01T00:00:00Z'), (early,'2020-01-01T00:00:00Z'),
                             (tie_a,'2025-01-01T00:00:00Z'), (tie_b,'2025-01-01T00:00:00Z'),
                             (title,'2010-01-01T00:00:00Z')]:
            d.stamp(ident, stamp)
        expected = [d.expected(override, priority=1)]
        expected += [d.expected(i, priority=2) for i in [title, early, tie_a, tie_b, late]]
        expected += [d.expected(conflicting, priority=5), d.expected(plain)]
        assert d.api.get_ready_items(d.handle, d.sprint) == expected

    def test_real_read_order_and_inputs_remain_unmodified(self, ready_driver, monkeypatch):
        d = ready_driver
        first, second = d.item('first'), d.item('second')
        d.api.add_dep(d.handle, first, second)
        original_items, original_blockers = d.api.list_work_items, d.api.list_deps_blocking
        captured, blockers, order = [], [], []
        def items(*args, **kwargs):
            rows = original_items(*args, **kwargs)
            captured.append((rows, deepcopy(rows)))
            return rows
        def dependencies(handle, ident):
            order.append(ident)
            rows = original_blockers(handle, ident)
            blockers.append((rows, deepcopy(rows)))
            return rows
        monkeypatch.setattr(d.api, 'list_work_items', items)
        monkeypatch.setattr(d.api, 'list_deps_blocking', dependencies)
        result = d.api.get_ready_items(d.handle, d.sprint)
        assert result == [d.expected(first)]
        assert order == [r['id'] for r in captured[0][0]]
        for actual, before in captured + blockers:
            assert actual == before
        assert all(result[0] is not r for r in captured[0][0])

    @pytest.mark.parametrize('fail_read', [False, True])
    def test_read_preserves_rollbackable_caller_write(self, ready_driver, monkeypatch, fail_read):
        d = ready_driver
        item = d.item('caller-owned')
        before = d.api.get_work_item(d.handle, item)
        d.write_description(item, 'pending caller write')
        if fail_read:
            original = d.api.list_deps_blocking
            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('backend read failure')
            monkeypatch.setattr(d.api, 'list_deps_blocking', fail)
            with pytest.raises(RuntimeError, match='backend read failure'):
                d.api.get_ready_items(d.handle, d.sprint)
        else:
            assert d.api.get_ready_items(d.handle, d.sprint) == [d.expected(item)]
        assert d.in_transaction()
        assert d.api.get_work_item(d.handle, item)['description'] == 'pending caller write'
        with d.independent() as other:
            assert d.api.get_work_item(other, item) == before
            assert d.api.list_events(other, d.sprint) == []
            assert d.api.list_reservations(other, item, active_only=False) == []
            assert d.api.list_releases(other, item) == []
            assert d.api.list_decisions(other, item) == []
        getattr(d.handle, 'conn', d.handle).rollback()
        assert d.api.get_work_item(d.handle, item) == before
