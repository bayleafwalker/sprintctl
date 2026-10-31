"""Read-only owner facts for a bounded currentness condition, never a grant."""
from __future__ import annotations

from .continuation_basis import SCHEMA as BASIS_SCHEMA

OPERATION = 'work.lease.currentness-v1'
SCHEMA_VERSION = 31
SCHEMA = 'sprintctl-work-currentness/v1'
HOLDER_SCHEMA = {
    'type':'object','properties':{
        'principal_id':{'type':'string','minLength':1},
        'workspace_id':{'type':'string','minLength':1},
        'client_id':{'type':['string','null']},'grant_id':{'type':['string','null']},
    },'required':['principal_id','workspace_id','client_id','grant_id'],'additionalProperties':False,
}
DELEGATION_SCHEMA = {
    'type':'object','properties':{
        'delegation_id':{'type':'string','pattern':'^dlg_[0-9A-HJKMNP-TV-Z]{26}$'},
        'run_id':{'type':'string','pattern':'^run_[0-9A-HJKMNP-TV-Z]{26}$'},
        'operations':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string','minLength':1}},
        'expires_at':{'type':'string','minLength':1},
    },'required':['delegation_id','run_id','operations','expires_at'],'additionalProperties':False,
}
RESULT_SCHEMA = {
    'type':'object','properties':{
        'schema_version':{'const':SCHEMA},'authorizes_effect':{'const':False},
        'run_id':{'type':'string','pattern':'^run_[0-9A-HJKMNP-TV-Z]{26}$'},
        'lease_id':{'type':'string','pattern':'^lease_[0-9A-HJKMNP-TV-Z]{26}$'},
        'holder':HOLDER_SCHEMA,'checkpoint':BASIS_SCHEMA,
        'requirements_sha256':{'type':'string','pattern':'^sha256:[0-9a-f]{64}$'},
        'observed_at':{'type':'string','minLength':1},'checked_at':{'type':'string','minLength':1},
        'lease_expires_at':{'type':'string','minLength':1},
        'delegation':{'anyOf':[DELEGATION_SCHEMA,{'type':'null'}]},
    },'required':['schema_version','authorizes_effect','run_id','lease_id','holder','checkpoint',
                  'requirements_sha256','observed_at','checked_at','lease_expires_at','delegation'],
    'additionalProperties':False,
}


class CurrentnessRefused(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code,self.http_status=code,status
