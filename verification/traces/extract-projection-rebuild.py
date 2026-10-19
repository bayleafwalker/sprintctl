"""Copy bounded raw F13 captures and bind each object to its actual bytes.

Run after both campaigns finish. This collects evidence; it does not declare a
campaign passing or replace the independent runtime assertions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET


def collect(root, output, prefix):
    records = []
    for path in sorted(root.glob('test_projection_rebuild*/*')):
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            continue
        if path.name not in {'oracle-control.json', 'projection-rebuild-history.json'} and path.suffix != '.db':
            continue
        assert 0 < path.stat().st_size <= 2 * 1024 * 1024, 'capture size bound'
        relative = Path(prefix) / path.parent.name / path.name
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        raw = target.read_bytes()
        records.append({'path': relative.as_posix(), 'source': str(path),
                        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return records


def junit(path):
    root = ET.parse(path).getroot()
    nodes = []
    for case in root.iter('testcase'):
        assert not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')), 'nonpassing test'
        nodes.append(case.attrib)
    assert nodes, 'empty campaign'
    return nodes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-root', type=Path, required=True)
    parser.add_argument('--pg-root', type=Path, required=True)
    parser.add_argument('--local-xml', type=Path, required=True)
    parser.add_argument('--pg-xml', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), 'refuse to overwrite earlier capture'
    local_cases, pg_cases = junit(args.local_xml), junit(args.pg_xml)
    args.output.mkdir(parents=True)
    objects = collect(args.local_root, args.output, 'local')
    objects += collect(args.pg_root, args.output, 'pg')
    assert objects, 'empty capture'
    data = {'schema_version': 'projection-rebuild-capture/v1', 'objects': objects,
            'junit': {'local': local_cases, 'pg': pg_cases},
            'qualification': 'raw capture only; result packet and independent review required'}
    (args.output / 'manifest.json').write_text(json.dumps(data, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'objects': len(objects), 'local_nodes': len(local_cases),
                      'pg_nodes': len(pg_cases)}, sort_keys=True))


if __name__ == '__main__':
    main()
