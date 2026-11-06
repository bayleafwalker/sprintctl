"""Disposable PostgreSQL schema 32 and private consent transactions."""
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sprintctl import pg, pg_migrations, recipient_binding_schema as schema
from sprintctl import recipient_binding_pg as binding, operator_request_pg as requests
from sprintctl.operator_request import RequestRefused
from sprintctl.agent_inbox import InboxRefused
from sprintctl.workitemcore import item_status_revision
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection
from tests.test_operator_request_pg import _publish
from tests.test_recipient_provenance import provenance, owner_identity
from sprintctl.recipient_provenance import AuthenticatedRequesterContext

pytestmark = PG_MARKS


@pytest.fixture
def case():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    name = 'recipient_' + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'SET search_path TO "{name}"')
    conn.commit()
    store = pg.PgStore(conn, 'recipient-repo')
    pg.init_db(store)
    sprint = pg.create_sprint(store, 'test', status='active')
    track = pg.get_or_create_track(store, sprint, 'test')
    item = pg.create_work_item(store, sprint, track, 'test')
    request = 'req_' + uuid.uuid4().hex[:26].upper()
    _publish(store, item, request)
    instance = 'ins_' + 'A' * 26
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace', instance, provenance().principal_id))
    caller = replace(provenance(), workspace_id='workspace-a')
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace-a', instance, caller.principal_id))
    yield store, name, request, item, instance, caller
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SET search_path TO public')
        cur.execute(f'DROP SCHEMA "{name}" CASCADE')
    conn.commit()
    conn.close()


def do_bind(case, caller=None, **kwargs):
    store, _, request, item, instance, default = case
    args = dict(request_id=request, work_item_id=item, instance_id=instance, idempotency_key=str(uuid.uuid4())) | kwargs
    with store.conn.transaction(), store.conn.cursor() as cur:
        return binding.bind(cur, store, caller or default, **args)


def prepare_lookup(case):
    store, _, request, item, _, recipient = case
    requester = AuthenticatedRequesterContext(
        repo_id=store.repo_id, principal_id='issuer:requester:2', workspace_id='workspace-a',
        gateway_issuer='issuer', subject='requester', epoch=2,
        authorities=frozenset({'work:evidence'}),
    )
    with store.conn.transaction(), store.conn.cursor() as cur:
        # The canned packet fixture is historical; this lookup test needs a
        # currently fresh request without changing production freshness rules.
        cur.execute(
            "UPDATE operator_request SET requester_principal_id=%s, "
            "packet=jsonb_set(packet, '{blocker,fresh_until}', "
            "to_jsonb((clock_timestamp()+interval '1 hour')::text)) "
            "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
            (requester.principal_id, store.repo_id, requester.workspace_id, request),
        )
        requests.report_performed(cur, store, requester.principal_id, requester.workspace_id,
                                  {'request_id': request, 'expected_revision': 1,
                                   'outcome': 'performed', 'note': 'test'})
        resolved = requests.attest(cur, store, requester.principal_id, requester.workspace_id,
                                   {'request_id': request, 'expected_revision': 2,
                                    'statement': 'verified',
                                    'performed_at': '2026-11-06T00:00:00Z'})
    bound = do_bind(case)
    with store.conn.cursor() as cur:
        cur.execute('SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s',
                    (store.repo_id, item))
        work = cur.fetchone()
    store.conn.commit()
    token = item_status_revision(work)
    return requester, bound, resolved['revision'], token


