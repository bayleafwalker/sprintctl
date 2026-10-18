"""Finite pre-extraction wrong implementations, in memory only."""
import ast
from copy import deepcopy
import inspect
import json
import os
import re

import pytest


@pytest.fixture(autouse=True)
def inject_release_policy_control(monkeypatch):
    mode=os.environ['RELEASE_POLICY_CONTROL']
    if mode=='baseline':return
    from sprintctl import db,pg,releases
    backend=db if mode.startswith('sqlite-') else pg
    action=mode.split('-',1)[1]
    if action=='digest-refs':
        original=releases.release_digest
        monkeypatch.setattr(releases,'release_digest',lambda aggregate,revision,contract,refs: original(aggregate,revision,contract,[]))
        return
    if action=='json-escape':
        monkeypatch.setattr(releases,'canonical_json',lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True))
        return
    if action in ('eager-values','commit'):
        name='_freeze_release' if backend is db else '_freeze_release_locked'
    elif action in ('falsey-default','foreign-member','extra-member-default'):
        name='_decision_release' if backend is db else '_decision_release_locked'
    elif action.startswith('basis-') or action=='readonly-lock':
        name='_release_basis' if backend is db else '_release_basis_locked'
    else:
        name='_current_release_row' if backend is db else '_current_release_locked'
    tree=ast.parse(inspect.getsource(getattr(backend,name)))
    function=tree.body[0]
    changed=0
    if action=='eager-values':
        for i,node in enumerate(function.body):
            if (isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='execute'
                and isinstance(node.value.args[0],ast.Constant) and 'INTO work_release' in node.value.args[0].value):
                values=node.value.args[1]
                assert isinstance(values,ast.Tuple)
                node.value.args[1]=ast.Name(id='_eager_insert_values',ctx=ast.Load())
                function.body.insert(i,ast.Assign(targets=[ast.Name(id='_eager_insert_values',ctx=ast.Store())],value=values))
                changed=1
                break
    elif action=='commit':
        assert isinstance(function.body[-1],ast.Return)
        function.body.insert(len(function.body)-1,ast.parse('conn.commit()' if backend is db else 'cur.connection.commit()').body[0])
        changed=1
    elif action=='falsey-default':
        branch=next(n for n in function.body if isinstance(n,ast.If))
        assert ast.unparse(branch.test)=='release_digest is None'
        branch.test=ast.UnaryOp(op=ast.Not(),operand=ast.Name(id='release_digest',ctx=ast.Load()))
        changed=1
    elif action=='extra-member-default':
        branch=next(n for n in function.body if isinstance(n,ast.If))
        assert ast.unparse(branch.test)=='release_digest is None'
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args and isinstance(n.args[0],ast.Constant) and 'SELECT 1 FROM work_release' in n.args[0].value]
        assert len(calls)==1
        branch.body.insert(0,ast.Expr(value=deepcopy(calls[0])))
        changed=1
    elif action in ('basis-event-item','basis-revise-item','current-revise-item'):
        alias='e' if action=='basis-event-item' else 'd'
        outer='wr' if action=='current-revise-item' else 'wi'
        for node in ast.walk(function):
            if not (isinstance(node,ast.Constant) and isinstance(node.value,str) and 'SELECT ' in node.value):continue
            sql=node.value
            if backend is db:
                sql,n=re.subn(alias+r'\.work_item_id = '+outer+r'\.id\s+AND\s+' if outer=='wi' else alias+r'\.work_item_id = wr\.work_item_id\s+AND\s+','',sql)
            else:
                sql,n=re.subn(r'\s+AND '+alias+r'\.work_item_id = '+(r'wi\.id' if outer=='wi' else r'wr\.work_item_id'),'',sql)
            if n:assert n==1;node.value=sql;changed+=1
    elif action=='current-is-none':
        result=function.body[-1].value
        assert isinstance(result,ast.IfExp) and isinstance(result.test,ast.Name) and result.test.id=='row'
        result.test=ast.Compare(left=ast.Name(id='row',ctx=ast.Load()),ops=[ast.IsNot()],comparators=[ast.Constant(value=None)])
        changed=1
    elif action=='readonly-lock':
        for node in ast.walk(function):
            if isinstance(node,ast.IfExp) and isinstance(node.test,ast.Name) and node.test.id=='lock':
                node.test=ast.Constant(value=True);changed+=1
    else:
        for node in ast.walk(function):
            if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='execute'
                and node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str)):
                continue
            sql=node.args[0].value
            if action=='foreign-member' and 'SELECT 1 FROM work_release' in sql:
                assert 'repo_id = %s AND ' in sql
                sql=sql.replace('repo_id = %s AND ','',1)
                assert isinstance(node.args[1],ast.Tuple)
                assert ast.unparse(node.args[1].elts.pop(0))=='repo_id'
            elif 'SELECT wr.* FROM work_release' in sql:
                if action=='omit-revise':sql,n=re.subn(r"AND wr\.revise_count = \([\s\S]+?\)\s*(?=ORDER BY)",'',sql);assert n==1
                elif action=='newest-release':sql,n=re.subn(r"ORDER BY \([\s\S]+?\) DESC NULLS LAST, wr\.id DESC",'ORDER BY wr.id DESC',sql);assert n==1
                elif action=='reservation-time':assert sql.count('SELECT MAX(r.id)')==1;sql=sql.replace('SELECT MAX(r.id)','SELECT MAX(r.created_at)')
                else:raise AssertionError(action)
            else:continue
            node.args[0].value=sql;changed+=1
    assert changed==1,(mode,changed)
    namespace={}
    exec(compile(ast.fix_missing_locations(tree),'<release-policy-control>','exec'),backend.__dict__,namespace)
    monkeypatch.setattr(backend,name,namespace[name])
