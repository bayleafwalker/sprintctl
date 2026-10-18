"""Finite original ref-canonicalizer capture mutants; no product file edits."""
import ast
from copy import deepcopy
import inspect
import os

import pytest


@pytest.fixture(autouse=True)
def inject(monkeypatch):
    mode=os.environ['REF_BINDING_CONTROL']
    if mode=='baseline':return
    from sprintctl import db,pg
    backend=db if mode.startswith('sqlite-') else pg
    name='_freeze_release' if backend is db else '_freeze_release_locked'
    tree=ast.parse(inspect.getsource(getattr(backend,name)));function=tree.body[0]
    binding=next(n for n in function.body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Call)
        and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='canonical_context_refs')
    if mode.endswith('-early'):
        capture=ast.Assign(targets=[ast.Name(id='_early_canonicalizer',ctx=ast.Store())],value=deepcopy(binding.value.func))
        binding.value.func=ast.Name(id='_early_canonicalizer',ctx=ast.Load())
        function.body.insert(1,capture)
    else:
        assert mode=='pg-late'
        fetch=binding.value.args[0]
        assert isinstance(fetch,ast.Call) and fetch.func.attr=='fetchall'
        binding.value.args[0]=ast.Name(id='_late_fetched_refs',ctx=ast.Load())
        function.body.insert(function.body.index(binding),ast.Assign(targets=[ast.Name(id='_late_fetched_refs',ctx=ast.Store())],value=fetch))
    namespace={}
    exec(compile(ast.fix_missing_locations(tree),'<ref-canonicalizer-binding-control>','exec'),backend.__dict__,namespace)
    monkeypatch.setattr(backend,name,namespace[name])