def test_private_lookup_derives_recipient_and_is_read_only(case):
    store, _, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)
    with store.conn.cursor() as cur:
        cur.execute('SELECT revision,state,packet_digest FROM operator_request WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, request))
        request_before = cur.fetchone()
        cur.execute('SELECT revision,withdrawn_at,principal_id,instance_id FROM operator_request_recipient_binding WHERE binding_id=%s',
                    (bound['binding_id'],))
        binding_before = cur.fetchone()
        cur.execute('SELECT count(*) AS n FROM operator_request_event WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, request))
        events_before = cur.fetchone()['n']
    with store.conn.transaction(), store.conn.cursor() as cur:
        result = binding.lookup_for_recipient_admission(
            cur, store, requester, request_id=request, request_revision=revision,
            resolution_event_revision=revision, work_item_id=item, work_revision=token,
            binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert result['recipient'] == {
        'principal_id': 'issuer:subject:1', 'instance_id': case[4],
        'gateway_issuer': 'issuer', 'subject': 'subject', 'epoch': 1,
        'client_id': 'client', 'grant_id': 'grant', 'resource': 'resource',
        'token_scopes': ['other', 'vuoro:evidence.record', 'vuoro:work.read'],
        'effective_scopes': ['work:evidence', 'work:read'],
        'effective_authority': ['work:read'],
    }
    assert result['request_revision'] == revision and result['work_revision'] == token
    assert 'session_id' not in result and 'packet' not in result
    with store.conn.cursor() as cur:
        cur.execute('SELECT revision,state,packet_digest FROM operator_request WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, request))
        assert cur.fetchone() == request_before
        cur.execute('SELECT revision,withdrawn_at,principal_id,instance_id FROM operator_request_recipient_binding WHERE binding_id=%s',
                    (bound['binding_id'],))
        assert cur.fetchone() == binding_before
        cur.execute('SELECT count(*) AS n FROM operator_request_event WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, request))
        assert cur.fetchone()['n'] == events_before


@pytest.mark.parametrize('change,code', [
    ({'request_revision': 2}, 'resolution-not-current'),
    ({'resolution_event_revision': 2}, 'resolution-not-current'),
    ({'work_revision': 'item:00000000-0000-0000-0000-000000000000@status:done'}, 'work-needs-refresh'),
    ({'binding_revision': 2}, 'binding-unavailable'),
    ({'binding_id': 'foreign-binding'}, 'binding-unavailable'),
])
def test_private_lookup_exact_revision_and_binding_refusals(case, change, code):
    store, _, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)
    arguments = {'request_revision': revision, 'resolution_event_revision': revision,
                 'work_item_id': item, 'work_revision': token,
                 'binding_id': bound['binding_id'], 'binding_revision': bound['revision']}
    arguments.update(change)
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.lookup_for_recipient_admission(cur, store, requester,
                                                   request_id=request, **arguments)
    assert exc.value.code == code


def test_private_lookup_requires_requester_ownership_and_evidence_authority(case):
    store, _, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)
    foreign = AuthenticatedRequesterContext(
        repo_id=store.repo_id, principal_id='issuer:foreign:3', workspace_id=requester.workspace_id,
        gateway_issuer='issuer', subject='foreign', epoch=3,
        authorities=frozenset({'work:evidence'}))
    for caller in (foreign,):
        with pytest.raises(RequestRefused) as exc:
            with store.conn.transaction(), store.conn.cursor() as cur:
                binding.lookup_for_recipient_admission(
                    cur, store, caller, request_id=request, request_revision=revision,
                    resolution_event_revision=revision, work_item_id=item, work_revision=token,
                    binding_id=bound['binding_id'], binding_revision=bound['revision'])
        assert exc.value.code == 'not-found'
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.lookup_for_recipient_admission(
                cur, store, replace(requester, repo_id='another-repo'),
                request_id=request, request_revision=revision,
                resolution_event_revision=revision, work_item_id=item, work_revision=token,
                binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert exc.value.code == 'repository-mismatch'
    with pytest.raises(ValueError, match='work:evidence'):
        AuthenticatedRequesterContext(
            repo_id=store.repo_id, principal_id=requester.principal_id, workspace_id=requester.workspace_id,
            gateway_issuer=requester.gateway_issuer, subject=requester.subject,
            epoch=requester.epoch, authorities=frozenset({'work:read'}))


@pytest.mark.parametrize('mutation,code', [
    ('withdraw-binding', 'binding-unavailable'),
    ('retire-instance', 'instance-retired'),
    ('change-work-status', 'work-needs-refresh'),
    ('withdraw-all-dependants', 'dependant-not-active'),
    ('corrupt-binding-provenance', 'binding-provenance-unavailable'),
    ('expire-request', 'request-needs-refresh'),
])
def test_private_lookup_refuses_changed_owner_state(case, mutation, code):
    store, _, request, item, instance, recipient = case
    requester, bound, revision, token = prepare_lookup(case)
    with store.conn.transaction(), store.conn.cursor() as cur:
        if mutation == 'withdraw-binding':
            binding.withdraw(cur, store, owner_identity(recipient), request_id=request,
                             binding_id=bound['binding_id'], expected_revision=bound['revision'])
        elif mutation == 'retire-instance':
            cur.execute("UPDATE agent_instance SET retired_at=now(),retire_reason='test' "
                        "WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s",
                        (store.repo_id, 'workspace-a', instance))
        elif mutation == 'change-work-status':
            cur.execute("UPDATE work_item SET status='active' WHERE repo_id=%s AND id=%s",
                        (store.repo_id, item))
        elif mutation == 'withdraw-all-dependants':
            cur.execute("UPDATE operator_request_dependant SET state='withdrawn',withdrawn_at=now() "
                        "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
                        (store.repo_id, 'workspace-a', request))
        elif mutation == 'corrupt-binding-provenance':
            cur.execute("UPDATE operator_request_recipient_binding SET effective_authority='[]'::jsonb "
                        "WHERE binding_id=%s", (bound['binding_id'],))
        elif mutation == 'expire-request':
            cur.execute("UPDATE operator_request SET packet=jsonb_set(packet,'{blocker,fresh_until}',"
                        "to_jsonb((clock_timestamp()-interval '1 second')::text)) "
                        "WHERE repo_id=%s AND request_id=%s", (store.repo_id, request))
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.lookup_for_recipient_admission(
                cur, store, requester, request_id=request, request_revision=revision,
                resolution_event_revision=revision, work_item_id=item, work_revision=token,
                binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert exc.value.code == code


def test_private_lookup_requires_supported_attestation_event(case):
    store, _, request, item, _, recipient = case
    requester = AuthenticatedRequesterContext(
        repo_id=store.repo_id, principal_id='issuer:requester:2', workspace_id='workspace-a',
        gateway_issuer='issuer', subject='requester', epoch=2,
        authorities=frozenset({'work:evidence'}),
    )
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(
            "UPDATE operator_request SET requester_principal_id=%s,state='resolved',revision=2, "
            "packet=jsonb_set(packet, '{blocker,fresh_until}', "
            "to_jsonb((clock_timestamp()+interval '1 hour')::text)), updated_at=now() "
            "WHERE repo_id=%s AND workspace_id=%s AND request_id=%s",
            (requester.principal_id, store.repo_id, requester.workspace_id, request),
        )
        cur.execute(
            "INSERT INTO operator_request_event(repo_id,workspace_id,request_id,revision,actor,action,details,recorded_at) "
            "VALUES (%s,%s,%s,2,%s,'resolved-human-attestation',%s::jsonb,now())",
            (store.repo_id, requester.workspace_id, request, requester.principal_id,
             '{"completion_basis":"human-attestation"}'),
        )
    bound = do_bind(case, recipient)
    with store.conn.cursor() as cur:
        cur.execute('SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s',
                    (store.repo_id, item))
        token = item_status_revision(cur.fetchone())
    store.conn.commit()
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.lookup_for_recipient_admission(
                cur, store, requester, request_id=request, request_revision=2,
                resolution_event_revision=2, work_item_id=item, work_revision=token,
                binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert exc.value.code == 'resolution-evidence-unavailable'


def test_private_lookup_checks_only_target_dependant_and_all_its_sessions(case):
    store, _, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)
    with store.conn.cursor() as cur:
        cur.execute('SELECT sprint_id,track_id FROM work_item WHERE repo_id=%s AND id=%s',
                    (store.repo_id, item))
        parent = cur.fetchone()
    other_item = pg.create_work_item(store, parent['sprint_id'], parent['track_id'], 'unrelated')
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s',
                    (store.repo_id, item))
        target = cur.fetchone()
        cur.execute('SELECT id,aggregate_uuid,status FROM work_item WHERE repo_id=%s AND id=%s',
                    (store.repo_id, other_item))
        unrelated = cur.fetchone()
        cur.execute(
            "INSERT INTO operator_request_dependant(repo_id,workspace_id,request_id,work_item_id,session_id,"
            "work_revision,state,attached_at) VALUES (%s,'workspace-a',%s,%s,'target-session-2',%s,'active',now()),"
            "(%s,'workspace-a',%s,%s,'unrelated-session',%s,'active',now())",
            (store.repo_id, request, item, item_status_revision(target),
             store.repo_id, request, other_item, item_status_revision(unrelated)),
        )
        cur.execute("UPDATE work_item SET status='active' WHERE repo_id=%s AND id=%s",
                    (store.repo_id, other_item))
    store.conn.commit()
    with store.conn.transaction(), store.conn.cursor() as cur:
        result = binding.lookup_for_recipient_admission(
            cur, store, requester, request_id=request, request_revision=revision,
            resolution_event_revision=revision, work_item_id=item, work_revision=token,
            binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert result['dependant_count'] == 2

    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute("UPDATE operator_request_dependant SET work_revision=%s WHERE repo_id=%s "
                    "AND workspace_id='workspace-a' AND request_id=%s AND work_item_id=%s "
                    "AND session_id='target-session-2'",
                    ('item:00000000-0000-0000-0000-000000000000@status:done',
                     store.repo_id, request, item))
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.lookup_for_recipient_admission(
                cur, store, requester, request_id=request, request_revision=revision,
                resolution_event_revision=revision, work_item_id=item, work_revision=token,
                binding_id=bound['binding_id'], binding_revision=bound['revision'])
    assert exc.value.code == 'work-needs-refresh'


def test_private_lookup_rechecks_freshness_after_waiting_for_request_lock(case):
    store, name, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute("UPDATE operator_request SET packet=jsonb_set(packet,'{blocker,fresh_until}',"
                    "to_jsonb((clock_timestamp()+interval '2 seconds')::text)) "
                    "WHERE repo_id=%s AND workspace_id='workspace-a' AND request_id=%s",
                    (store.repo_id, request))

    blocker = psycopg.connect(_PG_URL, row_factory=dict_row)
    blocker.cursor().execute(f'SET search_path TO "{name}"')
    blocker.cursor().execute(
        'SELECT request_id FROM operator_request WHERE repo_id=%s AND workspace_id=%s AND request_id=%s FOR UPDATE',
        (store.repo_id, requester.workspace_id, request))
    application_name = 'lookup_' + uuid.uuid4().hex[:20]
    outcomes = []
    started = threading.Event()

    def lookup_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    cur.execute("SELECT set_config('application_name',%s,false)", (application_name,))
                    started.set()
                    binding.lookup_for_recipient_admission(
                        cur, pg.PgStore(conn, store.repo_id), requester,
                        request_id=request, request_revision=revision,
                        resolution_event_revision=revision, work_item_id=item,
                        work_revision=token, binding_id=bound['binding_id'],
                        binding_revision=bound['revision'])
            outcomes.append('returned')
        except RequestRefused as exc:
            outcomes.append(exc.code)
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    worker = threading.Thread(target=lookup_worker)
    worker.start()
    waiting = False
    deadline = time.monotonic() + 10
    try:
        assert started.wait(5)
        while time.monotonic() < deadline:
            with store.conn.cursor() as cur:
                cur.execute('SELECT wait_event_type,query FROM pg_stat_activity WHERE application_name=%s',
                            (application_name,))
                activity = cur.fetchone()
            if activity and activity['wait_event_type'] == 'Lock' and 'operator_request' in activity['query']:
                waiting = True
                break
            if outcomes:
                break
            time.sleep(0.02)
        assert waiting, f'lookup did not block on the request lock: activity={activity!r}, outcomes={outcomes!r}'
        time.sleep(2.2)
    finally:
        blocker.commit()
        blocker.close()
        worker.join(10)
    assert not worker.is_alive()
    assert outcomes == ['request-needs-refresh']


def test_private_adapter_rechecks_gateway_expiry_after_owner_lock_wait(case, tmp_path):
    """A valid assertion must not expose recipient data after lock-wait expiry."""
    vuoro_identity = pytest.importorskip('vuoro_service.identity')
    from sprintctl.recipient_admission_adapter import lookup_for_invocation

    store, name, request, item, _, _ = case
    requester, bound, revision, token = prepare_lookup(case)

    # This expiry race tests the post-lookup boundary using the actual runtime
    # Identity object. The neighboring adapter integration test verifies that
    # object is created from a real signed gateway assertion.
    identity_type = vuoro_identity.Identity
    expiry = datetime.now(UTC) + timedelta(seconds=3)
    identity = identity_type(
        actor='github:requester', environment='adapter-test',
        authorities=frozenset({'work:evidence'}), repo_ids=frozenset({store.repo_id}),
        workspace_id=requester.workspace_id, principal_id=requester.principal_id,
        principal_epoch=requester.epoch, gateway_issuer=requester.gateway_issuer,
        gateway_subject=requester.subject, expires_at=expiry,
    )
    context = SimpleNamespace(identity=identity, repo_id=store.repo_id)

    blocker = psycopg.connect(_PG_URL, row_factory=dict_row)
    blocker.cursor().execute(f'SET search_path TO "{name}"')
    blocker.cursor().execute(
        'SELECT request_id FROM operator_request WHERE repo_id=%s AND workspace_id=%s '
        'AND request_id=%s FOR UPDATE',
        (store.repo_id, requester.workspace_id, request),
    )
    application_name = 'admission_' + uuid.uuid4().hex[:20]
    outcomes = []
    started = threading.Event()

    def lookup_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    cur.execute("SELECT set_config('application_name',%s,false)", (application_name,))
                    started.set()
                    lookup_for_invocation(
                        cur, pg.PgStore(conn, store.repo_id), context,
                        request_id=request, request_revision=revision,
                        resolution_event_revision=revision, work_item_id=item,
                        work_revision=token, binding_id=bound['binding_id'],
                        binding_revision=bound['revision'],
                    )
            outcomes.append('returned')
        except RequestRefused as exc:
            outcomes.append(exc.code)
        except Exception as exc:
            outcomes.append(type(exc).__name__)

    worker = threading.Thread(target=lookup_worker)
    worker.start()
    waiting = False
    deadline = time.monotonic() + 10
    try:
        assert started.wait(5)
        while time.monotonic() < deadline:
            with store.conn.cursor() as cur:
                cur.execute('SELECT wait_event_type,query FROM pg_stat_activity WHERE application_name=%s',
                            (application_name,))
                activity = cur.fetchone()
            if activity and activity['wait_event_type'] == 'Lock' and 'operator_request' in activity['query']:
                waiting = True
                break
            if outcomes:
                break
            time.sleep(0.02)
        assert waiting, f'admission lookup did not block on request lock: {activity!r}, {outcomes!r}'
        time.sleep(max(0, (expiry - datetime.now(UTC)).total_seconds()) + 0.1)
    finally:
        blocker.rollback()
        blocker.close()
        worker.join(10)
    assert not worker.is_alive()
    assert outcomes == ['gateway-identity-expired']


def test_migration_fingerprint_and_rollback(case, monkeypatch):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert schema.inspect_catalog(cur)['available']
        cur.execute('DROP TABLE operator_request_recipient_binding')
        cur.execute('UPDATE schema_version SET version=31')
    original = schema.install
    def fail(cur):
        original(cur)
        raise RuntimeError('failure after DDL')
    monkeypatch.setattr(schema, 'install', fail)
    with pytest.raises(RuntimeError):
        pg_migrations.migrate_schema(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT version, to_regclass('operator_request_recipient_binding') AS relation FROM schema_version")
        assert cur.fetchone() == {'version': 31, 'relation': None}
    store.conn.rollback()
    monkeypatch.setattr(schema, 'install', original)
    assert pg_migrations.migrate_schema(store)['applied_versions'] == [32]
    assert pg_migrations.migrate_schema(store)['applied_versions'] == []
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('ALTER TABLE operator_request_recipient_binding ADD COLUMN drift integer')
        assert not schema.inspect_catalog(cur)['complete']


def test_bind_withdraw_and_foreign_refusals(case):
    store, _, request, item, instance, caller = case
    result = do_bind(case)
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT * FROM operator_request_recipient_binding WHERE binding_id=%s', (result['binding_id'],))
        row = cur.fetchone()
        assert row['resource'] == caller.resource and set(row['token_scopes']) == caller.token_scopes
        assert row['effective_authority'] == ['work:read']
    for change in ({'workspace_id': 'elsewhere'}, {'principal_id': 'issuer:foreign:1', 'subject': 'foreign'}):
        with pytest.raises((RequestRefused, InboxRefused)):
            do_bind(case, replace(caller, **change))
    with pytest.raises(RequestRefused):
        do_bind(case, work_item_id=item + 99999)
    with pytest.raises(RequestRefused):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, replace(owner_identity(caller), principal_id='issuer:foreign:1', subject='foreign'), request_id=request, binding_id=result['binding_id'], expected_revision=1)
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)['revision'] == 2
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)
    assert exc.value.code == 'revision-conflict'
    assert do_bind(case)['binding_id'] != result['binding_id']


def test_concurrent_withdraw_cas(case):
    store, name, request, _, _, caller = case
    identifier = do_bind(case)['binding_id']
    barrier = threading.Barrier(2)
    outcomes = []
    def run():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    binding.withdraw(cur, pg.PgStore(conn, store.repo_id), owner_identity(caller), request_id=request, binding_id=identifier, expected_revision=1)
                conn.commit()
            outcomes.append('committed')
        except RequestRefused as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not t.is_alive() for t in threads)
    assert sorted(outcomes) == ['committed', 'revision-conflict']


def test_instance_retirement_refuses(case):
    store, _, request, item, instance, caller = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('UPDATE agent_instance SET retired_at=now(), retire_reason=%s WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s', ('test', store.repo_id, caller.workspace_id, instance))
    with pytest.raises(RequestRefused) as exc:
        do_bind(case)
    assert exc.value.code == "instance-retired"


def test_catalog_is_visible_without_table_privileges(case):
    import os
    url = os.environ.get('SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL')
    if not url:
        pytest.skip('requires disposable unprivileged catalog probe role')
    store, name, *_ = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA "{name}" TO PUBLIC')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{name}"')
            assert schema.inspect_catalog(cur)['available']
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute('SELECT * FROM operator_request_recipient_binding')


def test_bind_holds_instance_lock_until_commit(case):
    store, name, request, item, instance, caller = case
    locked, release, retired = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def bind_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key="lock-test")
                    locked.set()
                    assert release.wait(10)
        except Exception as exc:
            errors.append(exc)
    def retire_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    cur.execute('UPDATE agent_instance SET retired_at=now(), retire_reason=%s WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s', ('test', store.repo_id, caller.workspace_id, instance))
            retired.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=bind_worker); first.start()
    assert locked.wait(10)
    second = threading.Thread(target=retire_worker); second.start()
    try:
        assert not retired.wait(.2)
    finally:
        release.set()
        first.join(10); second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors and retired.is_set()
    with pytest.raises(RequestRefused) as exc:
        do_bind(case)
    assert exc.value.code == 'instance-retired'


