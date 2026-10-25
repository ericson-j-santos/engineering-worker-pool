"""Issue -> bounded Qwen -> draft PR policy tests (synthetic GitHub port)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import issue_draft_pr as gate
from app.structured_code_worker import render
from test_structured_code_worker import FIXED

NOW = datetime(2026, 10, 25, 4, 10, tzinfo=timezone.utc)
BASE = "2" * 40
DIGEST = gate.MODEL_DIGEST_PREFIX + "a" * (64 - len(gate.MODEL_DIGEST_PREFIX))


def snapshots(*, number=8, base=BASE):
    issue = {
        "number": number, "state": "open",
        "html_url": f"https://github.com/{gate.REPO}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{gate.REPO}",
    }
    comment = {
        "id": 8721, "user": {"login": gate.OWNER, "type": "User"},
        "author_association": "OWNER",
        "issue_url": f"https://api.github.com/repos/{gate.REPO}/issues/{number}",
        "body": f"/local-agent authorize {gate.PROFILE} sha:{base}",
    }
    branch = {"name": "main", "commit": {"sha": base}}
    return issue, comment, branch


def admitted():
    return gate.admit(*snapshots(), base_sha=BASE, now=NOW,
                      observed_at=NOW.isoformat())


@pytest.fixture
def source_pair():
    before = (Path(__file__).resolve().parents[1] / gate.PATH).read_bytes()
    task = gate.trusted_task(admitted(), before)
    after = render(before.decode(), task, __import__("json").dumps({"function": FIXED}))
    return before, after.encode()


def checker(after, produced):
    return {
        "independent_checkout": True, "source_sha256": gate.sha256(after),
        "produced_sha": produced, "collected": 52,
        "passed": 52, "failed": 0, "errors": 0,
    }


def verified(binding, before, after, validator=checker):
    return gate.verify_patch(
        binding, before, after, model=gate.MODEL,
        model_digest=DIGEST, model_calls=1, produced_sha="c" * 40,
        regression_checker=validator,
    )


def test_trusted_issue_admission_is_deterministic():
    x = admitted()
    y = admitted()
    assert x == y
    assert x.number == 8 and x.base_sha == BASE
    assert x.branch == "agent/qwen7b-issue-8-" + BASE[:12]
    assert x.request_id.startswith("issue-") and len(x.request_id) == 70


@pytest.mark.parametrize("kind,code", [
    ("closed", "issue_identity_invalid"),
    ("wrong_repo", "issue_identity_invalid"),
    ("pr", "issue_identity_invalid"),
    ("missing_repository", "issue_identity_invalid"),
    ("other_user", "owner_authorization_missing"),
    ("other_association", "owner_authorization_missing"),
    ("modified_command", "owner_authorization_missing"),
    ("comment_cross_issue", "owner_authorization_missing"),
    ("head_changed", "main_head_drift"),
    ("wrong_branch", "main_head_drift"),
    ("stale", "github_observation_stale_or_future"),
    ("future", "github_observation_stale_or_future"),
])
def test_untrusted_issue_fails_closed(kind, code):
    issue, comment, branch = snapshots()
    observed = NOW
    if kind == "closed":
        issue["state"] = "closed"
    elif kind == "wrong_repo":
        issue["html_url"] = "https://github.com/outsider/repo/issues/8"
    elif kind == "pr":
        issue["pull_request"] = {"url": "https://api.github.com/other"}
    elif kind == "missing_repository":
        del issue["repository_url"]
    elif kind == "other_user":
        comment["user"]["login"] = "attacker"
    elif kind == "other_association":
        comment["author_association"] = "CONTRIBUTOR"
    elif kind == "modified_command":
        comment["body"] += " && rm -rf ."
    elif kind == "comment_cross_issue":
        comment["issue_url"] += "9"
    elif kind == "head_changed":
        branch["commit"]["sha"] = "f" * 40
    elif kind == "wrong_branch":
        branch["name"] = "DEV"
    elif kind == "stale":
        observed -= timedelta(minutes=6)
    else:
        observed += timedelta(seconds=1)
    with pytest.raises(gate.HandoffBlocked, match="^" + code + "$"):
        gate.admit(issue, comment, branch, base_sha=BASE,
                   observed_at=observed.isoformat(), now=NOW)


def test_real_bounded_source_cannot_be_expanded(source_pair):
    before, after = source_pair
    actual = verified(admitted(), before, after)
    assert actual["accepted_cases"] == 42
    assert actual["baseline_passed"] < 42
    with pytest.raises(gate.HandoffBlocked, match="candidate_scope_changed"):
        verified(admitted(), before, after + b"\nUNAUTHORIZED = True\n")


@pytest.mark.parametrize("mutator,reason", [
    (lambda x: x.update(independent_checkout=False), "independent_regression_unproved"),
    (lambda x: x.update(passed=51), "independent_regression_unproved"),
    (lambda x: x.update(source_sha256="0" * 64), "independent_regression_unproved"),
    (lambda x: x.update(produced_sha="a" * 40), "independent_regression_unproved"),
])
def test_regression_attestation_required(source_pair, mutator, reason):
    before, after = source_pair
    def invalid(candidate, sha):
        receipt = checker(candidate, sha)
        mutator(receipt)
        return receipt
    with pytest.raises(gate.HandoffBlocked, match=reason):
        verified(admitted(), before, after, invalid)


def test_model_identity_rejected_before_validator(source_pair):
    before, after = source_pair
    for value in ("other-model", "", "qwen2.5-coder:1.5b"):
        with pytest.raises(gate.HandoffBlocked, match="model_identity_unproved"):
            gate.verify_patch(admitted(), before, after, model=value,
                              model_digest=DIGEST, model_calls=1,
                              produced_sha="c" * 40,
                              regression_checker=lambda *_: pytest.fail("validator called"))


class FakeOfficialGitHub:
    """Stateful stand-in; NOT a GitHub API E2E or real model invocation."""
    def __init__(self, binding, before):
        self.binding = binding
        self.issue, self.comment, self.main = snapshots()
        self.source = before
        self.branch_sha = None
        self.candidate = None
        self.pr = None
        self.changed_files = (gate.PATH,)
        self.effects = []

    def get_issue(self, number):
        assert number == self.binding.number
        return self.issue

    def get_comment(self, comment_id):
        assert comment_id == self.binding.comment_id
        return self.comment

    def get_branch(self, name):
        if name == "main":
            return self.main
        if name == self.binding.branch and self.branch_sha:
            return {"name": name, "commit": {"sha": self.branch_sha}}
        return None

    def get_source(self, ref):
        if ref == self.binding.base_sha:
            return self.source, "1" * 40
        if ref == self.binding.branch and self.branch_sha:
            return (self.candidate if self.candidate is not None else self.source,
                    "3" * 40 if self.candidate is not None else "1" * 40)
        raise AssertionError("unexpected source request")

    def changed_paths(self, base_sha, head_sha):
        assert base_sha == self.binding.base_sha and head_sha == self.branch_sha
        return self.changed_files

    def create_branch(self, name, sha):
        assert name == self.binding.branch and sha == self.binding.base_sha
        assert self.branch_sha is None
        self.effects.append("create_branch")
        self.branch_sha = sha

    def replace_source(self, name, content, blob_sha):
        assert name == self.binding.branch and blob_sha == "1" * 40
        assert self.branch_sha == self.binding.base_sha
        self.effects.append("replace_source")
        self.candidate = content
        self.branch_sha = "d" * 40

    def find_pr(self, head):
        assert head == self.binding.branch
        return self.pr

    def create_pr(self, head, title, body):
        assert head == self.binding.branch and self.pr is None
        self.effects.append("create_pr")
        self.pr = {"number": 46, "state": "open", "draft": True,
                   "head_sha": self.branch_sha, "base_ref": "main", "body": body}


def test_publish_draft_real_policy_idempotency(source_pair):
    before, after = source_pair
    binding = admitted()
    audit = verified(binding, before, after)
    api = FakeOfficialGitHub(binding, before)
    first = gate.publish_draft(api, binding, before, after, audit,
                               now=NOW, observed_at=NOW.isoformat(), regression_checker=checker)
    assert first["result"] == "DRAFT_PR_VERIFIED"
    assert first["merge_performed"] is False and first["replayed"] is False
    assert api.effects == ["create_branch", "replace_source", "create_pr"]
    replay = gate.publish_draft(api, binding, before, after, audit,
                                now=NOW, observed_at=NOW.isoformat(), regression_checker=checker)
    assert replay["pr_number"] == 46 and replay["replayed"] is True
    assert api.effects == ["create_branch", "replace_source", "create_pr"]
    assert gate.MARKER in api.pr["body"]


def test_branch_collision_does_not_overwrite(source_pair):
    before, after = source_pair
    binding = admitted()
    api = FakeOfficialGitHub(binding, before)
    api.branch_sha = "e" * 40
    audit = verified(binding, before, after)
    with pytest.raises(gate.HandoffBlocked, match="branch_collision_or_divergence"):
        gate.publish_draft(api, binding, before, after, audit,
                           now=NOW, observed_at=NOW.isoformat(), regression_checker=checker)
    assert api.effects == []


def test_authorization_or_main_drift_blocks_publication(source_pair):
    before, after = source_pair
    binding = admitted()
    audit = verified(binding, before, after)
    for reason in ("closed", "head_drift", "approval_revoked"):
        api = FakeOfficialGitHub(binding, before)
        if reason == "closed":
            api.issue["state"] = "closed"
        elif reason == "head_drift":
            api.main["commit"]["sha"] = "e" * 40
        else:
            api.comment["body"] = "/local-agent revoke"
        with pytest.raises(gate.HandoffBlocked):
            gate.publish_draft(api, binding, before, after, audit,
                               now=NOW, observed_at=NOW.isoformat(), regression_checker=checker)
        assert api.effects == []


def test_recovery_after_branch_create_before_write(source_pair):
    before, after = source_pair
    binding = admitted()
    audit = verified(binding, before, after)
    api = FakeOfficialGitHub(binding, before)
    api.create_branch(binding.branch, binding.base_sha)  # prior interrupted attempt
    out = gate.publish_draft(api, binding, before, after, audit,
                             now=NOW, observed_at=NOW.isoformat(), regression_checker=checker)
    assert out["result"] == "DRAFT_PR_VERIFIED"
    assert api.effects == ["create_branch", "replace_source", "create_pr"]


def test_forged_model_report_cannot_publish(source_pair):
    before, after = source_pair
    binding = admitted()
    correct = verified(binding, before, after)
    for forged in (
        {**correct, "regressions_passed": 999},
        {**correct, "model_digest": "f" * 64},
        {**correct, "source_sha256": "f" * 64},
    ):
        api = FakeOfficialGitHub(binding, before)
        with pytest.raises(gate.HandoffBlocked):
            gate.publish_draft(api, binding, before, after, forged,
                               now=NOW, observed_at=NOW.isoformat(),
                               regression_checker=checker)
        assert api.effects == []


def test_external_diff_exceeds_scope_blocks_pr(source_pair):
    before, after = source_pair
    binding = admitted()
    audit = verified(binding, before, after)
    api = FakeOfficialGitHub(binding, before)
    api.changed_files = (gate.PATH, "tests/test_portfolio_bridge.py")
    with pytest.raises(gate.HandoffBlocked, match="draft_branch_diff_scope_invalid"):
        gate.publish_draft(api, binding, before, after, audit,
                           now=NOW, observed_at=NOW.isoformat(),
                           regression_checker=checker)
    assert api.effects == ["create_branch", "replace_source"]
    assert api.pr is None  # Never publish a PR with unrelated files.


def test_unverified_regression_never_writes(source_pair):
    before, after = source_pair
    binding = admitted()
    audit = verified(binding, before, after)
    api = FakeOfficialGitHub(binding, before)
    with pytest.raises(gate.HandoffBlocked, match="independent_regression_unproved"):
        gate.publish_draft(api, binding, before, after, audit,
                           now=NOW, observed_at=NOW.isoformat(),
                           regression_checker=lambda *_: {"independent_checkout": False})
    assert api.effects == []
