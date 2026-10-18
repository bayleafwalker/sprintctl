"""Extract actual direct Release observations from completed JUnit captures."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def extract(paths):
    captures = {}
    for label, path in paths:
        assert label not in captures, label
        root = ET.parse(path).getroot()
        suites = list(root.iter('testsuite'))
        assert suites and all(int(s.get('errors', 0)) == int(s.get('failures', 0)) == 0 for s in suites)
        observations = []
        for case in root.iter('testcase'):
            if 'test_release_policy' not in case.get('classname', '').split('.'):
                continue
            for prop in case.findall('./properties/property'):
                if prop.get('name') == 'state_history':
                    observations.append({
                        'junit_case': {k: case.get(k) for k in ('classname', 'name')},
                        'observation': json.loads(prop.get('value')),
                    })
        assert observations, path
        captures[label] = observations
    return {'source': 'actual completed JUnit record_property observations; no invented schedules', 'captures': captures}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', nargs='+', metavar='LABEL=XML')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(extract([(label, Path(path)) for label, path in (value.split('=', 1) for value in args.capture)]), indent=2, sort_keys=True) + '\n')