def test_lost_response_retry_conflict_and_fresh_binding(case):
    store, _, request, item, instance, caller = case
    first = do_bind(case, idempotency_key='retry')  # commit, then discard response
    assert do_bind(case, idempotency_key='retry') == first
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, idempotency_key='retry', work_item_id=item + 1)
    assert exc.value.code == 'idempotency-conflict'
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, replace(caller, effective_scopes=caller.effective_scopes | {'extra'}), idempotency_key='retry')
    assert exc.value.code == 'idempotency-conflict'
    with store.conn.transaction(), store.conn.cursor() as cur:
        binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=first['binding_id'], expected_revision=1)
    assert do_bind(case, idempotency_key='retry') == {
        **first, 'historical_replay': True, 'current_revision': 2, 'current_status': 'withdrawn'}
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 0
    fresh = do_bind(case, idempotency_key='fresh')
    assert fresh['binding_id'] != first['binding_id']
    assert do_bind(case, idempotency_key='retry')['current_status'] == 'withdrawn'


def test_different_keys_one_active_and_renewal(case):
    store, _, request, _, _, caller = case
    first = do_bind(case, idempotency_key='first')
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, idempotency_key='second')
    assert exc.value.code == 'binding-already-active'
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1
        binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=first['binding_id'], expected_revision=1)
    second = do_bind(case, idempotency_key='second')
    assert second['binding_id'] != first['binding_id']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1


