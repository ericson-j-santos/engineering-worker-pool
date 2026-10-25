"""Native issue Builder/Validator: real Git commits, synthetic model only.

The actual Qwen7B/Worker Pool leased E2E is not simulated as physical proof.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.issue_draft_pr import IssueBinding, PATH, PROFILE, REPO, HandoffBlocked, sha256
from app.issue_native_adapter import DESCRIPTOR, IssueQwenNativeAdapter
from app.native_one_shot import AdapterContext, TaskManifest
from app.structured_code_worker import StructuredBlocked, git
from test_structured_code_worker import FIXED


TASK_ID = "cwp-" + "a" * 24
ORIGINAL = Path(__file__).resolve().parents[1] / PATH


def create_checkout(tmp_path):
    root = tmp_path / "builder"
    (root / "app").mkdir(parents=True)
    before = ORIGINAL.read_bytes()
    (root / PATH).write_bytes(before)
    git(root, "init", "-b", "worker/issue-8")
    git(root, "-c", "core.autocrlf=false", "add", "--", PATH)
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "baseline")
    base = git(root, "rev-parse", "HEAD")
    binding = IssueBinding(
        number=8, base_sha=base, comment_id=321,
        request_id="issue-" + "a" * 64,
        correlation_id="qwen7b-issue-8-" + base[:12],
        branch="agent/qwen7b-issue-8-" + base[:12],
    )
    return root, before, binding


def manifest(binding, before, *, produced=None):
    return TaskManifest.parse({
        "schema_version": 1, "task_id": TASK_ID,
        "repository": REPO, "base_sha": binding.base_sha,
        "workspace_sha": produced or binding.base_sha,
        "adapter_id": PROFILE,
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {PATH: sha256(before)},
    })


def candidate(_proposal, _source):
    return json.dumps({"function": FIXED})


def regression(_root):
    return {"exit_code": 0, "category": "passed",
            "output_within_limit": True,
            "passed_count": 52, "failed_count": 0, "error_count": 0}


def adapter(binding, before, tmp_path, *, model=candidate, checker=regression,
            native=False):
    return IssueQwenNativeAdapter(
        binding=binding, task_id=TASK_ID, before=before,
        state_directory=tmp_path / "issue-repair-state",
        propose=model, regression_runner=checker,
        require_native_model=native,
    )


def context(root, m, instance, role):
    return AdapterContext(root, m, DESCRIPTOR, instance.commands(root), role)


def test_issue_bound_real_git_builder_and_distinct_validator(tmp_path):
    builder, before, binding = create_checkout(tmp_path)
    instance = adapter(binding, before, tmp_path)
    ctx = context(builder, manifest(binding, before), instance, "builder")
    result = instance.build(ctx)
    assert result.produced_sha != binding.base_sha
    assert result.changed_paths == (PATH,)
    assert result.invoked_commands == ("issue.repair",)
    assert re.fullmatch(r"[0-9a-f]{64}", result.evidence_sha256)
    assert git(builder, "rev-parse", "HEAD") == result.produced_sha
    assert git(builder, "diff", "--name-only", binding.base_sha,
               result.produced_sha) == PATH
    assert git(builder, "status", "--porcelain") == ""

    separate = tmp_path / "validator"
    git(tmp_path, "clone", "--no-hardlinks", str(builder), str(separate))
    git(separate, "remote", "remove", "origin")
    git(separate, "checkout", "--detach", result.produced_sha)
    after = (separate / PATH).read_bytes()
    validator_context = context(
        separate, manifest(binding, after, produced=result.produced_sha),
        instance, "validator",
    )
    done = instance.validate(validator_context, result.produced_sha)
    assert done.produced_sha == result.produced_sha
    assert done.invoked_commands == ("issue.validate",)
    assert git(separate, "status", "--porcelain") == ""
    assert re.fullmatch(r"[0-9a-f]{64}", done.evidence_sha256)


def test_non_native_candidate_is_never_admitted_by_default(tmp_path):
    _, before, binding = create_checkout(tmp_path)
    with pytest.raises(HandoffBlocked, match="issue_native_model_required"):
        adapter(binding, before, tmp_path, native=True)


@pytest.mark.parametrize("variant", ["wrong_base", "wrong_repo", "wrong_task"])
def test_manifest_drift_never_invokes_model(tmp_path, variant):
    root, before, binding = create_checkout(tmp_path)
    calls = []
    def uncalled(*_args):
        calls.append(1)
        raise AssertionError("model must not run")
    instance = adapter(binding, before, tmp_path, model=uncalled)
    raw = {
        "schema_version": 1, "task_id": TASK_ID,
        "repository": REPO, "base_sha": binding.base_sha,
        "workspace_sha": binding.base_sha,
        "adapter_id": PROFILE,
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {PATH: sha256(before)},
    }
    if variant == "wrong_base":
        raw["base_sha"] = "e" * 40
    elif variant == "wrong_repo":
        raw["repository"] = "untrusted/other"
    else:
        raw["task_id"] = "cwp-" + "b" * 24
    ctx = context(root, TaskManifest.parse(raw), instance, "builder")
    with pytest.raises(HandoffBlocked, match="issue_native_manifest_untrusted"):
        instance.build(ctx)
    assert calls == []


def test_failed_regressions_block_validator(tmp_path):
    root, before, binding = create_checkout(tmp_path)
    instance = adapter(binding, before, tmp_path,
                       checker=lambda _: {"exit_code": 0, "passed_count": 51})
    built = instance.build(
        context(root, manifest(binding, before), instance, "builder")
    )
    after = (root / PATH).read_bytes()
    m = manifest(binding, after, produced=built.produced_sha)
    val = context(root, m, instance, "validator")
    with pytest.raises(HandoffBlocked, match="issue_native_regressions_unproved"):
        instance.validate(val, built.produced_sha)


def test_bad_model_candidate_stops_without_commit(tmp_path):
    root, before, binding = create_checkout(tmp_path)
    instance = adapter(
        binding, before, tmp_path,
        model=lambda *_: json.dumps({"function": "def _property(properties, name):\n    return ''\n"}),
    )
    with pytest.raises(StructuredBlocked, match="candidate_tests_failed"):
        instance.build(context(root, manifest(binding, before), instance, "builder"))
    assert git(root, "rev-parse", "HEAD") == binding.base_sha
    assert (root / PATH).read_bytes() == before
    assert git(root, "status", "--porcelain") == ""
