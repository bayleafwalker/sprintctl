"""Extract actual locked item-status observations from completed pytest JUnit files."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def extract(paths):
    runs, histories = [], []
    for path in paths:
        root = ET.parse(path).getroot()
        suites = list(root.iter('testsuite'))
        assert suites and all(int(s.get('errors', 0)) == int(s.get('failures', 0)) == 0 for s in suites)
        runs.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'suites': [s.attrib for s in suites]})
        for case in root.iter('testcase'):
            if 'test_locked_item_status' not in case.get('classname', '').split('.'):
                continue
            for prop in case.findall('./properties/property'):
                if prop.get('name') == 'state_history':
                    history = json.loads(prop.get('value'))
                    history['junit_case'] = {'class': case.get('classname'), 'name': case.get('name')}
                    histories.append(history)
    return {'source': 'actual completed JUnit record_property observations', 'runs': runs,
            'histories': histories}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xml', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = extract(args.xml)
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    print(f"Extracted {len(data['histories'])} actual histories to {args.output}")
