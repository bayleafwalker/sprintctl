"""Actual remote rebuilds with independently authored persisted-state checks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from uuid import uuid4
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest

from sprintctl import outbox, pg, projection, sync
from sprintctl.pg_testing import assert_disposable_connection
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row
from tests._projection_rebuild_contract import (
    authored_payload, canonical, check_state, check_time, digest,
    expected_record, observe, schema_v1, snapshot,
)
from tests.test_projection_rebuild_oracle import populated
from tests._outbox_fsync_probe import ObserverRefused

pytestmark = PG_MARKS


@contextmanager
def actor(repo_id, *, initialize=False):
    conn = psycopg.connect(_PG_URL, row_factory=dict_row, connect_timeout=5,
                          options='-c statement_timeout=5000 -c lock_timeout=3000')
    try:
        assert_disposable_connection(conn)
        store = pg.PgStore(conn=conn, repo_id=repo_id)
        if initialize:
            pg.init_db(store)
        yield store
    finally:
        conn.close()


def identity(store):
    row = store.conn.execute(
        "SELECT pg_backend_pid() AS pid, current_setting('transaction_isolation') AS isolation, "
        "current_setting('server_version') AS version").fetchone()
    store.conn.commit()
    assert row['isolation'] == 'read committed'
    return row


def produce(path, start, stop, *, label='source'):
    conn = outbox.open_outbox(path)
    records, expected, intervals = [], [], []
    try:
        for index in range(start, stop):
            before = time.time()
            record = outbox.append_observation(
                conn, event_type='work.completed', actor='verification',
                runtime_session_id='rebuild-contract', occurred_at='2026-10-18T00:00:00Z',
                event_id=f'rebuild-{label}-{index}', payload=authored_payload(index, label))
            after = time.time()
            check_time(record.created_at, before, after)
            records.append(record)
            expected.append(expected_record(index, record.origin_stream_id, record.created_at, label))
            intervals.append({'before': before, 'after': after, 'created_at': record.created_at})
    finally:
        conn.close()
    return records, expected, intervals


def normalized(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if isinstance(value, dict):
        return {key: normalized(val) for key, val in value.items()}
    if isinstance(value, list):
        return [normalized(val) for val in value]
    return value



def admit(store, records):
    before = time.time()
    values = pg.ingest_records(store, records)
    after = time.time()
    return values, [before, after]


def metadata(store):
    return {table: normalized(store.conn.execute(
        f'SELECT * FROM {table} WHERE repo_id=%s ORDER BY 1,2', (store.repo_id,)).fetchall())
        for table in ('ingest_stream', 'ingest_repo_cursor')}


def check_ingested_times(rows, interval):
    for row in rows:
        instant = datetime.fromisoformat(row['ingested_at'].replace('Z', '+00:00')).timestamp()
        assert interval[0] - 1 <= instant <= interval[1] + 1, 'remote ingest time outside actual interval'


def ledger(store, expected):
    raw = normalized(store.conn.execute(
        'SELECT * FROM ingest_record WHERE repo_id=%s ORDER BY ingest_offset',
        (store.repo_id,)).fetchall())
    assert len(raw) == len(expected)
    for index, (row, record) in enumerate(zip(raw, expected), 1):
        assert row['repo_id'] == store.repo_id and row['ingest_offset'] == index
        envelope = {key: row['producer_created_at' if key == 'created_at' else key]
                    for key in record}
        assert canonical(envelope) == canonical(record), 'authored remote ledger envelope'
        assert row['record_sha256'] == digest(canonical(record)), 'remote envelope hash'
    return raw


@pytest.fixture
def remote(pg_test_scope, tmp_path):
    repo = pg_test_scope('rebuild-primary')
    foreign_repo = pg_test_scope('rebuild-foreign')
    path = tmp_path / 'producer.db'
    with actor(repo, initialize=True) as store, actor(foreign_repo, initialize=True) as foreign:
        records, expected, intervals = produce(path, 1, 4)
        foreign_records, foreign_expected, foreign_intervals = produce(
            tmp_path / 'foreign.db', 1, 2, label='foreign')
        _, source_ingest_interval = admit(store, records)
        _, foreign_ingest_interval = admit(foreign, foreign_records)
        history = {'reader': identity(store), 'source_append_intervals': intervals,
                   'foreign_append_intervals': foreign_intervals,
                   'foreign_rows': ledger(foreign, foreign_expected),
                   'source_rows': ledger(store, expected),
                   'source_metadata': metadata(store),
                   'source_ingest_interval': source_ingest_interval,
                   'foreign_ingest_interval': foreign_ingest_interval}
        check_ingested_times(history['source_rows'], source_ingest_interval)
        check_ingested_times(history['foreign_rows'], foreign_ingest_interval)
        store.conn.commit()
        yield store, path, expected, history


def checked(path, repo, expected, before, after):
    state, reader = snapshot(path)
    check_state(state, repo, expected, before, after)
    return {'state': state, 'reader': reader, 'operation_interval': [before, after]}


def persist(tmp_path, history):
    (tmp_path / 'projection-rebuild-history.json').write_text(
        json.dumps(history, sort_keys=True, indent=2) + '\n')


def rebuild_checked(store, path, expected, history):
    before = time.time()
    result = sync.rebuild_ingest_projection(store, path, batch_size=2)
    after = time.time()
    assert result.ingest_offset == len(expected)
    history['result'] = checked(path, store.repo_id, expected, before, after)
    raw_mark = history['result']['state']['rows']['cached_ingest_watermark'][0]
    returned = {'ingest_offset': result.ingest_offset, 'advanced_at': result.advanced_at}
    assert returned == {key: raw_mark[key] for key in returned}, 'returned watermark differs from raw state'
    history['returned_watermark'] = returned
    conn = projection.open_cached_projection(path, repo_id=store.repo_id)
    try:
        normal_mark = projection.get_watermark(conn)
        normal_records = projection.list_cached_records(conn)
        normal_schema = projection.get_schema_version(conn)
    finally:
        conn.close()
    assert normal_schema == 2
    assert normal_mark == result
    assert [v.ingest_offset for v in normal_records] == list(range(1, len(expected) + 1))
    assert [canonical(v.record) for v in normal_records] == [canonical(v) for v in expected]
    after_open, after_reader = snapshot(path)
    assert after_open == history['result']['state'], 'normal reopen changed persisted state'
    history['normal_reopen'] = {'schema_version': normal_schema, 'watermark': returned,
                                'records': [v.to_dict() for v in normal_records],
                                'raw_reader_after': after_reader}
    assert list(path.parent.glob(f'.{path.name}.rebuild-*')) == []


def test_projection_rebuild_upgrades_schema_v1_from_real_remote(remote, tmp_path):
    store, _, expected, history = remote
    path = tmp_path / 'cache.db'
    schema_v1(path)
    history['before'], history['before_reader'] = snapshot(path)
    original_bytes = path.read_bytes()
    (tmp_path / 'cache-before.db').write_bytes(original_bytes)
    reopened = projection.open_cached_projection(path, repo_id=store.repo_id)
    try:
        health = projection.assess_freshness(reopened)
        assert health.schema_version == 1 and health.fallback_reason == 'schema-upgrade-required'
        with pytest.raises(RuntimeError, match='requires an atomic rebuild'):
            projection.apply_ingested_records(reopened, [projection.CachedIngestRecord(8, expected[0])])
    finally:
        reopened.close()
    assert path.read_bytes() == original_bytes
    rebuild_checked(store, path, expected, history)
    persist(tmp_path, history)


def test_projection_rebuild_replaces_current_cache_from_real_remote(remote, tmp_path):
    store, _, expected, history = remote
    path = tmp_path / 'cache.db'
    populated(path, 1)
    history['before'], history['before_reader'] = snapshot(path)
    rebuild_checked(store, path, expected, history)
    persist(tmp_path, history)


def remote_writer(repo_id, producer):
    """Own the PG connection and local producer in a separately reaped process."""
    with actor(repo_id) as other:
        writer_identity = identity(other)
        records, authored, intervals = produce(producer, 4, 5)
        admitted, ingest_interval = admit(other, records)
        assert admitted[0].ingest_offset == 4
        return {'writer': writer_identity, 'expected': authored,
                'writer_append_intervals': intervals,
                'writer_ingest_interval': ingest_interval,
                'writer_commit_ack': time.monotonic_ns()}


def test_projection_rebuild_stops_at_captured_high_water_while_remote_advances(
        remote, tmp_path, monkeypatch):
    store, producer, expected, history = remote
    path = tmp_path / 'cache.db'
    original_high = pg.get_ingest_high_water
    original_page = pg.list_ingested_records
    added = []
    history['calls'] = []

    def capture(subject):
        value = original_high(subject)
        history['calls'].append({'operation': 'capture', 'value': value,
                                 'at': time.monotonic_ns()})
        assert value == 3
        return value

    def page(subject, *, after_offset=0, limit=None):
        if after_offset == 0:
            # observe owns the exact child, bounds output/deadline and kills/reaps on error.
            result, reader = observe(
                [sys.executable, '-B', __file__,
                 '--remote-writer', store.repo_id, str(producer)],
                timeout=8, output_limit=65536)
            assert reader['returncode'] == 0
            history.update({key: value for key, value in result.items() if key != 'expected'})
            history['writer_process'] = reader
            added.extend(result['expected'])
            observed = original_high(subject)
            assert observed == 4, 'independent reader did not observe actual commit'
            history['calls'].append({'operation': 'reader-observed-advance', 'value': observed,
                                     'at': time.monotonic_ns()})
        values = original_page(subject, after_offset=after_offset, limit=limit)
        history['calls'].append({'operation': 'page', 'after': after_offset,
                                 'offsets': [v.ingest_offset for v in values],
                                 'at': time.monotonic_ns()})
        return values

    with monkeypatch.context() as hooks:
        hooks.setattr(pg, 'get_ingest_high_water', capture)
        hooks.setattr(pg, 'list_ingested_records', page)
        rebuild_checked(store, path, expected, history)
    assert len(added) == 1
    assert history['writer']['pid'] != history['reader']['pid']
    assert [c['offsets'] for c in history['calls'] if c['operation'] == 'page'] == [[1, 2], [3, 4]]
    assert next(c['at'] for c in history['calls'] if c['operation'] == 'capture') < history['writer_commit_ack']
    assert history['writer_commit_ack'] < next(
        c['at'] for c in history['calls'] if c['operation'] == 'reader-observed-advance')
    history['source_rows_after_advance'] = ledger(store, expected + added)
    check_ingested_times(history['source_rows_after_advance'][-1:], history['writer_ingest_interval'])
    history['source_metadata_after_advance'] = metadata(store)
    conn = projection.open_cached_projection(path, repo_id=store.repo_id)
    try:
        values = original_page(store, after_offset=3, limit=2)
        projection.apply_ingested_records(conn, [sync._cached_record(v) for v in values])
    finally:
        conn.close()
    history['catch_up'] = checked(path, store.repo_id, expected + added,
                                  history['result']['operation_interval'][0], time.time())
    persist(tmp_path, history)


def test_projection_rebuild_remote_writer_stall_is_killed_and_reaped(remote, tmp_path):
    store, producer, expected, history = remote
    nonce = uuid4().hex
    with pytest.raises(ObserverRefused) as caught:
        observe([sys.executable, '-B', __file__,
                 '--stall-remote-writer', store.repo_id, nonce], timeout=3, output_limit=65536)
    error = caught.value
    history['writer_stall_owner'] = error.evidence
    persist(tmp_path, history)
    assert str(error) == 'observer wall-clock deadline'
    assert error.evidence['returncode'] == -9
    marker = json.loads(error.evidence['stdout'])
    assert set(marker) == {'pid', 'nonce', 'phase', 'backend_pid'}
    assert type(marker['pid']) is int and marker['pid'] == error.evidence['pid']
    assert marker['nonce'] == nonce and marker['phase'] == 'connected-before-producer-append'
    assert type(marker['backend_pid']) is int
    deadline = time.monotonic() + 2
    while True:
        remaining = store.conn.execute(
            'SELECT count(*) AS n FROM pg_stat_activity WHERE pid=%s',
            (marker['backend_pid'],)).fetchone()['n']
        store.conn.commit()  # fresh statistics snapshot on each bounded cleanup observation
        if remaining == 0:
            break
        assert time.monotonic() < deadline, 'killed writer backend remains live'
        time.sleep(.01)
    assert pg.get_ingest_high_water(store) == 3
    assert ledger(store, expected) == history['source_rows']
    history['writer_stall_control'] = {'marker': marker, 'owner': error.evidence,
                                       'remaining_backend_rows': remaining}
    persist(tmp_path, history)


class InjectedRemotePageFailure(RuntimeError):
    pass


def test_projection_rebuild_remote_page_failure_preserves_live_cache(remote, tmp_path, monkeypatch):
    store, _, expected, history = remote
    path = tmp_path / 'cache.db'
    populated(path, 1)
    original_bytes = path.read_bytes()
    (tmp_path / 'cache-before.db').write_bytes(original_bytes)
    original_state, original_reader = snapshot(path)
    original_page = pg.list_ingested_records
    before = time.time()
    calls = []

    def page(subject, *, after_offset=0, limit=None):
        values = original_page(subject, after_offset=after_offset, limit=limit)
        calls.append({'after': after_offset, 'offsets': [v.ingest_offset for v in values]})
        if after_offset == 2:
            siblings = list(path.parent.glob(f'.{path.name}.rebuild-*'))
            assert len(siblings) == 1
            history['partial_sibling'] = checked(
                siblings[0], store.repo_id, expected[:2], before, time.time())
            (tmp_path / 'cache-partial-sibling.db').write_bytes(siblings[0].read_bytes())
            raise InjectedRemotePageFailure('after actual second PG page, before returning it')
        return values

    with monkeypatch.context() as hooks:
        hooks.setattr(pg, 'list_ingested_records', page)
        with pytest.raises(InjectedRemotePageFailure, match='actual second PG page'):
            sync.rebuild_ingest_projection(store, path, batch_size=2)
    assert calls == [{'after': 0, 'offsets': [1, 2]}, {'after': 2, 'offsets': [3]}]
    assert path.read_bytes() == original_bytes
    (tmp_path / 'cache-after-failure.db').write_bytes(path.read_bytes())
    after_state, after_reader = snapshot(path)
    assert after_state == original_state
    assert list(path.parent.glob(f'.{path.name}.rebuild-*')) == []
    history.update({'fault': 'after actual second PG page, before return', 'calls': calls,
                    'preserved_state': after_state, 'before_reader': original_reader,
                    'after_reader': after_reader})
    rebuild_checked(store, path, expected, history)
    persist(tmp_path, history)


def test_projection_rebuild_retained_suffix_preserves_live_cache(remote, tmp_path):
    store, _, expected, history = remote
    path = tmp_path / 'cache.db'
    populated(path, 1)
    original_bytes = path.read_bytes()
    (tmp_path / 'cache-before.db').write_bytes(original_bytes)
    original_state, original_reader = snapshot(path)
    cursors = pg.list_ingest_stream_high_water(store)
    high = pg.get_ingest_high_water(store)
    before_metadata = metadata(store)
    # Explicit disposable-only fault injection. The normal immutable ledger refuses deletion.
    # Transactional DDL holds the table lock until the trigger has been restored.
    store.conn.commit()
    with store.conn.transaction():
        assert_disposable_connection(store.conn)
        store.conn.execute('ALTER TABLE ingest_record DISABLE TRIGGER ingest_record_immutable')
        deleted = store.conn.execute(
            'DELETE FROM ingest_record WHERE repo_id=%s AND ingest_offset=1', (store.repo_id,))
        assert deleted.rowcount == 1
        store.conn.execute('ALTER TABLE ingest_record ENABLE TRIGGER ingest_record_immutable')
    assert store.conn.execute(
        "SELECT tgenabled FROM pg_trigger WHERE tgrelid='ingest_record'::regclass "
        "AND tgname='ingest_record_immutable'").fetchone()['tgenabled'] == 'O'
    assert metadata(store) == before_metadata == history['source_metadata']
    history['source_metadata_after_fault'] = metadata(store)
    assert pg.list_ingest_stream_high_water(store) == cursors
    assert pg.get_ingest_high_water(store) == high == 3
    assert [v.ingest_offset for v in pg.list_ingested_records(store)] == [2, 3]
    with pytest.raises(projection.ProjectionGapError) as caught:
        sync.rebuild_ingest_projection(store, path, batch_size=2)
    assert (caught.value.expected, caught.value.received) == (1, 2)
    assert path.read_bytes() == original_bytes
    (tmp_path / 'cache-after-failure.db').write_bytes(path.read_bytes())
    state, reader = snapshot(path)
    assert state == original_state
    assert list(path.parent.glob(f'.{path.name}.rebuild-*')) == []
    history.update({'fixture_mutation': 'transactional disposable-only trigger disable, scoped offset 1 delete, trigger restored before commit', 'source_cursors': cursors,
                    'source_high_water': high, 'gap': [1, 2], 'preserved_state': state,
                    'before_reader': original_reader, 'after_reader': reader})
    persist(tmp_path, history)


def test_projection_rebuild_empty_remote_creates_bound_empty_cache(pg_test_scope, tmp_path):
    repo = pg_test_scope('rebuild-empty')
    foreign_repo = pg_test_scope('rebuild-empty-foreign')
    path = tmp_path / 'cache.db'
    populated(path, 1)
    with actor(repo, initialize=True) as store, actor(foreign_repo, initialize=True) as foreign:
        records, expected, intervals = produce(tmp_path / 'foreign.db', 1, 2, label='foreign')
        _, foreign_ingest_interval = admit(foreign, records)
        history = {'reader': identity(store), 'foreign_rows': ledger(foreign, expected),
                   'foreign_append_intervals': intervals, 'source_rows': ledger(store, [])}
        history['foreign_ingest_interval'] = foreign_ingest_interval
        check_ingested_times(history['foreign_rows'], foreign_ingest_interval)
        assert pg.get_ingest_high_water(store) == 0
        rebuild_checked(store, path, [], history)
        persist(tmp_path, history)


if __name__ == '__main__':
    assert len(sys.argv) == 4 and sys.argv[1] in {'--remote-writer', '--stall-remote-writer'}
    from pathlib import Path
    if sys.argv[1] == '--remote-writer':
        print(canonical(remote_writer(sys.argv[2], Path(sys.argv[3]))))
    else:
        with actor(sys.argv[2]) as store:
            connected = identity(store)
            print(canonical({'pid': os.getpid(), 'nonce': sys.argv[3],
                             'phase': 'connected-before-producer-append',
                             'backend_pid': connected['pid']}), flush=True)
            time.sleep(60)
            raise AssertionError('injected writer stall unexpectedly returned')
