from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuity-identity-bundle.yml"


def test_identity_bundle_workflow_is_narrow_pinned_and_non_deploying() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in content
    assert "push:" in content
    assert "branches: [main]" in content
    assert "workflow_dispatch:" in content
    assert '"services/continuity_identity/**"' in content
    assert '"scripts/build_continuity_identity.py"' in content

    assert "permissions:\n  contents: read" in content
    assert "runs-on: ubuntu-24.04" in content
    assert "timeout-minutes: 5" in content
    assert "persist-credentials: false" in content
    assert 'ref: ${{ env.EXPECTED_SHA }}' in content

    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in content
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in content
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in content
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in content

    assert 'node-version: "24"' in content
    assert 'python-version: "3.12"' in content
    assert "npm ci --ignore-scripts --no-audit --no-fund --prefix services/continuity_identity" in content
    assert "node --test services/continuity_identity/identity.test.mjs" in content
    assert "python scripts/build_continuity_identity.py" in content
    assert "IDENTITY_BUNDLE_VERIFIED" in content

    assert "if-no-files-found: error" in content
    assert "retention-days: 1" in content
    assert "compression-level: 0" in content
    assert "Secrets/configuration included: no" in content
    assert "Deployment performed: no" in content

    assert "secrets." not in content
    assert "deploy_function" not in content
    assert "EWP_RUNTIME_CONFIG_JSON" not in content
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in content
    assert "id-token: write" not in content
