"""Observe the disposable pytest runtime without changing tests or owner state."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import subprocess


def pytest_sessionstart(session):
    root = Path.cwd().resolve()
    modules = {}
    for name in ('sprintctl', 'sprintctl.pg', 'sprintctl.db', 'sprintctl.authority',
                 'sprintctl.outbox', 'sprintctl.projection', 'sprintctl.reservationcore',
                 'sprintctl.workitemcore', 'sprintctl.proposal_intake'):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if path.parent != root / 'sprintctl':
            raise RuntimeError('unexpected executed package import path')
        modules[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    import psycopg
    # The runner exclusively provisions this local disposable URL. Never persist it.
    with psycopg.connect(os.environ['SPRINTCTL_TEST_PG_URL']) as conn:
        with conn.cursor() as cur:
            cur.execute('SHOW server_version'); version = cur.fetchone()[0]
            cur.execute('SHOW transaction_isolation'); isolation = cur.fetchone()[0]
    data = {'python': platform.python_version(), 'pytest': importlib.import_module('pytest').__version__,
            'psycopg': psycopg.__version__, 'modules': modules,
            'postgresql': {'server_version': version, 'observer_isolation': isolation,
                           'limit': 'Independent metadata connection; not a new measurement of every test actor.'},
            'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'tracked_status': subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True),
            'argv': list(session.config.invocation_params.args),
            'plugin_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if data['head'] != 'd27985bc872f0fe8efcead9c25befef139ed623d':
        raise RuntimeError('campaign requires approved accepted source')
    if data['tracked_status']:
        raise RuntimeError('campaign requires unchanged tracked source')
    session.config._sprintctl2803_runtime = data
    Path('/tmp/sprintctl-2803-runtime.json').write_text(json.dumps(data, indent=2)+'\n')


def pytest_sessionfinish(session, exitstatus):
    data = getattr(session.config, '_sprintctl2803_runtime', None)
    if data is None:
        return
    finish = {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'tracked_status': subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True),
              'modules': {name: hashlib.sha256(Path(info['path']).read_bytes()).hexdigest()
                          for name, info in data['modules'].items()},
              'pytest_exitstatus': int(exitstatus)}
    stable = (finish['head'] == data['head'] and finish['tracked_status'] == data['tracked_status'] == ''
              and all(finish['modules'][name] == info['sha256'] for name, info in data['modules'].items()))
    data['finish_identity'] = finish
    data['source_stable_through_campaign'] = stable
    Path('/tmp/sprintctl-2803-runtime.json').write_text(json.dumps(data, indent=2)+'\n')
    if not stable:
        raise RuntimeError('source identity changed during the campaign')
