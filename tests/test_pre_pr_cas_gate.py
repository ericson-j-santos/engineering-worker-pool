"""Policy-only tests: fake transports are NOT evidence of a real CAS/model/E2E."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace

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
        self.head = None
        self.task = {
            "task_id": self.intent.task_id, "repository": self.intent.repository,
            "issue_number": 42, "request_id": self.intent.request_id,
            "correlation_id": self.intent.correlation_id,
            "base_sha": BASE, "produced_sha": HEAD,
            "fencing_required": True, "leased_by": None,
            "state": "completed", "lease_generation": 2,
            "attempt_count": 2, "builder_worker_id": "builder-model001",
            "validator_worker_id": "validator-independent001",
        }
        self.checkpoint = {
            "task_id": self.intent.task_id,
            "stage": "validator_result", "next_action": "complete",
            "lease_generation": 2, "attempt_id": "fenced-validator-02",
        }
        self.model = {
            "provider_verified": True, "run_id": RUN,
            "task_id": self.intent.task_id,
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
            "source_sha": SOURCE,
            "candidate_ref": self.intent.candidate_ref,
            "todo_id": self.intent.todo_id,
            "correlation_id": self.intent.correlation_id,
            "allowed_paths": list(self.intent.allowed_paths),
        }

    def branch_sha(self, repository, ref):
        return self.base if ref == "main" else self.head

    def get_task(self, task_id):
        return dict(self.task)

    def get_latest_checkpoint(self, task_id):
        return dict(self.checkpoint)

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
    fx.head = HEAD  # Aprovada a READY, a publicação remota é uma etapa distinta.
    values = dict(intent=fx.intent, authority=fx, ledger=fx,
                  github=fx, independent_reader=FakeCas(fx))
    first = publish_single_draft(**values)
    again = publish_single_draft(**values)
    assert first["state"] == "DRAFT_PR_VERIFIED"
    assert first["replayed"] is False and again["replayed"] is True
    assert fx.pr_writes == 1 and fx.pr_reads == 2


def test_duplicate_pr_blocks_before_publication(fx):
    run(fx)
    fx.head = HEAD
    fx.prs = [{"number": 123}, {"number": 124}]
    with pytest.raises(PrePRBlocked, match="prepr_duplicate_pr"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    assert fx.pr_writes == 0


def test_ambiguous_publication_does_not_replay_write(fx):
    run(fx)
    fx.head = HEAD
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



def feature_context(fx):
    fx.intent = replace(
        fx.intent, kind="feature", source_mode="assisted",
        allowed_paths=(".sdd/specs/piloto.requirements.md", "docs/ux/piloto.md"))
    fx.feature_proof = {
        "source": "trusted-control-plane", "status": "verified_by_trusted_control_plane",
        "kind": "feature", "source_mode": "assisted",
        "repository": fx.intent.repository, "issue_number": fx.intent.issue_number,
        "todo_id": fx.intent.todo_id, "request_id": fx.intent.request_id,
        "correlation_id": fx.intent.correlation_id,
        "idempotency_key": fx.intent.idempotency_key,
        "base_sha": BASE, "candidate_sha": HEAD, "source_sha": SOURCE,
        "allowed_paths": list(fx.intent.allowed_paths),
        "locked_before_execution": True, "capability_absent_at_base": True,
        "acceptance_preexisting": True, "baseline_failure_reproduced": False,
        "prior_repair_blocked": False, "model_calls": 0, "model_digest": None,
        "rules_sha": "d"*40, "baseline_sha256": fx.model["baseline_sha256"],
        "evidence_sha256": "e"*64,
    }
    fx.verified_feature = lambda request_id: dict(fx.feature_proof)
    original = fx.execute_independently

    def feature_validator(**kwargs):
        result = original(**kwargs)
        result["baseline_failures"] = 0
        result["feature_acceptance_passed"] = True
        return result

    fx.execute_independently = feature_validator


def test_feature_assisted_ready_with_zero_model_calls_and_replay(fx):
    feature_context(fx)
    result = run(fx)
    assert result["state"] == "READY_FOR_PR"
    assert result["kind"] == "feature"
    assert result["model_calls"] == 0 and result["model_digest"] is None
    assert result["provenance_sha256"] == "e"*64
    assert (fx.model_reads, fx.writes, fx.validations) == (0, 1, 1)
    assert run(fx)["replayed"] is True
    assert (fx.model_reads, fx.writes, fx.validations) == (0, 1, 1)


@pytest.mark.parametrize("name,value", [
    ("locked_before_execution", False),
    ("capability_absent_at_base", False),
    ("acceptance_preexisting", False),
    ("baseline_failure_reproduced", True),
    ("prior_repair_blocked", True),
    ("model_calls", 1),
    ("model_digest", "a"*64),
    ("base_sha", "f"*40),
    ("evidence_sha256", "invalid"),
])
def test_feature_proof_fail_closed(fx, name, value):
    feature_context(fx)
    fx.feature_proof[name] = value
    with pytest.raises(PrePRBlocked, match="prepr_feature_provenance_unproved"):
        run(fx)
    assert fx.writes == fx.validations == fx.model_reads == 0


def test_feature_requires_trusted_provider(fx):
    fx.intent = replace(fx.intent, kind="feature", source_mode="assisted")
    with pytest.raises(PrePRBlocked, match="prepr_feature_authority_missing"):
        run(fx)


def test_feature_refuses_validator_without_acceptance(fx):
    feature_context(fx)
    validator = fx.execute_independently

    def invalid(**kw):
        result = validator(**kw)
        result["feature_acceptance_passed"] = False
        return result

    fx.execute_independently = invalid
    with pytest.raises(PrePRBlocked, match="prepr_validator_unproved"):
        run(fx)
    assert not fx.decision


@pytest.mark.parametrize("bad_path", [
    ".github/workflows/injected.yml", ".sdd/secret/report.spec.json",
    ".sdd/specs/../escape.requirements.md", "docs/.hidden/entry.md",
])
def test_feature_hidden_scope_remains_blocked(fx, bad_path):
    feature_context(fx)
    fx.intent = replace(fx.intent, allowed_paths=(bad_path,))
    with pytest.raises(PrePRBlocked, match="prepr_scope_invalid"):
        run(fx)


def test_repair_still_requires_real_model(fx):
    fx.model["call_count"] = 0
    with pytest.raises(PrePRBlocked, match="prepr_real_model_unproved"):
        run(fx)
    fx.intent = replace(fx.intent, kind="repair", source_mode="assisted")
    with pytest.raises(PrePRBlocked, match="prepr_kind_source_mode_invalid"):
        run(fx)


def test_feature_mode_requires_explicit_source(fx):
    fx.intent = replace(fx.intent, kind="feature")
    with pytest.raises(PrePRBlocked, match="prepr_kind_source_mode_invalid"):
        run(fx)


def test_feature_cost_fail_closed(fx):
    feature_context(fx)
    with pytest.raises(PrePRBlocked, match="prepr_cost_unknown"):
        run(fx, cost={"verified": False, "incremental_brl": 0, "overage_blocked": False})
    assert fx.writes == 0


def test_feature_replay_checks_mode(fx):
    feature_context(fx)
    run(fx)
    fx.decision[KEY]["kind"] = "repair"
    with pytest.raises(PrePRBlocked, match="prepr_replay_conflict"):
        run(fx)


def test_unpublished_candidate_can_reach_ready_after_independent_checks(fx):
    assert fx.branch_sha(fx.intent.repository, fx.intent.candidate_ref) is None
    result = run(fx)
    assert result["state"] == "READY_FOR_PR" and result["head_sha"] == HEAD
    assert fx.pr_writes == 0
    assert fx.head is None


def test_preexisting_candidate_ref_cannot_mint_ready(fx):
    fx.head = HEAD
    with pytest.raises(PrePRBlocked, match="prepr_candidate_published_before_ready"):
        run(fx)
    assert fx.writes == fx.model_reads == fx.validations == 0
    assert not fx.decision


def test_candidate_published_during_validation_blocks_ready(fx):
    validate = fx.execute_independently

    def concurrent_publish(**kwargs):
        result = validate(**kwargs)
        fx.head = HEAD
        return result

    fx.execute_independently = concurrent_publish
    with pytest.raises(PrePRBlocked, match="prepr_candidate_published_during_admission"):
        run(fx)
    assert not fx.decision


def test_publish_denies_missing_ref_even_with_ready(fx):
    run(fx)
    with pytest.raises(PrePRBlocked, match="prepr_candidate_advanced"):
        publish_single_draft(intent=fx.intent, authority=fx, ledger=fx,
                             github=fx, independent_reader=FakeCas(fx))
    assert fx.pr_writes == 0


def test_ready_replay_after_exact_branch_publication_is_idempotent(fx):
    first = run(fx)
    fx.head = HEAD
    again = run(fx)
    assert again["replayed"] is True
    assert again["artifact_ref"] == first["artifact_ref"]
    assert (fx.writes, fx.validations, fx.model_reads) == (1, 1, 1)


def test_unverified_candidate_lookup_blocks_before_cas(fx):
    fx.head = "not-a-sha"
    with pytest.raises(PrePRBlocked, match="prepr_candidate_readback_invalid"):
        run(fx)
    assert fx.writes == fx.model_reads == 0


def test_reqsys715_workflow_scoped_feature_path(fx):
    """Scope only; classification still requires external authenticated proof."""
    fx.intent = replace(fx.intent,
        repository="ericson-j-santos/reqsys-v2-enterprise-real",
        issue_number=715, kind="feature", source_mode="assisted",
        allowed_paths=(
            ".github/workflows/codex-worker-pool-handoff.yml",
            ".sdd/specs/reqsys-prova-rapida-interface-pareto.spec.json",
            "frontend/tests/e2e/prova-rapida.spec.js",
        ))
    fx.intent.check()


@pytest.mark.parametrize("change", [
    {"repository": "ericson-j-santos/engineering-worker-pool"},
    {"issue_number": 716},
    {"kind": "repair", "source_mode": "model_builder"},
    {"allowed_paths": (".github/workflows/other.yml",)},
    {"allowed_paths": (".github/workflows/codex-worker-pool-handoff.yml/../evil.yml",)},
    {"allowed_paths": (".git/config", ".github/workflows/codex-worker-pool-handoff.yml")},
])
def test_reqsys715_workflow_exception_never_opens_other_scopes(fx, change):
    fx.intent = replace(fx.intent,
        repository="ericson-j-santos/reqsys-v2-enterprise-real",
        issue_number=715, kind="feature", source_mode="assisted",
        allowed_paths=(".github/workflows/codex-worker-pool-handoff.yml",))
    fx.intent = replace(fx.intent, **change)
    with pytest.raises(PrePRBlocked, match="prepr_scope_invalid"):
        fx.intent.check()


def test_desktop123_workflow_scoped_feature_path(fx):
    """Only the existing Desktop #123 locator inventory workflow is admitted."""
    fx.intent = replace(
        fx.intent,
        repository="ericson-j-santos/desktop-pc24x7-runtime",
        issue_number=123, kind="feature", source_mode="assisted",
        allowed_paths=(
            ".github/workflows/pc24x7-orchestrator-supervisor-readonly.yml",
            "scripts/reqsys_dev_locator_risk1_inventory.py",
            "tests/test_reqsys_dev_locator_risk1_inventory.py",
        ),
    )
    fx.intent.check()


