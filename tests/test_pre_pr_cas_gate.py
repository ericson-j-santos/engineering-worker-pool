"""Policy-only tests: fake transports are NOT evidence of a real CAS/model/E2E."""
from __future__ import annotations

import copy
from dataclasses import dataclass

import pytest

from app.pre_pr_cas_gate import (
    PrePRBlocked, PrePRIntent, digest, publish_single_draft, ready_for_pr,
)

BASE = "a" * 40
HEAD = "b" * 40
SOURCE = "c" * 40
RUN = "model-run-verified-001"
KEY = "engineering-control-plane:noteri42:prepr-cas:515ede49"


@dataclass
class Fake:
    def __post_init__(self):
        self.intent = PrePRIntent(
            repository="ericson-j-santos/noteri-runtime", issue_number=42,
            todo_id="TODO-54-validation", request_id="noteri-request-0042",
            correlation_id="noteri-issue42-prepr-cas-route-20261107",
            idempotency_key=KEY, base_ref="main", base_sha=BASE,
            candidate_ref="fix/noteri-probe-rules-sha-20261106",
            candidate_sha=HEAD, source_sha=SOURCE,
            allowed_paths=("scripts/noteri_qualification_receipt.py",),
            task_id="cwp-0123456789abcdef01234567",
        )
        self.issue_state = "open"
        self.todo_state = "BLOCKED"
        self.authorized = True
        self.base = BASE
        self.head = HEAD
        self.task = {
            "task_id": self.intent.task_id, "repository": self.intent.repository,
            "issue_number": 42, "request_id": self.intent.request_id,
            "correlation_id": self.intent.correlation_id,
            "base_sha": BASE, "produced_sha": HEAD,
            "fencing_required": True, "leased_by": None,
            "state": "builder_completed", "lease_generation": 1,
            "fence_id": "fence-noteri001", "builder_id": "builder-model001",
            "model_run_id": RUN,
        }
        self.model = {
            "provider_verified": True, "run_id": RUN,
            "request_id": self.intent.request_id, "base_sha": BASE,
            "produced_sha": HEAD, "source_sha": SOURCE,
            "builder_id": "builder-model001", "model": "verified-model",
            "model_digest": "d" * 64, "call_count": 1,
            "baseline_failures": 3, "baseline_sha256": "e" * 64,
        }
        self.objects = {
            "git_bundle": b"GITBUNDLE\nOBJECT-1",
            "patch": b"diff --git a/file b/file\n+model repair\n",
            "before": b"failing original",
            "after": b"tested model repair",
        }
        self.writes = 0
        self.model_reads = 0
        self.validations = 0
        self.cas = {}
        self.decision = {}
        self.prs = []
        self.pr_reads = 0
        self.pr_writes = 0
        self.tamper = False
        self.validator_wrong_sha = False
        self.validator_skipped = False
        self.materialization_bad = False

    def issue(self, repository, number):
        return {"repository": repository, "number": number,
                "state": self.issue_state, "is_pull_request": False}

    def todo(self, todo_id):
        return {"id": todo_id, "repository": self.intent.repository,
                "issue_number": 42, "state": self.todo_state}

    def authorization(self, request_id):
        return {
            "authorized": self.authorized, "source": "trusted-control-plane",
            "repository": self.intent.repository, "issue_number": 42,
            "request_id": request_id, "idempotency_key": KEY,
            "base_sha": BASE, "candidate_sha": HEAD,
        }

    def branch_sha(self, repository, ref):
        return self.base if ref == "main" else self.head

    def get_task(self, task_id):
        return dict(self.task)

    def get_verified_run(self, run_id):
        self.model_reads += 1
        return dict(self.model)

    def export(self, intent):
        return dict(self.objects)

    def materialize_independently(self, raw, *, expected_sha):
        assert raw and expected_sha == HEAD
        d = {key + "_sha256": digest(value) for key, value
             in self.objects.items()}
        return {
            "checkout_isolated": not self.materialization_bad,
            "commit_verified": True, "source_integrity": True,
            "base_sha": BASE, "produced_sha": HEAD,
            "published_sha": HEAD, "source_sha": SOURCE,
            "changed_paths": list(self.intent.allowed_paths),
            "diff_nonempty": True, "hashes": d,
            "parent_is_base_ancestor": True,
        }

    def put_if_absent(self, digest_hex, raw, *, retention_days):
        assert retention_days >= 30
        self.writes += 1
        self.cas.setdefault(digest_hex, raw)
        return "cas://sha256/" + digest_hex

    def get_exact(self, ref):
        data = self.cas[ref.rsplit("/", 1)[-1]]
        return b"forged" if self.tamper else data

    def execute_independently(self, *, repository, sha, artifact_ref,
                              request_id):
        self.validations += 1
        return {
            "checkout_isolated": True,
            "model_calls": 0, "validator_id": "validator-independent001",
            "sha": BASE if self.validator_wrong_sha else HEAD,
            "source_sha": SOURCE,
            "before_sha256": digest(self.objects["before"]),
            "after_sha256": digest(self.objects["after"]),
            "baseline_sha256": self.model["baseline_sha256"],
            "baseline_failures": 3,
            "passed": 10, "total": 10,
            "regression_passed": 12, "regression_total": 12,
            "failed": 0, "errors": 0,
            "skipped": 1 if self.validator_skipped else 0,
        }

    def read(self, key):
        return copy.deepcopy(self.decision.get(key))

    def put_if_absent(self, key, decision):
        # This Fake uses one method for both CAS and ledger by argument form.
        if isinstance(decision, dict):
            self.decision.setdefault(key, copy.deepcopy(decision))
        else:
            raise AssertionError("ledger invalid")

    def find_open_prs(self, repository, branch):
        return copy.deepcopy(self.prs)

    def create_draft(self, *, repository, head, base, body):
        self.pr_writes += 1
        self.prs = [{"number": 123}]

    def read_pr(self, repository, number):
        self.pr_reads += 1
        return {
            "state": "open", "draft": True, "head_ref": self.intent.candidate_ref,
            "head_sha": HEAD, "base_ref": "main", "base_sha": BASE,
            "changed_paths": list(self.intent.allowed_paths),
            "diff_nonempty": True, "idempotency_key": KEY,
        }


