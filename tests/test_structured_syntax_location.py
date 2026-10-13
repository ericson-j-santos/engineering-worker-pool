import ast
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.structured_diagnostics import RevisionTrace, DiagnosticWriteError, syntax_kind
SOURCE = 'def f(x):\n    return x\n'
ALLOWED = {type(n) for n in ast.walk(ast.parse(SOURCE))}

@pytest.mark.parametrize(('code','kind','line','prior_colon'),[
    ('def f(x):\n    if x:\n    return x\n','missing_block',3,True),
    ('def f(x):\n    return x\n      return x\n','unexpected_indent',3,False),
    ('def f(x):\n    if x:\n        return x\n     return x\n','bad_unindent',4,False),
    ('def f(x):\n    return x )\n','other',2,True),
    ('def f(x):\n    return x if\n','invalid_syntax',2,True),
])
def test_syntax_details_are_bounded_and_do_not_copy_source(code,kind,line,prior_colon):
    output=[]
    trace=RevisionTrace('SECRET_REVIEW',output.append)
    trace.before_guard(code,ALLOWED)
    trace.rejected('candidate_syntax')
    result=output[-1]
    assert result['stage']=='rejected'
    assert result['syntax_kind']==kind
    assert result['syntax_line']==line
    assert result['syntax_prior_line_colon'] == prior_colon
    assert result['ast_parse']=='invalid'
    assert 'SECRET' not in json.dumps(result)
    assert code not in json.dumps(result)
    assert len(json.dumps(result)) < 2048

def test_replay_idempotent_and_valid_source_without_syntax_fields():
    results=[]
    for i in range(2):
        trace=RevisionTrace('abc', results.append)
        trace.before_guard(SOURCE,ALLOWED)
        trace.accepted()
    assert results[2]==results[5]
    assert 'syntax_line' not in results[5]
    assert results[5]['ast_parse']=='valid'

def test_observer_receives_error_before_rejection(tmp_path):
    target=tmp_path/'diagnostic.json'
    def writer(record):target.write_text(json.dumps(record))
    trace=RevisionTrace('review-secret',writer)
    assert json.loads(target.read_text())['stage']=='received'
    trace.before_guard('def f(x):\n    if x:\n    return x\n',ALLOWED)
    assert json.loads(target.read_text())['syntax_kind']=='missing_block'
    trace.rejected('candidate_syntax')
    assert json.loads(target.read_text())['stage']=='rejected'
    assert 'review-secret' not in target.read_text()

def test_observer_failure_fails_closed():
    trace=RevisionTrace('')
    trace.observer=lambda _: (_ for _ in ()).throw(ValueError('PRIVATE_VALUE'))
    with pytest.raises(DiagnosticWriteError,match='^revision_diagnostic_write_failed$'):
        trace.before_guard('def f(x):\n    if x:\n    return x\n',ALLOWED)

def test_oversized_source_not_parsed():
    trace=RevisionTrace('')
    trace.before_guard('x'*65537,ALLOWED)
    assert trace.record['ast_parse']=='size_blocked'
    assert 'syntax_line' not in trace.record

def test_syntax_kind_unknown_not_leaked():
    exc=SyntaxError('PRIVATE_MESSAGE')
    assert syntax_kind(exc)=='other'
