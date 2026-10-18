"""Retain actual report outputs while running the unchanged behavioral oracle."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess

BASE = '0b1b18004fa968e9919eeeb924b038e6b18b9986'
records = []
current_node = None
bindings = {}


def pytest_sessionstart(session):
    from sprintctl import db, pg
    for module, backend in [(db, 'db'), (pg, 'pg')]:
        if os.environ['READY_CAPTURE_PHASE'] == 'baseline':
            repo = Path(module.__file__).resolve().parents[1]
            source = subprocess.check_output(['git', 'show', f'{BASE}:sprintctl/{backend}.py'], cwd=repo, text=True)
            node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'get_ready_items')
            text = ast.get_source_segment(source, node)
            # Execute only the accepted base function definition with unchanged
            # real backend helper bindings, never a simulated backend.
            exec(compile(text, f'{BASE}:sprintctl/{backend}.py', 'exec'), module.__dict__)
            bindings[backend] = hashlib.sha256(text.encode()).hexdigest()
        else:
            bindings[backend] = 'current source function and private reducer'
        original = module.get_ready_items
        def capture(handle, sprint, _original=original, _backend=backend):
            try:
                result = _original(handle, sprint)
            except Exception as error:
                records.append({'node': current_node, 'backend': _backend, 'sprint': sprint,
                                'error': type(error).__name__, 'message': str(error)})
                raise
            rows = json.loads(json.dumps(result, default=lambda value: value.isoformat()))
            types = [{key: type(value).__name__ for key, value in row.items()} for row in result]
            records.append({'node': current_node, 'backend': _backend, 'sprint': sprint,
                            'rows': rows, 'field_types': types})
            return result
        module.get_ready_items = capture


def pytest_runtest_setup(item):
    global current_node
    current_node = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['READY_CAPTURE_OUTPUT']).write_text(json.dumps({
        'phase': os.environ['READY_CAPTURE_PHASE'], 'source_base': BASE,
        'bindings': bindings, 'exitstatus': int(exitstatus), 'records': records,
        'limits': 'Complete outputs and native field types from each real-backend oracle run. Baseline capture reexecutes only the accepted base function against unchanged backend helpers after extraction; original pre-edit baseline separately passed15SQLite/16PG. Fresh test IDs/UUIDs/timestamps differ across runs; equality is to the same explicitly authored complete-row oracle in each run, not raw cross-run metadata bytes.'
    }, indent=2)+'\n')