# Distinct class prevents method collision between CAS and ledger fakes.
class FakeCas:
    def __init__(self, fake):
        self.fake = fake

    def put_if_absent(self, digest_hex, raw, *, retention_days):
        assert retention_days == 30
        self.fake.writes += 1
        self.fake.cas.setdefault(digest_hex, raw)
        return "cas://sha256/" + digest_hex

    def get_exact(self, ref):
        return self.fake.get_exact(ref)


COST = {"verified": True, "incremental_brl": 0,
        "overage_blocked": True}


def run(fake, cost=COST):
    return ready_for_pr(
        intent=fake.intent, authority=fake, pool=fake, model_audit=fake,
        source=fake, writer=FakeCas(fake), independent_reader=FakeCas(fake),
        validator=fake, ledger=fake, cost=cost,
    )


@pytest.fixture
def fx():
    return Fake()


def test_ready_written_read_and_replayed_without_model_or_cas_mutation(fx):
    first = run(fx)
    assert first["state"] == "READY_FOR_PR"
    assert first["artifact_sha256"] in fx.cas
    assert first["replayed"] is False
    assert (fx.model_reads, fx.writes, fx.validations) == (1, 1, 1)
    again = run(fx)
    assert again["replayed"] is True
    assert again["artifact_ref"] == first["artifact_ref"]
    assert (fx.model_reads, fx.writes, fx.validations) == (1, 1, 1)


@pytest.mark.parametrize("change,code", [
    ("base", "prepr_base_advanced"),
    ("head", "prepr_candidate_advanced"),
    ("issue_state", "prepr_issue_not_open"),
    ("todo_state", "prepr_todo_not_authorized"),
    ("authorized", "prepr_authority_unproved"),
])
def test_admission_fail_closed(fx, change, code):
    setattr(fx, change, {"base": "f"*40, "head": "f"*40,
                         "issue_state": "closed", "todo_state": "UNAUTHORIZED",
                         "authorized": False}[change])
    with pytest.raises(PrePRBlocked, match=code):
        run(fx)
    assert fx.writes == fx.model_reads == 0