def test_distinct_instance_is_distinct_active_identity(case):
    store, _, _, _, _, caller = case
    other = 'ins_' + 'B' * 26
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())',
                    (store.repo_id, caller.workspace_id, other, caller.principal_id))
    first = do_bind(case, idempotency_key='first')
    second = do_bind(case, idempotency_key='second', instance_id=other)
    assert first['binding_id'] != second['binding_id']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 2


def test_concurrent_different_keys_one_active(case):
    store, name, request, item, instance, caller = case
    barrier = threading.Barrier(2)
    outcomes, errors = [], []
    def run(key):
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request,
                                 work_item_id=item, instance_id=instance, idempotency_key=key)
            outcomes.append('committed')
        except RequestRefused as exc:
            outcomes.append(exc.code)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(str(i),)) for i in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not thread.is_alive() for thread in threads) and not errors
    assert sorted(outcomes) == ['binding-already-active', 'committed']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1


def test_concurrent_exact_retries(case):
    store, name, request, item, instance, caller = case
    barrier = threading.Barrier(2)
    results, errors = [], []
    def run():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    result = binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key='concurrent')
                conn.commit()
                results.append(result)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(15)
    assert all(not t.is_alive() for t in threads) and not errors
    assert len(results) == 2 and results[0] == results[1]