@pytest.mark.parametrize("change", [
    {"repository": "ericson-j-santos/engineering-worker-pool"},
    {"issue_number": 124},
    {"kind": "repair", "source_mode": "model_builder"},
    {"source_mode": "model_builder"},
    {"allowed_paths": (".github/workflows/other.yml",)},
    {"allowed_paths": (".github/workflows/pc24x7-orchestrator-supervisor-readonly.yml/../evil.yml",)},
    {"allowed_paths": (".git/config", ".github/workflows/pc24x7-orchestrator-supervisor-readonly.yml")},
])
def test_desktop123_workflow_exception_rejects_other_scopes(fx, change):
    fx.intent = replace(
        fx.intent,
        repository="ericson-j-santos/desktop-pc24x7-runtime",
        issue_number=123, kind="feature", source_mode="assisted",
        allowed_paths=(".github/workflows/pc24x7-orchestrator-supervisor-readonly.yml",),
    )
    fx.intent = replace(fx.intent, **change)
    with pytest.raises(PrePRBlocked, match="prepr_scope_invalid|prepr_kind_source_mode_invalid"):
        fx.intent.check()


def test_short_canonical_todo_global_id_is_admissible_without_alias(fx):
    """An authenticated provider must still bind TODO-74 to its real record."""
    fx.intent = replace(fx.intent, todo_id="TODO-74")
    fx.intent.check()


