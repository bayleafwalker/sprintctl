"""Copy exact owned capture objects and reconcile the final campaign inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(source, destination, xml, freeze, repository):
    source = source.resolve()
    assert source.is_dir() and not destination.exists()
    pinned = json.loads(freeze.read_text())
    for category in ('source_sha256', 'protected_product_sha256'):
        for relative, expected in pinned[category].items():
            assert digest(repository / relative) == expected, relative
    for relative in pinned['protected_absence']:
        assert not (repository / relative).exists()
    destination.mkdir()
    cases = []
    captured = 0
    for folder in sorted(source.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        evidence_files = sorted(folder.glob('*.json'))
        if not evidence_files:
            continue
        # Only named evidence emitted by this test module, never neighboring DBs.
        evidence_files = [p for p in evidence_files if p.name in (
            'history.json', 'refusal.json', 'observer-refusal.json', 'startup-refusal.json',
            'stall-marker-refusal.json')]
        if not evidence_files:
            continue
        target = destination / folder.name
        target.mkdir()
        files = []
        captures = folder / 'captures'
        if captures.is_dir():
            (target / 'captures').mkdir()
        markers = sorted(folder.rglob('*.reached.json'))
        for path in evidence_files + ([folder / 'runtime.json'] if (folder / 'runtime.json').exists() else []) + (
                sorted(captures.iterdir()) if captures.is_dir() else []) + markers:
            assert path.is_file() and not path.is_symlink()
            assert 0 < path.stat().st_size <= 2 * 1024 * 1024
            relative = path.relative_to(folder)
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
            assert digest(path) == digest(output)
            files.append({'path': str(output.relative_to(destination)),
                          'sha256': digest(output), 'bytes': output.stat().st_size})
            if path.parent == captures:
                captured += 1
        cases.append({'case': folder.name, 'files': files})
    histories = [json.loads(p.read_text()) for p in destination.glob('*/history.json')]
    outbox = [r for r in histories if 'trace' in r]
    assert len(outbox) == 8 and len(histories) == 14
    killed = [r for r in outbox if r['trace']['mode'] in ('before', 'after')]
    assert len(killed) == 4
    assert {(r['record_kind'], r['trace']['mode']) for r in killed} == {
        (kind, mode) for kind in ('observation', 'authority-command') for mode in ('before', 'after')}
    assert all(r['trace']['returncode'] == -9 and r['trace']['raw_wait_status'] == 9 for r in killed)
    tests = ET.parse(xml).getroot().findall('.//testcase')
    new = [t for t in tests if t.attrib['classname'] == 'tests.test_outbox_fsync']
    assert len(new) == 70 and len(tests) == 144
    assert not ET.parse(xml).getroot().findall('.//failure')
    assert not ET.parse(xml).getroot().findall('.//error')
    assert not ET.parse(xml).getroot().findall('.//skipped')
    result = {'schema': 'outbox-fsync-retained-captures/v1',
              'source_freeze_sha256': digest(freeze), 'xml_sha256': digest(xml),
              'counts': {'new_tests': len(new), 'focused_tests': len(tests),
                         'outbox_histories': len(outbox), 'generic_histories': len(histories)-len(outbox),
                         'actual_outbox_sigkill_histories': len(killed), 'raw_capture_objects': captured,
                         'raw_stall_marker_objects': len(list(destination.rglob('*.reached.json')))},
              'cases': cases}
    (destination / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['counts']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--xml', type=Path, required=True)
    parser.add_argument('--freeze', type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    extract(args.source, args.destination, args.xml, args.freeze, repository)