def test_collision_preserves_foreign_relation(case):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('DROP TABLE operator_request_recipient_binding')
        cur.execute('CREATE TABLE operator_request_recipient_binding (foreign_value integer)')
        cur.execute('INSERT INTO operator_request_recipient_binding VALUES (42)')
        cur.execute('UPDATE schema_version SET version=31')
    for _ in range(2):
        with pytest.raises(pg_migrations.RemoteSchemaMigrationError):
            pg_migrations.migrate_schema(store)
        with store.conn.cursor() as cur:
            cur.execute('SELECT foreign_value FROM operator_request_recipient_binding')
            assert cur.fetchone()['foreign_value'] == 42
            cur.execute('SELECT version FROM schema_version')
            assert cur.fetchone()['version'] == 31
        store.conn.rollback()


def test_handshake_rejects_missing_and_partial_binding_storage(case):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('DROP TABLE operator_request_recipient_binding')
    assert pg_migrations.compatibility_handshake(store)['reason'] == 'recipient-binding-storage-missing'
    store.conn.rollback()
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('CREATE TABLE operator_request_recipient_binding (foreign_value integer)')
    assert pg_migrations.compatibility_handshake(store)['reason'] == 'recipient-binding-storage-partial'
    store.conn.rollback()


def test_runtime_role_can_bind_and_withdraw_without_ddl(case):
    import os
    url = os.environ.get('SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL')
    if not url:
        pytest.skip('requires disposable runtime role')
    store, name, request, item, instance, caller = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA "{name}" TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, UPDATE ON operator_request TO sprintctl_production_probe')
        cur.execute('GRANT SELECT ON operator_request_dependant TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, UPDATE ON agent_instance TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, INSERT, UPDATE ON operator_request_recipient_binding TO sprintctl_production_probe')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{name}"')
            runtime = pg.PgStore(conn, store.repo_id)
            result = binding.bind(cur, runtime, caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key='runtime')
            binding.withdraw(cur, runtime, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute('CREATE TABLE unauthorized_ddl (id integer)')
        conn.rollback()


@pytest.mark.parametrize('mutation', ['withdraw', 'progress'])
def test_bind_serializes_with_request_mutations(case, mutation):
    store, name, request, item, instance, caller = case
    locked, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def bind_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request,
                                 work_item_id=item, instance_id=instance, idempotency_key='mutation')
                    locked.set()
                    assert release.wait(10)
        except Exception as exc:
            errors.append(exc)
    def mutate_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    args = {'request_id': request, 'work_item_id': item, 'session_id': 'session-a', 'expected_revision': 1}
                    if mutation == 'withdraw':
                        requests.withdraw_dependant(cur, pg.PgStore(conn, store.repo_id), 'principal-a', 'workspace-a', args)
                    else:
                        args.update(step_id='check', status='completed', note='checked')
                        requests.progress(cur, pg.PgStore(conn, store.repo_id), 'principal-a', 'workspace-a', args)
            finished.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=bind_worker); first.start()
    assert locked.wait(10)
    second = threading.Thread(target=mutate_worker); second.start()
    try:
        assert not finished.wait(.2)
    finally:
        release.set(); first.join(10); second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors and finished.is_set()
    if mutation == 'withdraw':
        with pytest.raises(RequestRefused) as exc:
            do_bind(case)
        assert exc.value.code == 'dependant-not-active'


