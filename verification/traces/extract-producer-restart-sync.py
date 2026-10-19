"""Extract actual F02 histories from completed JUnit captures, without invented phases."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

MODULES = {'test_producer_restart_sync', 'test_producer_restart_sync_oracle'}


def extract(captures):
    result = {}
    for label, path in captures:
        assert label not in result
        root = ET.parse(path).getroot()
        suites = list(root.iter('testsuite'))
        assert suites and all(int(s.get('errors', 0)) == int(s.get('failures', 0)) == 0 for s in suites)
        observations = []
        for case in root.iter('testcase'):
            if not MODULES.intersection(case.get('classname', '').split('.')):
                continue
            assert case.find('skipped') is None
            values = [p.get('value') for p in case.findall('./properties/property')
                      if p.get('name') == 'state_history']
            assert len(values) == 1
            observations.append({'junit_case': {'classname': case.get('classname'), 'name': case.get('name')},
                                 'observation': json.loads(values[0])})
        assert observations
        result[label] = observations
    return {'source': 'actual completed JUnit record_property observations', 'captures': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', nargs='+', metavar='LABEL=XML')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    inputs = [(label, Path(path)) for label, path in (v.split('=', 1) for v in args.captures)]
    args.output.write_text(json.dumps(extract(inputs), indent=2, sort_keys=True) + '\n')
