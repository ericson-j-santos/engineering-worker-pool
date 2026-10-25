"""Static fail-closed gate for an inert exact-SHA Neon function package."""
from pathlib import Path

F = (Path(__file__).resolve().parents[1] /
     ".github/workflows/continuity-oidc-neon-dev-bundle.yml")


def test_source_sha_and_no_credential_or_deploy_controls():
    text = F.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in text
    assert "persist-credentials: false" in text
    assert "EXPECTED_SHA: ${{ github.event.pull_request.head.sha }}" in text
    assert "fix/continuity-oidc-rejection-diagnostics-20261025" in text
    assert "npm ci --prefix services/continuity_identity" in text
    assert "npx --yes esbuild@0.25.12" in text
    assert "node --test services/continuity_identity/identity.test.mjs" in text
    assert "node --test services/continuity_identity/dist/identity.test.mjs" in text
    assert "SOURCE_COMMIT.txt" in text
    for forbidden in ("contents: write", "id-token: write", "secrets.",
                      "neon deploy", "deploy_function", "pull_request_target:"):
        assert forbidden not in text


def test_package_verifies_bundle_identity_and_denial_semantics():
    text = F.read_text(encoding="utf-8")
    assert 'assert token in app' in text
    assert 'assert forbidden not in app' in text
    assert 'assert zf.read("index.mjs")==app' in text
    assert "NEON_BROKER_ZIP_SHA256=" in text
    assert "NEON_BROKER_BASE64=" in text
    assert "NEON_BROKER_SOURCE_SHA=" in text



def test_optional_live_dev_smoke_requires_owner_edit_and_remains_unprivileged():
    text = F.read_text(encoding="utf-8")
    assert "OWNER_OIDC_DEV_SMOKE_20261025_ONCE" in text
    assert "github.event.action == 'edited'" in text
    assert "github.actor == 'ericson-j-santos'" in text
    assert "!contains(github.event.changes.body.from" in text
    assert "needs.bundle.result == 'success'" in text
    assert "OIDC_DEV_SMOKE_SOURCE_DRIFT" in text
    assert "br-gentle-river-b4yz6b44-continuityidentity.compute.c-6.us-east-2.aws.neon.tech" in text
    assert "OIDC_DEV_E2E_PASS: health=200 unknown=404 unsigned=401 no_write=true" in text
    assert "contents: write" not in text
    assert "id-token: write" not in text
    assert "secrets." not in text