def test_inconsistent_provenance_never_reaches_consent_storage(case):
    store, _, _, _, _, caller = case
    with pytest.raises(ValueError, match="principal_id must match"):
        do_bind(case, replace(caller, subject='different-subject'))
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding')
        assert cur.fetchone()['n'] == 0
    assert do_bind(case)['binding_id']


def test_withdraw_after_scope_loss_and_identity_substitution(case):
    store, _, request, item, instance, caller = case
    result = do_bind(case)
    owner = owner_identity(caller)  # Current authentication carries no consent scopes.
    for substitute in (
        replace(owner, workspace_id='elsewhere'),
        replace(owner, principal_id='issuer:foreign:1', subject='foreign'),
        replace(owner, principal_id='other:subject:1', gateway_issuer='other'),
        replace(owner, principal_id='issuer:subject:2', epoch=2),
    ):
        with pytest.raises(RequestRefused) as exc:
            with store.conn.transaction(), store.conn.cursor() as cur:
                binding.withdraw(cur, store, substitute, request_id=request,
                                 binding_id=result['binding_id'], expected_revision=1)
        assert exc.value.code == 'not-found'
    with pytest.raises(TypeError, match='authenticated owner identity'):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, caller, request_id=request,
                             binding_id=result['binding_id'], expected_revision=1)
    with pytest.raises(TypeError, match='verified recipient provenance'):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.bind(cur, store, owner, request_id=request, work_item_id=item,
                         instance_id=instance, idempotency_key='scope-loss')
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert binding.withdraw(cur, store, owner, request_id=request,
                                binding_id=result['binding_id'], expected_revision=1)['revision'] == 2
        cur.execute('SELECT revision, withdrawn_at FROM operator_request_recipient_binding WHERE binding_id=%s',
                    (result['binding_id'],))
        row = cur.fetchone()
        assert row['revision'] == 2 and row['withdrawn_at'] is not None
