"""The reader experiment is explicitly opt-in, including during collection."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('opt_in', [False, True])
def test_reader_trial_collection_requires_explicit_output(opt_in, tmp_path):
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment.pop('RESUME_COLD_TRIAL_OUTPUT', None)
    if opt_in:
        environment['RESUME_COLD_TRIAL_OUTPUT'] = str(tmp_path / 'uncreated-output')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q',
         'tests/pg', '-k', 'test_generate_independent_reader_trial_inputs_and_oracles'],
        cwd=root, env=environment, text=True, capture_output=True,
    )
    assert result.returncode == (0 if opt_in else 5), result.stdout + result.stderr
    assert ('test_generate_independent_reader_trial_inputs_and_oracles' in result.stdout) is opt_in
    assert not (tmp_path / 'uncreated-output').exists()
