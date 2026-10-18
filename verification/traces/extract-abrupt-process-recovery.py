"""Retain actual completed abrupt-process test observations from JUnit."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def extract(path):
    root = ET.parse(path).getroot()
    suites = list(root.iter('testsuite'))
    assert suites and all(int(s.get('errors', 0)) == int(s.get('failures', 0)) == 0 for s in suites)
    observations = []
    for case in root.iter('testcase'):
        if 'test_abrupt_process_recovery' not in case.get('classname', '').split('.'):
            continue
        assert case.find('skipped') is None
        for prop in case.findall('./properties/property'):
            if prop.get('name') == 'state_history':
                value = json.loads(prop.get('value'))
                value['junit_case'] = {'class': case.get('classname'), 'name': case.get('name')}
                observations.append(value)
    assert len(observations) == 13
    kills = [x for x in observations if 'actual_child' in x]
    assert len(kills) == 8 and all(x['actual_child']['returncode'] == -9 for x in kills)
    assert len([x for x in observations if 'negative_control' in x]) == 5
    return {'source': 'actual completed JUnit record_property observations',
        'junit_file': path.name, 'junit_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'suites': [s.attrib for s in suites], 'observations': observations}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xml', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = extract(args.xml)
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    print('Retained 8 actual SIGKILL observations and 5 separate negative controls')