@pytest.mark.parametrize("change,code", [
    ("model_absent", "prepr_real_model_unproved"),
    ("no_model_calls", "prepr_real_model_unproved"),
    ("wrong_model_sha", "prepr_real_model_unproved"),
    ("fence_absent", "prepr_fenced_builder_unproved"),
    ("empty_patch", "prepr_empty_patch"),
    ("validator_sha", "prepr_validator_unproved"),
    ("validator_skipped", "prepr_validator_unproved"),
    ("materialization", "prepr_independent_git_unproved"),
    ("cas_unreadable", "prepr_cas_readback_failed"),
])
def test_negative_controls_never_record_ready(fx, change, code):
    if change == "model_absent":
        fx.model["provider_verified"] = False
    elif change == "no_model_calls":
        fx.model["call_count"] = 0
    elif change == "wrong_model_sha":
        fx.model["produced_sha"] = "f"*40
    elif change == "fence_absent":
        fx.task["fencing_required"] = False
    elif change == "empty_patch":
        fx.objects["patch"] = b" \n"
    elif change == "validator_sha":
        fx.validator_wrong_sha = True
    elif change == "validator_skipped":
        fx.validator_skipped = True
    elif change == "materialization":
        fx.materialization_bad = True
    else:
        fx.tamper = True
    with pytest.raises(PrePRBlocked, match=code):
        run(fx)
    assert not fx.decision


@pytest.mark.parametrize("cost", [
    {"verified": False, "incremental_brl": 0, "overage_blocked": True},
    {"verified": True, "incremental_brl": None, "overage_blocked": True},
    {"verified": True, "incremental_brl": 0.1, "overage_blocked": True},
    {"verified": True, "incremental_brl": 0, "overage_blocked": False},
])
def test_unknown_cost_blocks_before_model_or_cas(fx, cost):
    with pytest.raises(PrePRBlocked, match="prepr_cost_unknown"):
        run(fx, cost=cost)
    assert fx.writes == fx.model_reads == 0


def test_replay_conflict_and_replay_tampering(fx):
    run(fx)
    fx.decision[KEY]["head_sha"] = "f"*40
    with pytest.raises(PrePRBlocked, match="prepr_replay_conflict"):
        run(fx)
    fx.decision[KEY]["head_sha"] = HEAD
    fx.tamper = True
    with pytest.raises(PrePRBlocked, match="prepr_replay_artifact_unreadable"):
        run(fx)


def test_publish_only_after_ready_with_unique_draft_readback(fx):
    with pytest.raises(PrePRBlocked, match="prepr_ready_missing"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    run(fx)
    values = dict(intent=fx.intent, authority=fx, ledger=fx,
                  github=fx, independent_reader=FakeCas(fx))
    first = publish_single_draft(**values)
    again = publish_single_draft(**values)
    assert first["state"] == "DRAFT_PR_VERIFIED"
    assert first["replayed"] is False and again["replayed"] is True
    assert fx.pr_writes == 1 and fx.pr_reads == 2


def test_duplicate_pr_blocks_before_publication(fx):
    run(fx)
    fx.prs = [{"number": 123}, {"number": 124}]
    with pytest.raises(PrePRBlocked, match="prepr_duplicate_pr"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    assert fx.pr_writes == 0


def test_ambiguous_publication_does_not_replay_write(fx):
    run(fx)
    fx.create_draft = lambda **kwargs: None
    with pytest.raises(PrePRBlocked, match="prepr_publication_ambiguous"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    assert fx.prs == []


def test_candidate_head_change_after_ready_blocks_draft(fx):
    run(fx)
    fx.head = "f"*40
    with pytest.raises(PrePRBlocked, match="prepr_candidate_advanced"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    assert fx.pr_writes == 0
