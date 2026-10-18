"""Falsifiers for the frozen reader grading oracle."""
import importlib.util
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('trial_grading',Path(__file__).parents[1]/'scripts/grade_resume_reader_trial.py')
grader=importlib.util.module_from_spec(spec);spec.loader.exec_module(grader)


def test_grading_preserves_unknowns_wrong_sources_types_and_missing_answers():
    expected={'q1':{'value':7,'paths':['/item'],'answer_class':'known'},
              'q2':{'value':'unknown','paths':['/source'],'answer_class':'unknown'},
              'q3':{'value':False,'paths':['/authorizes'],'answer_class':'known'},
              'q4':{'value':None,'paths':['/missing'],'answer_class':'unknown'}}
    report=grader.grade(expected,{'answers':{'q1':{'value':'7','sources':['/item']},
        'q2':{'value':'unknown','sources':['/source']},'q3':{'value':False,'sources':['/item']}}},
        {'item':7,'source':'unknown','authorizes':False,'missing':None})
    assert report['questions']==4
    assert report['correct_known']==0 and report['correct_unknown']==1
    assert report['incorrect']==2 and report['unanswered']==1


def test_caution_cannot_replace_known_answer_and_extra_claims_are_retained():
    expected={'q1':{'value':7,'paths':['/item'],'answer_class':'known'}}
    report=grader.grade(expected,{'answers':{'q1':{'value':'unknown','sources':['/item']},'q99':{'value':True,'sources':['/item']}}},{'item':7})
    assert report['incorrect']==1
    assert report['unsupported_extra_question_ids']==['q99']
    assert report['total_incorrect_claims']==2
    assert report['unsupported_extra_answers'][0]['result']=='incorrect'


@pytest.mark.parametrize('answer',[{},[],{'answers':{},'acceptance':True},{'answers':[]}])
def test_malformed_output_cannot_silently_enter_population(answer):
    with pytest.raises(ValueError):grader.grade({},answer,{})


def test_one_valid_citation_cannot_hide_a_fabricated_or_irrelevant_extra_citation():
    expected={'q1':{'value':7,'paths':['/item'],'answer_class':'known'}}
    for sources in [['/item','/fabricated'],['/item','/unrelated']]:
        report=grader.grade(expected,{'answers':{'q1':{'value':7,'sources':sources}}},{'item':7,'unrelated':7})
        assert report['incorrect']==1
        assert report['rows'][0]['value_correct'] is True
        assert report['rows'][0]['sources_valid'] is False


def test_freeze_preflight_refuses_changed_oracle_grader_or_prompt(tmp_path):
    import json
    source=tmp_path/'source';source.mkdir()
    script=source/'grader.py';script.write_text('frozen grader')
    oracle=tmp_path/'oracle.json';oracle.write_text('{}')
    prompt=tmp_path/'reader-prompt.txt';prompt.write_text('frozen prompt')
    frozen=tmp_path/'freeze.json'
    frozen.write_text(json.dumps({'oracle_sha256':grader.digest(oracle),
        'source_sha256':{'grader.py':grader.digest(script)},'prompt_sha256':grader.digest(prompt)}))
    expectation=grader.digest(frozen)
    grader.verify_freeze(frozen,expectation,oracle,source)
    for path in [script,oracle,prompt,frozen]:
        original=path.read_bytes();path.write_bytes(original+b' changed')
        with pytest.raises(ValueError):grader.verify_freeze(frozen,expectation,oracle,source)
        path.write_bytes(original)