@pytest.mark.parametrize("invalid", [
    "TODO-0", "TODO-00074", "TODO-", "TODO-74\\n",
    "TODO-12345678901", "TO-74", 74, None,
])
def test_canonical_todo_id_malformed_values_stay_blocked(fx, invalid):
    fx.intent = replace(fx.intent, todo_id=invalid)
    with pytest.raises(PrePRBlocked, match="prepr_todo_invalid"):
        fx.intent.check()


def test_noteri42_workflow_scoped_repair_path(fx):
    """Noteri #42 repair may include only its exact workflow under .github."""
    fx.intent = replace(
        fx.intent,
        repository="ericson-j-santos/noteri-runtime",
        issue_number=42,
        todo_id="TODO-54",
        kind="repair",
        source_mode="model_builder",
        allowed_paths=(
            ".github/workflows/noteri-control-plane-probe.yml",
            "scripts/noteri_qualification_receipt.py",
        ),
    )
    fx.intent.check()


@pytest.mark.parametrize("change", [
    {"repository": "ericson-j-santos/engineering-worker-pool"},
    {"repository": "ericson-j-santos/reqsys-v2-enterprise-real"},
    {"issue_number": 43},
    {"issue_number": 715},
    {"todo_id": "TODO-75"},
    {"kind": "feature", "source_mode": "assisted"},
    {"allowed_paths": (".github/workflows/other.yml",)},
    {"allowed_paths": (".github/workflows/codex-worker-pool-handoff.yml",)},
    {"allowed_paths": (".github/workflows/pc24x7-orchestrator-supervisor-readonly.yml",)},
    {"allowed_paths": (".github/workflows/noteri-control-plane-probe.yml/../evil.yml",)},
    {"allowed_paths": (".git/config", ".github/workflows/noteri-control-plane-probe.yml")},
])
def test_noteri42_workflow_exception_rejects_other_scopes(fx, change):
    """A scoped exception never changes another consumer's policy."""
    fx.intent = replace(
        fx.intent,
        repository="ericson-j-santos/noteri-runtime",
        issue_number=42,
        kind="repair",
        source_mode="model_builder",
        allowed_paths=(".github/workflows/noteri-control-plane-probe.yml",),
    )
    fx.intent = replace(fx.intent, **change)
    with pytest.raises(PrePRBlocked, match="prepr_scope_invalid"):
        fx.intent.check()


def test_noteri42_workflow_not_admitted_without_model_even_if_path_exact(fx):
    fx.intent = replace(
        fx.intent,
        repository="ericson-j-santos/noteri-runtime",
        issue_number=42,
        kind="repair",
        source_mode="assisted",
        allowed_paths=(".github/workflows/noteri-control-plane-probe.yml",),
    )
    with pytest.raises(PrePRBlocked, match="prepr_kind_source_mode_invalid"):
        fx.intent.check()
