"""Coordinator-owned direct primitive examples; product source stays unchanged."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json

import pytest


KINDS = [('accept', 'accepted'), ('reject', 'rejected'), ('withdraw', 'withdrawn'),
         ('supersede', 'superseded'), ('revise', None)]


def request(kind='accept', **changes):
    # Authored already-normalized input; expected admission is never generated
    # by consulting the production transition table.
    return {'kind': kind, 'actor': 'primitive-reviewer', 'rationale': 'explicit reason',
            'evidence_digests': ['ab' * 32, 'cd' * 32], 'release_digest': None,
            'superseded_by_item_id': None, **changes}


def connection(d):
    return d.store.conn if d.is_pg else d.store


@contextmanager
def owner(d, iid):
    conn = connection(d)
    if d.is_pg:
        # Finish only preceding test observation reads, before arranging caller
        # work. No product owner transaction is being composed here.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM work_item WHERE repo_id=%s AND id=%s FOR UPDATE',
                        (d.store.repo_id, iid))
            item = cur.fetchone()
            try:
                invoke = lambda value, row=item, cursor=cur: d.backend._decide_locked(
                    cursor, d.store.repo_id, row, value)
                invoke.cursor = cur
                yield item, invoke
            finally:
                conn.rollback()  # Only after all primary/durable assertions.
    else:
        assert not conn.in_transaction
        conn.execute('BEGIN IMMEDIATE')
        item = dict(conn.execute('SELECT * FROM work_item WHERE id=?', (iid,)).fetchone())
        try:
            yield item, lambda value, row=item, handle=conn: d.backend._decide_locked(handle, row, value)
        finally:
            conn.rollback()


def assert_pending(d):
    assert d.transaction() == ('INTRANS' if d.is_pg else 'ACTIVE')


def legacy(d):
    from uuid import uuid4
    sid = d.backend.create_sprint(d.store, 'direct historical '+uuid4().hex, status='active')
    tid = d.backend.get_or_create_track(d.store, sid, 'legacy')
    closed_at = '2001-02-03T04:05:06Z'  # Distinct from now even with SQLite second precision.
    if d.is_pg:
        with d.store.conn.cursor() as cur:
            cur.execute(f"SET LOCAL {d.backend.LEGACY_IMPORT_SETTING} = 'on'")
            cur.execute("INSERT INTO work_item(repo_id,sprint_id,track_id,title,status,legacy,aggregate_uuid,updated_at) "
                        "VALUES (%s,%s,%s,'historical direct fixture','done',true,%s,%s) RETURNING id",
                        (d.store.repo_id, sid, tid, str(uuid4()), closed_at))
            iid = cur.fetchone()['id']
        d.store.conn.commit()
    else:
        with d.backend.legacy_import_gate(d.store):
            iid = d.store.execute("INSERT INTO work_item(sprint_id,track_id,title,status,legacy,aggregate_uuid,updated_at) "
                                  "VALUES (?,?,'historical direct fixture','done',1,?,?)",
                                  (sid, tid, str(uuid4()), closed_at)).lastrowid
        d.store.commit()
    return replace(d, sprint=sid, track=tid), iid


class LockedDecisionWritingCases:
    @pytest.mark.parametrize('source', ('pending', 'active', 'blocked', 'done'))
    @pytest.mark.parametrize('kind,resolution', KINDS)
    def test_authored_direct_kind_state_matrix(self, writing_driver, source, kind, resolution):
        d = writing_driver
        iid, successor = d.new_item(source), d.new_item()
        before = d.snapshot(iid)
        req = request(kind, superseded_by_item_id=successor if kind == 'supersede' else None)
        original_req = deepcopy(req)
        invalid = source == 'done' or (kind == 'accept' and source != 'active')
        with owner(d, iid) as (item, invoke):
            original_item = deepcopy(item)
            if invalid:
                with pytest.raises(d.backend.InvalidTransition, match='terminal|requires an active item'):
                    invoke(req)
                assert_pending(d)
                assert d.snapshot(iid) == before
                with d.independent() as other:
                    assert d.snapshot(iid, other) == before
                assert item == original_item and req == original_req
                return
            result = invoke(req)
            assert type(result) is dict and result['kind'] == kind
            assert result['work_item_id'] == iid and result['actor'] == 'primitive-reviewer'
            assert result['evidence_digests'] == original_req['evidence_digests']
            assert result['rationale'] == 'explicit reason' and type(result['created_at']) is str
            assert result['superseded_by_item_id'] == req['superseded_by_item_id']
            assert result['release_digest'] is None
            assert item == original_item and req == original_req
            assert_pending(d)
            pending = d.snapshot(iid)
            assert len(pending['decisions']) == len(before['decisions']) + 1
            assert pending['events'] == before['events']  # Primitive owns no canonical event.
            assert pending['releases'] == before['releases']
            if resolution:
                assert pending['item']['status'] == 'done'
                assert pending['item']['resolution'] == resolution
                assert pending['item']['terminal_decision_id'] == result['id']
            else:
                assert pending['item'] == before['item']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            connection(d).commit()
        with d.independent() as other:
            assert d.snapshot(iid, other) == pending

    @pytest.mark.parametrize('kind,resolution', KINDS)
    def test_legacy_branch_preserves_actual_close_time(self, writing_driver, kind, resolution):
        d, iid = legacy(writing_driver)
        successor = d.new_item()
        before = d.snapshot(iid)
        req = request(kind, superseded_by_item_id=successor if kind == 'supersede' else None)
        with owner(d, iid) as (_, invoke):
            if kind == 'revise':
                with pytest.raises(d.backend.InvalidTransition, match='only a terminal decision'):
                    invoke(req)
                assert_pending(d)
                return
            result = invoke(req)
            pending = d.snapshot(iid)
            assert pending['item']['status'] == 'done'
            assert pending['item']['updated_at'] == before['item']['updated_at']
            assert pending['item']['resolution'] == resolution
            assert pending['item']['terminal_decision_id'] == result['id']
            assert pending['events'] == before['events']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            connection(d).commit()
        with d.independent() as other:
            assert d.snapshot(iid, other) == pending

    @pytest.mark.parametrize('rationale,evidence', [('', ['ab'*32]), ('historic', [])])
    def test_legacy_requires_both_evidence_and_rationale_before_release(self, writing_driver, rationale, evidence):
        d, iid = legacy(writing_driver)
        before = d.snapshot(iid)
        req = request(rationale=rationale, evidence_digests=evidence, release_digest='f'*64)
        with owner(d, iid) as (_, invoke):
            with pytest.raises(d.backend.InvalidTransition, match='needs a rationale and at least one evidence digest'):
                invoke(req)
            assert_pending(d)
            assert d.snapshot(iid) == before
            with d.independent() as other:
                assert d.snapshot(iid, other) == before

    @pytest.mark.parametrize('case', ('inactive', 'blank', 'self', 'missing', 'release'))
    def test_competing_invalid_arguments_retain_admission_order(self, writing_driver, case):
        from sprintctl.releases import ReleaseMismatch
        d = writing_driver
        iid = d.new_item('pending' if case == 'inactive' else 'active')
        before = d.snapshot(iid)
        req = request(release_digest='f'*64)
        if case in ('self', 'missing', 'blank'):
            req = request('supersede', superseded_by_item_id=iid if case != 'missing' else 2_000_000_000,
                          rationale='' if case == 'blank' else 'explicit reason', release_digest='f'*64)
        expected = {'inactive': (d.backend.InvalidTransition, 'requires an active item'),
                    'blank': (d.backend.InvalidTransition, 'requires a non-empty rationale'),
                    'self': (ValueError, 'cannot supersede itself'),
                    'missing': (ValueError, 'Superseding item #2000000000 not found'),
                    'release': (ReleaseMismatch, 'not a release of item')}
        with owner(d, iid) as (_, invoke):
            with pytest.raises(*expected[case][:1], match=expected[case][1]):
                invoke(req)
            assert_pending(d)
            assert d.snapshot(iid) == before
            with d.independent() as other:
                assert d.snapshot(iid, other) == before

    def test_successor_is_captured_before_real_release_resolution(self, writing_driver, monkeypatch, record_property):
        # Intentional same-thread test-owned mutation, not a concurrency or
        # general input-immutability guarantee. Other INSERT fields stay late.
        d = writing_driver
        iid, validated, replacement = d.new_item('active'), d.new_item(), d.new_item()
        req = request('supersede', superseded_by_item_id=validated)
        before = d.snapshot(iid)
        name = '_decision_release_locked' if d.is_pg else '_decision_release'
        original = getattr(d.backend, name)
        resolutions = []
        def resolve(*args):
            value = original(*args)
            resolutions.append(value)
            req['superseded_by_item_id'] = replacement
            req['rationale'] = 'late rationale from test-owned resolver'
            return value
        with owner(d, iid) as (_, invoke), monkeypatch.context() as patch:
            patch.setattr(d.backend, name, resolve)
            result = invoke(req)
            assert resolutions == [None]
            assert req['superseded_by_item_id'] == replacement
            assert result['superseded_by_item_id'] == validated
            assert result['rationale'] == 'late rationale from test-owned resolver'
            assert_pending(d)
            pending = d.snapshot(iid)
            assert pending['decisions'][-1]['superseded_by_item_id'] == validated
            assert pending['events'] == before['events']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            connection(d).commit()
        with d.independent() as other:
            assert d.snapshot(iid, other) == pending
        record_property('state_history', json.dumps({'history': 'saved-successor-before-release',
            'validated_successor': validated, 'test_mutated_successor': replacement,
            'before': before, 'pending': pending, 'decision': result}, default=str, sort_keys=True))

    @pytest.mark.parametrize('fault', (False, True))
    @pytest.mark.parametrize('commit', (False, True))
    def test_unrelated_owner_write_stays_provisional_until_caller_boundary(self, writing_driver, fault, commit, monkeypatch, record_property):
        d = writing_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        goal = d.backend.get_sprint(d.store, d.sprint)['goal']
        captured = []
        commit_rejected, commit_error_state = False, None
        decode_name = '_decision_row' if d.is_pg else '_decision_rows'
        original = getattr(d.backend, decode_name)
        armed = True
        def after_decode(*args, **kwargs):
            nonlocal armed
            result = original(*args, **kwargs)
            if armed:
                armed = False
                captured.append(result)
                if fault:
                    raise RuntimeError('after actual primitive decode')
            return result
        with owner(d, iid) as (_, invoke):
            if d.is_pg:
                connection(d).execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',
                                      ('pending direct owner', d.store.repo_id, d.sprint))
            else:
                connection(d).execute('UPDATE sprint SET goal=? WHERE id=?', ('pending direct owner', d.sprint))
            with monkeypatch.context() as patch:
                patch.setattr(d.backend, decode_name, after_decode)
                if fault:
                    with pytest.raises(RuntimeError, match='actual primitive decode'):
                        invoke(request())
                else:
                    invoke(request())
            assert len(captured) == 1 and d.transaction() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(iid)
            assert len(pending['decisions']) == 1
            # PG decodes RETURNING before item effects; SQLite decodes its final
            # result SELECT after item writes. A Python decoder fault preserves
            # that actual pending-state difference without helper cleanup.
            expected_status = 'active' if fault and d.is_pg else 'done'
            assert pending['item']['status'] == expected_status
            assert d.backend.get_sprint(d.store, d.sprint)['goal'] == 'pending direct owner'
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
                assert d.backend.get_sprint(other, d.sprint)['goal'] == goal
            if commit:
                if fault and d.is_pg:
                    import psycopg
                    with pytest.raises(psycopg.errors.CheckViolation, match='terminal decision .* is not bound to work item'):
                        connection(d).commit()
                    commit_rejected, commit_error_state = True, d.transaction()
                    assert commit_error_state == 'IDLE'  # Measured before caller cleanup.
                    # Observe the actual commit failure before owner cleanup.
                    with d.independent() as other:
                        assert d.snapshot(iid, other) == before
                        assert d.backend.get_sprint(other, d.sprint)['goal'] == goal
                else:
                    connection(d).commit()
        effective_commit = commit and not commit_rejected
        with d.independent() as other:
            actual = d.snapshot(iid, other)
            assert actual == (pending if effective_commit else before)
            assert d.backend.get_sprint(other, d.sprint)['goal'] == ('pending direct owner' if effective_commit else goal)
        record_property('state_history', json.dumps({'history': f'direct-owner-{fault}-{commit}',
            'before': before, 'pending_before_owner_boundary': pending,
            'commit_requested': commit, 'commit_rejected_by_deferred_constraint': commit_rejected,
            'transaction_state_after_failed_commit_before_owner_cleanup': commit_error_state,
            'committed': effective_commit, 'durable_after_owner_boundary': actual}, sort_keys=True))

    @pytest.mark.parametrize('row,error,match', [({}, KeyError, 'id'),
        ({'id': 'invalid-item-id'}, ValueError, 'invalid literal')])
    def test_item_conversion_precedes_admission_callback(self, writing_driver, row, error, match, monkeypatch):
        d = writing_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        def forbidden(*args):
            raise AssertionError('admission evaluated before item conversion')
        with owner(d, iid) as (_, invoke), monkeypatch.context() as patch:
            patch.setattr(d.backend._decisions, 'decision_error', forbidden)
            with pytest.raises(error, match=match):
                invoke(request(), row=deepcopy(row))
            assert_pending(d)
            assert d.snapshot(iid) == before

    @pytest.mark.parametrize('explicit', (False, True))
    def test_release_default_and_explicit_resolution_preserve_request(self, writing_driver, explicit):
        d = writing_driver
        iid = d.new_item('active')
        reservation = d.backend.reserve(d.store, iid, actor='fixture', session_id='direct-release')
        digest = reservation['release_digest']
        assert type(digest) is str and len(digest) == 64
        before = d.snapshot(iid)
        req = request('revise', release_digest=digest if explicit else None)
        original = deepcopy(req)
        with owner(d, iid) as (_, invoke):
            result = invoke(req)
            assert result['release_digest'] == digest and req == original
            assert d.snapshot(iid)['releases'] == before['releases']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before

    def test_foreign_release_is_refused_before_decision_write(self, writing_driver):
        from sprintctl.releases import ReleaseMismatch
        d = writing_driver
        iid, other = d.new_item('active'), d.new_item()
        foreign = d.backend.reserve(d.store, other, actor='fixture', session_id='foreign-release')['release_digest']
        before = d.snapshot(iid)
        with owner(d, iid) as (_, invoke):
            with pytest.raises(ReleaseMismatch, match='not a release of item'):
                invoke(request(release_digest=foreign))
            assert_pending(d)
            assert d.snapshot(iid) == before
            with d.independent() as observed:
                assert d.snapshot(iid, observed) == before

    @pytest.mark.parametrize('kind,is_legacy', [(kind, False) for kind, _ in KINDS] +
        [(kind, True) for kind in ('accept', 'reject', 'withdraw', 'supersede')])
    def test_real_insert_decode_effect_order_and_returned_object(self, writing_driver, kind, is_legacy, monkeypatch):
        d = writing_driver
        if is_legacy:
            d, iid = legacy(d)
        else:
            iid = d.new_item('active')
        successor = d.new_item() if kind == 'supersede' else None
        req = request(kind, superseded_by_item_id=successor)
        original_req, order, decoded = deepcopy(req), [], []
        decoder_name = '_decision_row' if d.is_pg else '_decision_rows'
        original_decode = getattr(d.backend, decoder_name)
        def decode(*args, **kwargs):
            result = original_decode(*args, **kwargs)
            decoded.append(result if d.is_pg else result[0])
            order.append('decode')
            return result
        def statement(query):
            sql = ' '.join(query.split()).upper()
            if sql.startswith('INSERT INTO WORK_DECISION '):
                order.append('insert')
            elif sql.startswith('UPDATE WORK_ITEM '):
                order.append('item-update')
            elif sql.startswith('UPDATE WORK_LEASE '):
                order.append('lease-update')
            elif sql.startswith('SELECT * FROM WORK_DECISION WHERE ID = '):
                order.append('final-result-select')
        with owner(d, iid) as (item, invoke), monkeypatch.context() as patch:
            original_item = deepcopy(item)
            patch.setattr(d.backend, decoder_name, decode)
            if d.is_pg:
                original_stamp = d.backend._stamp_awaiting_reports
                def stamp(cur, repo_id, item_id, recorded):
                    assert len(decoded) == 1 and recorded is decoded[0]
                    original_stamp(cur, repo_id, item_id, recorded)
                    order.append('stamp-reports')
                patch.setattr(d.backend, '_stamp_awaiting_reports', stamp)
                class Cursor:
                    def execute(self, query, params=None):
                        result = invoke.cursor.execute(query, params)
                        statement(query)  # Actual SQL finished before this observation.
                        return self if result is invoke.cursor else result
                    def __getattr__(self, name):
                        return getattr(invoke.cursor, name)
                value = invoke(req, cursor=Cursor())
            else:
                class Connection:
                    def execute(self, query, *args, **kwargs):
                        cursor = connection(d).execute(query, *args, **kwargs)
                        statement(query)  # One actual owner execute, not repeated trigger trace callbacks.
                        return cursor
                    def __getattr__(self, name):
                        return getattr(connection(d), name)
                value = invoke(req, handle=Connection())
            assert item == original_item and req == original_req
            assert len(decoded) == 1 and value is decoded[0]
            if d.is_pg:
                expected = ['insert', 'decode', 'stamp-reports']
                if kind != 'revise':
                    expected.append('item-update')
                    if not is_legacy:
                        expected.append('lease-update')
            else:
                expected = ['insert']
                if kind != 'revise':
                    expected.append('item-update')
                expected += ['final-result-select', 'decode']
            assert order == expected, (order, expected)  # Detect omission, duplication and timing, not dictionary equality.
            assert_pending(d)

    @pytest.mark.parametrize('point', ('insert', 'item-update'))
    def test_after_actual_write_python_fault_stays_in_caller_transaction(self, writing_driver, point, record_property):
        d = writing_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        observed = []
        with owner(d, iid) as (_, invoke):
            handle = invoke.cursor if d.is_pg else connection(d)
            armed = True
            class Probe:
                def execute(self, query, *args, **kwargs):
                    nonlocal armed
                    result = handle.execute(query, *args, **kwargs)
                    sql = ' '.join(query.split()).upper()
                    selected = (point == 'insert' and sql.startswith('INSERT INTO WORK_DECISION ')) or (
                        point == 'item-update' and sql.startswith('UPDATE WORK_ITEM '))
                    if selected and armed:
                        armed = False
                        pending = d.snapshot(iid)
                        assert len(pending['decisions']) == 1
                        assert pending['item']['status'] == ('active' if point == 'insert' else 'done')
                        assert_pending(d)
                        with d.independent() as other:
                            assert d.snapshot(iid, other) == before
                        observed.append(pending)
                        raise RuntimeError('after actual direct write')
                    return self if result is handle else result
                def __getattr__(self, name):
                    return getattr(handle, name)
            with pytest.raises(RuntimeError, match='after actual direct write'):
                invoke(request(), **({'cursor': Probe()} if d.is_pg else {'handle': Probe()}))
            assert len(observed) == 1
            assert_pending(d)  # The helper must not catch and roll back its owner.
            assert d.snapshot(iid) == observed[0]
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
        with owner(d, iid) as (_, invoke):
            value = invoke(request())
            connection(d).commit()
        with d.independent() as other:
            final = d.snapshot(iid, other)
            assert len(final['decisions']) == 1 and final['decisions'][0]['id'] == value['id']
            assert final['events'] == before['events']
        record_property('state_history', json.dumps({'history': 'after-real-direct-'+point,
            'before': before, 'pending_before_caller_cleanup': observed,
            'rollback_then_retry': final}, sort_keys=True))

    @pytest.mark.parametrize('point', ('insert', 'item-update'))
    def test_actual_sql_abort_preserves_backend_owner_failure_state(self, writing_driver, point, record_property):
        import sqlite3
        from uuid import uuid4
        d = writing_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        conn = connection(d)
        table = 'work_decision' if point == 'insert' else 'work_item'
        operation = 'INSERT' if point == 'insert' else 'UPDATE'
        key = 'work_item_id' if point == 'insert' else 'id'
        name = 'direct_decision_fault_' + uuid4().hex
        if d.is_pg:
            from psycopg import sql
            conn.rollback()  # Observation reads only, before trigger setup/owner writes.
            conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'injected direct Decision SQL fault'; END $$").format(sql.Identifier(name)))
            conn.execute(sql.SQL('CREATE TRIGGER {} AFTER '+operation+' ON {} FOR EACH ROW WHEN '
                '(NEW.repo_id={} AND NEW.{}={}) EXECUTE FUNCTION {}()').format(
                sql.Identifier(name), sql.Identifier(table), sql.Literal(d.store.repo_id),
                sql.Identifier(key), sql.Literal(iid), sql.Identifier(name)))
        else:
            conn.execute(f'CREATE TEMP TRIGGER {name} AFTER {operation} ON {table} '
                f'WHEN NEW.{key}={iid} BEGIN '
                "SELECT RAISE(ABORT,'injected direct Decision SQL fault'); END")
        conn.commit()  # Fixture trigger only, before caller state.
        observed = None
        try:
            with owner(d, iid) as (_, invoke):
                if d.is_pg:
                    conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',
                                 ('pending SQL-fault owner', d.store.repo_id, d.sprint))
                    import psycopg
                    error = psycopg.Error
                else:
                    conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('pending SQL-fault owner', d.sprint))
                    error = sqlite3.IntegrityError
                with pytest.raises(error, match='injected direct Decision SQL fault'):
                    invoke(request())
                assert d.transaction() == ('INERROR' if d.is_pg else 'ACTIVE')
                if d.is_pg:
                    with pytest.raises(psycopg.errors.InFailedSqlTransaction):
                        d.snapshot(iid)
                    assert d.transaction() == 'INERROR'
                    pending = None
                else:
                    pending = d.snapshot(iid)
                    assert len(pending['decisions']) == (0 if point == 'insert' else 1)
                    assert pending['item'] == before['item']
                    assert d.backend.get_sprint(d.store, d.sprint)['goal'] == 'pending SQL-fault owner'
                with d.independent() as other:
                    assert d.snapshot(iid, other) == before
                observed = {'transaction_before_owner_cleanup': d.transaction(), 'pending': pending,
                            'primary_unreadable': d.is_pg, 'durable': before}
        finally:
            conn.rollback()
            if d.is_pg:
                conn.execute(sql.SQL('DROP TRIGGER {} ON {}').format(sql.Identifier(name), sql.Identifier(table)))
                conn.execute(sql.SQL('DROP FUNCTION {}()').format(sql.Identifier(name)))
            else:
                conn.execute('DROP TRIGGER '+name)
            conn.commit()
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
        with owner(d, iid) as (_, invoke):
            invoke(request())
            conn.commit()
        with d.independent() as other:
            final = d.snapshot(iid, other)
            assert len(final['decisions']) == 1 and final['item']['status'] == 'done'
        record_property('state_history', json.dumps({'history': 'SQL-abort-'+point,
            'before': before, 'observed_before_owner_cleanup': observed, 'retry': final}, sort_keys=True))
