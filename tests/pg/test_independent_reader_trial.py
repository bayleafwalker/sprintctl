"""Generate frozen inputs/oracles for actual cold readers; no shared live experiments."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from sprintctl import effect_verification, pg
from sprintctl.effect_explanation import render_effect_explanation
from sprintctl.resume_diagnostics import diagnose_resume
from tests.pg._shared import PG_MARKS, _PG_URL, dict_row, psycopg
from tests.pg.test_effect_intent import PROPOSER, _app, _items, _propose, _run
from tests.pg.test_protected_artifact_acceptance import CHECKER, accept, receipt
from tests.pg.test_releases import _edit

pytestmark = PG_MARKS
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def _question(text, value, paths, unknown=False):
    return text, {'value': value, 'paths': paths, 'answer_class': 'unknown' if unknown else 'known'}


def _resume_questions(report):
    owner=report['owner_response']; delta=owner['delta']; checkpoint=delta['source_checkpoint']
    root='/report/owner_response/delta'
    eligible=[r for r in report['observations'] if r['eligible']]
    return [
        _question('What repository does the supplied work belong to?',owner['repo_id'],['/report/owner_response/repo_id']),
        _question('What is the current item ID? Return an integer.',delta['work']['item_id'],[root+'/work/item_id']),
        _question('What is this read\'s authorized successor/current run ID?',delta['authorization_scope']['run_id'],[root+'/authorization_scope/run_id']),
        _question('What is the current Release digest? Return null if unavailable.',delta['current']['current_release_digest'],[root+'/current/current_release_digest'],delta['current']['current_release_digest'] is None),
        _question('Is the Release record changed, unchanged or unknown against the supplied comparison basis?',delta['comparison']['current_release_digest'],[root+'/comparison/current_release_digest'],delta['comparison']['current_release_digest']=='unknown'),
        _question('Is the Decision record changed, unchanged or unknown against the supplied comparison basis?',delta['comparison']['current_decision_digest'],[root+'/comparison/current_decision_digest'],delta['comparison']['current_decision_digest']=='unknown'),
        _question('Is a current continuation checkpoint supplied? Return a boolean.',checkpoint is not None,[root+'/source_checkpoint']),
        _question('What Git SHA is declared in that checkpoint? Return null if unavailable.',checkpoint['declared_git_sha'] if checkpoint else None,[root+'/source_checkpoint/declared_git_sha'] if checkpoint else [root+'/source_checkpoint'],checkpoint is None),
        _question('What assurance is supplied for source bytes? Return the explicit assurance value.',delta['source_bytes'],[root+'/source_bytes'],True),
        _question('What assurance is supplied for external content availability?',delta['external_missing_content'],[root+'/external_missing_content'],True),
        _question('What eligible caller-reported access status is present? Return "none" if no eligible report.',eligible[0]['check']['status'] if eligible else 'none',['/report/observations']),
        _question('Does this report authorize an action? Return a boolean.',report['authorizes_action'],['/report/authorizes_action']),
    ]


def _effect_questions(response):
    snapshot=response['snapshot']; intent=snapshot['intent']; receipt=snapshot['receipt']; root='/owner_response/snapshot'
    return [
        _question('What repository does the supplied work belong to?',response['repo_id'],['/owner_response/repo_id']),
        _question('What is the item ID? Return an integer.',intent['item_id'],[root+'/intent/item_id']),
        _question('What exact intent ID is reported?',intent['intent_id'],[root+'/intent/intent_id']),
        _question('What artifact digest is recorded on the owner intent?',intent['artifact_sha256'],[root+'/intent/artifact_sha256']),
        _question('Does the requested basis match? Return a boolean.',snapshot['requested_basis_matches'],[root+'/requested_basis_matches']),
        _question('Does the requested artifact match? Return a boolean.',snapshot['artifact_digest_matches'],[root+'/artifact_digest_matches']),
        _question('What receipt visibility/input status is reported?',receipt['status'],[root+'/receipt/status']),
        _question('What artifact-check receipt comparison is reported?',receipt['comparison'],[root+'/receipt/comparison'],receipt['comparison']=='unknown'),
        _question('What intent lifecycle state is recorded?',intent['state'],[root+'/intent/state']),
        _question('Is terminal work acceptance established by this supplied read? Return "unknown" when not established.','unknown',['/explanation'],True),
        _question('Is an actual external target effect observed by this supplied read? Return "unknown" when not established.','unknown',['/explanation'],True),
        _question('Does this read authorize acceptance? Return a boolean.',snapshot['authorizes_acceptance'],[root+'/authorizes_acceptance']),
    ]


def test_generate_independent_reader_trial_inputs_and_oracles(store):
    output=os.environ.get('RESUME_COLD_TRIAL_OUTPUT')
    if not output:
        pytest.skip('set RESUME_COLD_TRIAL_OUTPUT for the frozen reader experiment')
    target=Path(output)
    if target.exists():
        raise AssertionError('trial destination must be fresh; no oracle or attempt replacement')
    target.mkdir(parents=True)
    store.connection_factory=lambda:psycopg.connect(_PG_URL,row_factory=dict_row)
    app=_app(store); cases=[]
    def emit(family,condition,document,questions):
        attempt_id='attempt-'+hashlib.sha256(f'cold-reader-v1:{family}:{condition}'.encode()).hexdigest()[:12]
        filename=attempt_id+'/input.json'; (target/attempt_id).mkdir()
        document={'questions':{f'q{i:02}':{'text':text,'source_locations':oracle['paths']} for i,(text,oracle) in enumerate(questions,1)},**document}
        raw=json.dumps(document,sort_keys=True,ensure_ascii=True,indent=2).encode()
        (target/filename).write_bytes(raw)
        cases.append({'attempt_id':attempt_id,'family':family,'condition':condition,'input_file':filename,
                      'input_sha256':'sha256:'+hashlib.sha256(raw).hexdigest(),
                      'oracle':{f'q{i:02}':oracle for i,(_,oracle) in enumerate(questions,1)}})
    for family in ['checkpoint','baseline','access-report']:
        item=_items(store)[0]; run=_run(store,PROPOSER)
        ref=pg.add_ref(store,item,'doc','docs/fixture-not-fetched.md','fixture')
        pg.reserve(store,item,actor='fixture',session_id=uuid.uuid4().hex,role='execution')
        def read(**extra):
            return app.invoke('work.run.continuation-delta-v1',{'run_id':run,'item_id':item,**extra},PROPOSER)
        baseline=read()
        if family=='baseline':
            _edit(pg,store,item,'Inspect replacement artifact before continuation.')
            pg.record_decision(store,item,'revise',actor='fixture-reviewer')
            pg.reserve(store,item,actor='fixture',session_id=uuid.uuid4().hex,role='execution')
        before=diagnose_resume(read(),checkpoint_supplied=False,now=NOW)
        emit(family,'before',{'kind':'continuation','report':before},_resume_questions(before))
        arguments={}; observations=None
        if family=='checkpoint':
            row=pg.get_work_item(store,item)
            pg.create_event(store,row['sprint_id'],'fixture','lane.checkpoint',work_item_id=item,
                payload={'summary':'Continue artifact review','git_sha':'a'*40,'detail':'next_action: inspect replacement artifact'})
        elif family=='baseline':arguments['checkpoint']=baseline['delta']['current']
        owner=read(**arguments)
        if family=='access-report':
            observations={'schema':'sprintctl-resume-observations/v1','repo_id':store.repo_id,'item_id':item,
                'snapshot_digest':owner['delta']['snapshot_digest'],'checks':[{'target':{'kind':'reference','reference_id':ref},
                    'observed_at':NOW.isoformat(),'observer':'fixture-reader','method':'synthetic supplied access report; no content fetched','status':'inaccessible'}]}
        after=diagnose_resume(owner,checkpoint_supplied=bool(arguments),observations=observations,now=NOW)
        emit(family,'after',{'kind':'continuation','report':after},_resume_questions(after))
    for condition in ['matching','changed-artifact','omitted-receipt']:
        item=_items(store)[0]
        reservation=pg.reserve(store,item,actor='fixture',session_id=uuid.uuid4().hex,role='execution',
            acceptance_contract={'effect_verification_required':True,'effect_check_revisions':{'patch-check':'sha256:'+'d'*64}})
        intent=_propose(store,item,_run(store,PROPOSER)); ref,_,_=receipt(store,intent,pg.get_release(store,reservation['release_digest']))
        accept(app,intent,ref)
        arguments={k:intent[k] for k in ('intent_id','revision','canonical_intent_digest')}
        arguments['artifact_sha256']='sha256:'+'0'*64 if condition=='changed-artifact' else effect_verification.artifact_digest(intent['unified_diff'])
        if condition!='omitted-receipt':arguments['verification_ref']=ref
        response=app.invoke('work.effect.acceptance-snapshot-v1',arguments,CHECKER)
        emit('effect',condition,{'kind':'effect','owner_response':response,'explanation':render_effect_explanation(response)},_effect_questions(response))
    assert len(cases)==9
    manifest={'schema':'sprintctl-cold-reader-trial/v1','population_size':9,'question_count':108,
        'case_id_seed':'cold-reader-v1 (owner IDs/times are native generated values bound by input hashes)',
        'fixture_source_sha256':'sha256:'+hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'cases':cases}
    (target/'oracle.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
