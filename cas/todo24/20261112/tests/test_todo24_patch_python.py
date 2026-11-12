"""Non-operational, isolated Python verification of exact persisted patch bytes.
This is NOT the independent checkout, NOT an authenticated Validator, and NOT E2E.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

PATCH = Path(__file__).with_name('desktop_state_blocker_fix.patch')
SHA256 = 'bc3a0084d7316b0adb780134fc3ce2468b3ed0fb5a210a707c3b7f8566cdd76d'
EXPECTED_GUARD_CODES = {
    'FIXED_PATH_MISSING', 'FIXED_PATH_REDIRECTED', 'FIXED_PATH_INVALID',
    'CODE_FILE_SIZE_LIMIT', 'CODE_FILE_READ_FAILED', 'CODE_CHANGED_DURING_READ',
    'CODE_ENTRY_LIMIT', 'CODE_PATH_REDIRECTED', 'CODE_FILE_TYPE_INVALID',
    'CODE_FILE_NOT_ALLOWLISTED', 'CODE_VOLUME_LIMIT', 'CODE_CONTRACT_INCOMPLETE',
}

class RollbackError(ValueError):
    pass

class StateError(ValueError):
    pass


def patch_sections(text: str) -> dict[str, list[str]]:
    sections = {}
    current = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith('--- a/'):
            current = line[6:]
            sections[current] = []
            in_hunk = False
        elif line.startswith('+++ b/'):
            assert line[6:] == current
        elif line.startswith('@@ '):
            in_hunk = True
        elif in_hunk and line.startswith('+'):
            sections[current].append(line[1:])
    return sections


def load_exact_patch_logic():
    blob = PATCH.read_bytes()
    assert len(blob) == 5213, len(blob)
    assert hashlib.sha256(blob).hexdigest() == SHA256
    parts = patch_sections(blob.decode('utf8'))
    assert len(parts) == 4
    segment = '\n'.join(parts['scripts/desktop_orchestrator_protected_state.py'])
    # Preserve the exact code from the patch and isolate only the two new definitions.
    begin = segment.index('SAFE_ROLLBACK_BLOCKERS = frozenset({')
    end = segment.index('    return "UNEXPECTED_FILE_ERROR"', begin) + len('    return "UNEXPECTED_FILE_ERROR"')
    tree = ast.parse(segment[begin:end])
    target = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SAFE_ROLLBACK_BLOCKERS' for t in node.targets):
            target.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == 'safe_blocker_code':
            target.append(node)
    assert len(target) == 2
    code = compile(ast.fix_missing_locations(ast.Module(body=target, type_ignores=[])), '<actual-github-patch-logic>', 'exec')
    env = {'RollbackError': RollbackError, 'StateError': StateError}
    exec(code, env)
    return env['safe_blocker_code'], env['SAFE_ROLLBACK_BLOCKERS'], parts


def test_exact_patch_semantics():
    func, allowed, parts = load_exact_patch_logic()
    assert set(allowed) == EXPECTED_GUARD_CODES
    # Reproduce the original one-line behavior directly from the removed patch line.
    source = PATCH.read_text(encoding='utf-8')
    removed = [line[1:].strip() for line in source.splitlines()
               if line.startswith('-        outcome["blocker_code"] = ')]
    assert len(removed) == 1
    for code in EXPECTED_GUARD_CODES:
        scope = {'failure': RollbackError(code), 'StateError': StateError,
                 'outcome': {}}
        exec(compile(removed[0], '<old-real-source-line>', 'exec'), scope)
        assert scope['outcome']['blocker_code'] == 'UNEXPECTED_FILE_ERROR'
    # Exact typed known cases.
    for code in EXPECTED_GUARD_CODES:
        assert func(RollbackError(code)) == code
    # Unknown, user-shaped or secret-like input never crosses boundary.
    for text in ['TOKEN=abc', 'C:\\Users\\user\\AppData\\secret.json',
                 'FIXED_PATH_MISSING\nJWT=abc', '', 'fixed_path_missing',
                 'FIXED_PATH_MISSING ', 'CODE_FILE_READ_FAILED: /private/key',
                 'FIXED_PATH_MISSING\x00', 'UNEXPECTED_FILE_ERROR']:
        assert func(RollbackError(text)) == 'UNEXPECTED_FILE_ERROR'
    for text in EXPECTED_GUARD_CODES:
        assert func(ValueError(text)) == 'UNEXPECTED_FILE_ERROR'
        assert func(OSError(text)) == 'UNEXPECTED_FILE_ERROR'
    class DerivedRollbackError(RollbackError):
        pass
    for code in EXPECTED_GUARD_CODES:
        assert func(DerivedRollbackError(code)) == 'UNEXPECTED_FILE_ERROR'
    # Existing StateError path unchanged (the downstream workflow has a separate exact allowlist).
    assert func(StateError('DPAPI_WINDOWS_REQUIRED')) == 'DPAPI_WINDOWS_REQUIRED'
    # Confirm workflow's literal whitelist has all exact codes; no regex matching introduced.
    wf = '\n'.join(parts['.github/workflows/desktop-orchestrator-sequential-recovery-readback.yml'])
    for code in EXPECTED_GUARD_CODES:
        assert "'" + code + "'," in wf
    assert not any(x in wf for x in ('-match', '-like', 'regex'))
    return {
        'patch_sha256': SHA256,
        'old_generic_behavior_reproductions': 12,
        'positive_cases': 12,
        'unknown_input_negative_cases': 9,
        'generic_exception_negative_cases': 24,
        'subclass_negative_cases': 12,
        'existing_state_error_cases': 1,
        'workflow_literally_whitelisted_codes': 12,
        'status': 'ISOLATED_PYTHON_ONLY_NO_INDEPENDENT_VALIDATOR',
    }

if __name__ == '__main__':
    import json
    print(json.dumps(test_exact_patch_semantics(), ensure_ascii=False, sort_keys=True))
