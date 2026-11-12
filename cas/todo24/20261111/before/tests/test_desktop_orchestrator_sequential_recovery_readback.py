"""Owner-only admission and structural regression tests for sequential DEV restore.

These tests never dispatch a workflow, touch the Desktop or upload evidence.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/desktop-orchestrator-sequential-recovery-readback.yml"
MAIN = "a" * 40
RULES = "58dcd4200f412a866278027ba9e433439a03f3b3"

NODE = r"""
const fs = require('node:fs');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const isComment = p.event === 'issue_comment';
const commentId = p.comment_id ?? 6759990011;
const commentAuthor = p.comment_author ?? 'ericson-j-santos';
const body = p.body ?? ('/todo24 verify-backups main=' + p.main +
  ' rules=' + p.rules + ' request=' + (p.request_id ?? 'TODO24-20261110-01'));
process.env.GITHUB_RUN_ATTEMPT = String(p.attempt ?? 1);
const context = {
  eventName: p.event ?? 'workflow_dispatch',
  actor: p.actor ?? 'ericson-j-santos',
  repo: { owner: p.owner ?? 'ericson-j-santos',
          repo: p.repo ?? 'desktop-pc24x7-runtime' },
  ref: p.ref ?? 'refs/heads/main',
  sha: p.sha ?? p.main,
  payload: isComment ? {
    action: p.activity ?? 'created',
    issue: { number: p.issue_number ?? 31,
             pull_request: p.is_pr ? {url: 'https://example.invalid/pull/1'} : null },
    comment: {id: commentId, body, user: {login: commentAuthor}}
  } : {inputs: {
    expected_head_sha: p.expected_head_sha ?? p.main,
    expected_rules_sha: p.expected_rules_sha ?? p.rules,
    action: p.action ?? 'VERIFY-DESKTOP-RECOVERY-SEQUENTIAL-DEV'
  }}
};
const comments = p.comments ?? [{
  id: commentId, body, user: {login: commentAuthor}
}];
const github = {rest: {
  repos: {
    get: async () => ({data: {private: p.private ?? false}}),
    getBranch: async ({repo}) => ({data: {commit: {sha:
      repo === 'chatgpt-operational-rules' ? (p.rules_head ?? p.rules)
        : (p.main_head ?? p.main)
    }}})
  },
  issues: {
    getComment: async () => ({data: {
      id: p.saved_id ?? commentId,
      body: p.saved_body ?? body,
      user: {login: p.saved_author ?? commentAuthor},
      created_at: '2026-11-10T20:00:00Z',
      updated_at: p.edited ? '2026-11-10T20:01:00Z' : '2026-11-10T20:00:00Z'
    }}),
    listComments: async () => comments
  }
}, paginate: async () => comments};
const core = {setOutput: (key, value) => {
  if (key === 'rules_sha' && value === p.rules) return;
  if (key === 'owner_comment_id' && value === String(commentId)) return;
  if (key === 'idempotency_key' &&
      value === (p.request_id ?? 'TODO24-20261110-01')) return;
  throw new Error('BAD_OUTPUT:' + key);
}};
(async () => {
  try {
    await new AsyncFunction('context', 'github', 'core', p.script)(context, github, core);
    process.stdout.write('ADMITTED');
  } catch (error) {
    process.stdout.write(String(error.message || error));
    process.exitCode = 7;
  }
})();
"""


def ler() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def executar_porteiro(**extras) -> tuple[int, str]:
    script = ler()["jobs"]["admission"]["steps"][0]["with"]["script"]
    payload = dict(script=script, main=MAIN, rules=RULES)
    payload.update(extras)
    run = subprocess.run(
        ["node", "-e", NODE], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, check=False,
    )
    return run.returncode, run.stdout


def test_acao_proprietario_main_publica_passou():
    assert executar_porteiro() == (0, "ADMITTED")


@pytest.mark.parametrize("alteracao,erro", [
    ({"event": "push"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"event": "issues"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"actor": "outro"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"owner": "outro"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"repo": "outro"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"ref": "refs/heads/feature"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"action": "INSTALL-RUNTIME"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"sha": "b" * 40}, "SEQUENTIAL_EXPECTED_SHA_INVALID"),
    ({"expected_head_sha": "invalido"}, "SEQUENTIAL_EXPECTED_SHA_INVALID"),
    ({"expected_rules_sha": "invalido"}, "SEQUENTIAL_EXPECTED_SHA_INVALID"),
    ({"private": True}, "SEQUENTIAL_COST_PUBLIC_REQUIRED"),
    ({"main_head": "b" * 40}, "SEQUENTIAL_MAIN_MOVED"),
    ({"rules_head": "b" * 40}, "SEQUENTIAL_RULES_SHA_MOVED"),
    ({"expected_rules_sha": "b" * 40}, "SEQUENTIAL_RULES_SHA_MOVED"),
])
def test_porteiro_rejeita_entradas_indevidas(alteracao, erro):
    status, retorno = executar_porteiro(**alteracao)
    assert status == 7
    assert retorno == erro


def test_esquema_controles_positivos_e_custo_zero():
    doc = ler()
    events = doc.get("on", doc.get(True))  # YAML 1.1 transforma "on" em True.
    assert set(events) == {"workflow_dispatch", "issue_comment"}
    assert events["issue_comment"]["types"] == ["created"]
    assert doc["permissions"] == {"contents": "read", "issues": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False
    entrada = events["workflow_dispatch"]["inputs"]
    assert set(entrada["action"]["options"]) == {
        "VERIFY-DESKTOP-RECOVERY-SEQUENTIAL-DEV"}
    assert entrada["expected_head_sha"]["required"]
    assert entrada["expected_rules_sha"]["required"]
    assert doc["jobs"]["admission"]["runs-on"] == "ubuntu-latest"
    physical = doc["jobs"]["physical"]
    assert physical["needs"] == "admission"
    assert physical["runs-on"] == [
        "self-hosted", "Windows", "X64", "pc24x7", "desktop-runtime",
        "runtime-dev", "desktop-pdqk954",
    ]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "SEQUENTIAL_COST_PUBLIC_REQUIRED" in text
    assert "SESSION_LAUNCH_OK" in text and "state_validated" in text
    assert "--require-runner-version-preflight" in text
    assert "--sync-ref', 'origin/main'" in text
    assert "command_gateway.py" in text
    assert "'--risk', '2'" in text
    assert "SEQUENTIAL_RECOVERY_READBACK_VERIFIED" in text
    assert "upload-artifact" not in text
    assert "docker " not in text.lower()


def test_probes_em_ordem_sem_bypass_e_sem_recuperar_servico_vivo():
    doc = ler()
    steps = doc["jobs"]["physical"]["steps"]
    assert len(steps) == 4
    assert steps[2]["id"] == "session"
    runner = steps[3]["run"]
    isolated = runner.index("$isolated = Invoke-ApprovedProbe ")
    first = runner.index("$first = Invoke-ApprovedProbe ")
    second = runner.index("$second = Invoke-ApprovedProbe ")
    shadow = runner.index("$shadow = Invoke-ApprovedProbe ")
    assert isolated < first < second < shadow
    assert "SEQUENTIAL_ISOLATED_RESTORE_REJECTED" in runner
    assert "SEQUENTIAL_PROTECTED_STATE_REJECTED" in runner
    assert "SEQUENTIAL_REPLAY_REJECTED" in runner
    assert "SEQUENTIAL_SHADOW_REJECTED" in runner
    assert "source_sha -ne $env:GITHUB_SHA" in runner
    assert "recovery_allowed -ne $false" in runner
    assert "production_touched -ne $false" in runner
    assert "live_service_restarted -ne $false" in runner
    assert "worker_started -ne $false" in runner
    assert "snapshot_manifest_sha256 -ne $second.snapshot_manifest_sha256" in runner


def test_owner_issue_comment_triggers_validated_automatic_sequence():
    assert executar_porteiro(event="issue_comment") == (0, "ADMITTED")


@pytest.mark.parametrize("alteracao,erro", [
    ({"event": "issue_comment", "actor": "invasor"}, "SEQUENTIAL_OWNER_MAIN_SCOPE_DENIED"),
    ({"event": "issue_comment", "issue_number": 5}, "SEQUENTIAL_ISSUE_COMMENT_DENIED"),
    ({"event": "issue_comment", "is_pr": True}, "SEQUENTIAL_ISSUE_COMMENT_DENIED"),
    ({"event": "issue_comment", "activity": "edited"}, "SEQUENTIAL_ISSUE_COMMENT_DENIED"),
    ({"event": "issue_comment", "comment_author": "outro"}, "SEQUENTIAL_ISSUE_COMMENT_DENIED"),
    ({"event": "issue_comment", "attempt": 2}, "SEQUENTIAL_ISSUE_COMMENT_DENIED"),
    ({"event": "issue_comment", "body": "install now"}, "SEQUENTIAL_COMMAND_INVALID"),
    ({"event": "issue_comment", "body": "/todo24 verify-backups"}, "SEQUENTIAL_COMMAND_INVALID"),
    ({"event": "issue_comment", "saved_body": "alterado"}, "SEQUENTIAL_COMMENT_CHANGED"),
    ({"event": "issue_comment", "saved_author": "outro"}, "SEQUENTIAL_COMMENT_CHANGED"),
    ({"event": "issue_comment", "saved_id": 1}, "SEQUENTIAL_COMMENT_CHANGED"),
    ({"event": "issue_comment", "edited": True}, "SEQUENTIAL_COMMENT_CHANGED"),
    ({"event": "issue_comment", "comments": None, "main_head": "b" * 40}, "SEQUENTIAL_MAIN_MOVED"),
    ({"event": "issue_comment", "rules_head": "b" * 40}, "SEQUENTIAL_RULES_SHA_MOVED"),
    ({"event": "issue_comment", "private": True}, "SEQUENTIAL_COST_PUBLIC_REQUIRED"),
    ({"event": "issue_comment", "comments": [
        {"id": 6759990000, "body": (
          "/todo24 verify-backups main=" + MAIN +
          " rules=" + RULES + " request=TODO24-20261110-01"),
          "user": {"login": "ericson-j-santos"}},
        {"id": 6759990011, "body": (
          "/todo24 verify-backups main=" + MAIN +
          " rules=" + RULES + " request=TODO24-20261110-01"),
          "user": {"login": "ericson-j-santos"}},
    ]}, "SEQUENTIAL_DUPLICATE_REQUEST_DENIED"),
])
def test_issue_comment_negative_controls(alteracao, erro):
    rc, output = executar_porteiro(**alteracao)
    assert rc == 7, output
    assert output == erro


def test_unrelated_issue_comments_never_allocate_hosted_job():
    doc = ler()
    admission = doc["jobs"]["admission"]
    guard = admission["if"]
    for phrase in (
        "github.event.issue.number == 31",
        "github.actor == 'ericson-j-santos'",
        "github.event.action == 'created'",
        "!github.event.issue.pull_request",
        "startsWith(github.event.comment.body, '/todo24 verify-backups ')",
    ):
        assert phrase in guard
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "SEQUENTIAL_DUPLICATE_REQUEST_DENIED" in source
    assert "SEQUENTIAL_COMMENT_CHANGED" in source
    assert "GITHUB_RUN_ATTEMPT" in source
    assert "listComments" in source
    assert "getComment" in source


def test_sanitized_gateway_diagnostics_use_only_a_fixed_allowlist():
    """Untrusted error strings must never be echoed as GitHub Actions logs."""
    import re

    source = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"\$approvedBlockers = @\((.*?)\)\s+if \(\$proofFailed\.blocker_code",
        source, flags=re.DOTALL,
    )
    assert match is not None, "Missing fail-closed diagnostic allowlist"
    codes = re.findall(r"'([A-Z][A-Z0-9_]+)'", match.group(1))
    assert codes and len(codes) == len(set(codes))
    assert "CODE_BACKUP_MISMATCH" in codes
    assert "SQLITE_SNAPSHOT_FAILED" in codes
    assert "SHADOW_READINESS_TIMEOUT" in codes
    assert "GATEWAY_OR_PROBE_BLOCKED" not in codes
    assert "$approvedBlockers -ccontains $proofFailed.blocker_code" in source
    assert "$proofFailed.blocker_code -is [string]" in source
    assert "$proofFailed.blocker_code -cmatch" not in source
    for prohibited in ("PRIVATE_FIELD", "PASSWORD", "TOKEN", "SECRET", "API_KEY"):
        assert prohibited not in codes


def test_protected_state_fixed_blockers_are_all_safely_publishable():
    """Keep physical state failures diagnosable without echoing arbitrary text."""
    import re

    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"\$approvedBlockers = @\((.*?)\)\s+if \(\$proofFailed\.blocker_code",
        workflow, flags=re.DOTALL,
    )
    assert match is not None
    approved = set(re.findall(r"'([A-Z][A-Z0-9_]+)'", match.group(1)))

    protected = ROOT / "scripts/desktop_orchestrator_protected_state.py"
    tree = ast.parse(protected.read_text(encoding="utf-8"))
    emitted = {"UNEXPECTED_FILE_ERROR"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "StateError" or not node.args:
            continue
        for literal in ast.walk(node.args[0]):
            if (isinstance(literal, ast.Constant) and
                    isinstance(literal.value, str) and
                    re.fullmatch(r"[A-Z][A-Z0-9_]+", literal.value)):
                emitted.add(literal.value)

    assert emitted <= approved, sorted(emitted - approved)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Native PowerShell is Windows-only")
def test_native_powershell_allowlist_rejects_secret_shaped_and_case_variant_strings():
    """Execute the exact literal allowlist parsed from the workflow in PowerShell."""
    import re
    import subprocess

    source = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(\$approvedBlockers = @\(.*?\))\s+if \(\$proofFailed\.blocker_code",
        source, flags=re.DOTALL,
    )
    assert match is not None
    script = (
        "$ErrorActionPreference = 'Stop';\n" + match.group(1) + "\n"
        "foreach ($value in @('CODE_BACKUP_MISMATCH','SQLITE_SNAPSHOT_FAILED',"
        "'TOKEN_ABC123XYZ','SECRET_12345','code_backup_mismatch','')) {\n"
        "  if ($approvedBlockers -ccontains $value) { Write-Output 'ACCEPT' }"
        "  else { Write-Output 'REJECT' }\n"
        "}\n"
    )
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert run.returncode == 0, run.stderr[:300]
    assert run.stdout.strip().splitlines() == [
        "ACCEPT", "ACCEPT", "REJECT", "REJECT", "REJECT", "REJECT",
    ]
