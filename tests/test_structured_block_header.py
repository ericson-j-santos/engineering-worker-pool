import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from app.structured_anchors import assemble_anchors,AnchorRejected,anchored_schema

SOURCE = "def f(x):\n    if x is None:\n        return ''\n    if x:\n        return ''\n    return x\n"

def make(edits): return json.dumps({'edits':edits})

@pytest.mark.parametrize('new', ['return 1', '', 'if x:\n    return 1', 'if x:\n    return 1\nelse:\n    return 0'])
def test_block_replacement_must_leave_an_opening_header(new):
    if new.endswith(':'):
        result,_=assemble_anchors(make([{'anchor':'L2','new':new}]),SOURCE)
        assert result.startswith('def f(x):\n    if x:')
    else:
        with pytest.raises(AnchorRejected,match='^revision_block_header$'):
            assemble_anchors(make([{'anchor':'L2','new':new}]),SOURCE)

def test_return_anchor_can_change_without_header_restriction():
    changed,_=assemble_anchors(make([{'anchor':'L6','new':'return 1'}]),SOURCE)
    assert changed.endswith('    return 1\n')

def test_example_reproduces_unexpected_indent_and_is_blocked_earlier():
    import ast
    defective=SOURCE.replace('    if x:', '    return 1')
    with pytest.raises(IndentationError,match='unexpected indent'):
        ast.parse(defective)
    with pytest.raises(AnchorRejected,match='revision_block_header'):
        assemble_anchors(make([{'anchor':'L4','new':'return 1'}]),SOURCE)

def test_replay_and_multi_line_header_preservation():
    edits=make([{'anchor':'L2','new':"if not x:\n    return None\nif x is None:"},
                {'anchor':'L6','new':'return 9'}])
    one=assemble_anchors(edits,SOURCE)
    assert one==assemble_anchors(edits,SOURCE)
    assert one[0].endswith('    return 9\n')
    assert SOURCE.endswith('    return x\n')

def test_schema_still_bounded_and_signature_not_editable():
    item=anchored_schema(SOURCE)['properties']['edits']['items']
    assert 'L1' not in item['properties']['anchor']['enum']
    assert item['properties']['new']['maxLength']==6000
    assert item['additionalProperties'] is False


def test_negative_control_rejects_broken_header_without_relaxing_ast():
    from app.structured_diagnostics import REASONS
    assert 'revision_block_header' in REASONS
    with pytest.raises(AnchorRejected):
        assemble_anchors(make([{'anchor':'L2','new':'return x[0]'}]),SOURCE)
