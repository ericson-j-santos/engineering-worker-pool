"""Contract tests are synthetic; no Qwen/CAS/host is claimed."""
import copy
import hashlib
import pytest
from app.native_one_shot import NativeExecutorBlocked
from app.prepr_evidence_gate import check

def sha(x):
    return hashlib.sha256(x).hexdigest()

OLD, NEW, OBJ = b"data = 1\n", b"data = 2\n", b"fake git bundle"
BASE, HEAD = "a"*40, "b"*40

def fixture():
    req = dict(schema_version=1, repository="ericson-j-santos/sample",
               issue_number=42, base_sha=BASE, produced_sha=HEAD,
               rules_sha="c"*40, request_id="issue42-prepr-task",
               correlation_id="issue42-prepr-corr",
               idempotency_key="issue42:prepr:once",
               changed_paths=["app/code.py"],
               before_sha256={"app/code.py":sha(OLD)},
               after_sha256={"app/code.py":sha(NEW)},
               artifact=dict(artifact_kind="git_bundle_v1",
                             artifact_ref="cas+git://sha256/"+sha(OBJ),
                             artifact_sha256=sha(OBJ)),
               patch_sha256="d"*64)
    live = dict(repository=req["repository"], issue_number=42,
                issue_state="open", main_sha=BASE,
                branch_head_sha=HEAD, base_is_ancestor=True,
                changed_paths=["app/code.py"])
    model = dict(source="attested_ollama_runtime", request_id=req["request_id"],
                 produced_sha=HEAD, model="qwen2.5-coder:7b",
                 model_digest="e"*64, model_calls=1, baseline_failures=2)
    validator = dict(source="independent_sha_bound_validator",
                     request_id=req["request_id"], base_sha=BASE,
                     produced_sha=HEAD, correlation_id=req["correlation_id"],
                     artifact_sha256=sha(OBJ), patch_sha256=req["patch_sha256"],
                     materialized_git_bundle=True, independent_checkout=True,
                     builder_identity="builder-one",
                     validator_identity="validator-two", tests_passed=42,
                     tests_total=42, tests_failed=0, tests_skipped=0)
    calls = []
    def live_read(*args):
        calls.append("live")
        return copy.deepcopy(live)
    def cas_read(ref):
        calls.append("cas")
        return OBJ
    def source_read(commit, path):
        calls.append("source")
        return OLD if commit == BASE else NEW
    def model_read(*args):
        calls.append("model")
        return copy.deepcopy(model)
    def validator_read(*args):
        calls.append("validator")
        return copy.deepcopy(validator)
    def cost_read(*args):
        calls.append("cost")
        return True
    readers = dict(live_github=live_read, model_attestor=model_read,
                   cas_reader=cas_read, source_reader=source_read,
                   independent_validator=validator_read, cost_attestor=cost_read)
    return req, live, model, validator, readers, calls

def test_positive_is_only_stage_not_ready():
    req, _, _, _, r, calls = fixture()
    a, b = check(req, **r), check(req, **r)
    assert a == b
    assert a["ready_for_pr"] is False
    assert a["draft_pr_created"] is False
    assert a["host_e2e_proved"] is False
    assert calls.count("cas") == 4
    assert calls.count("live") == 4

@pytest.mark.parametrize("field,value,reason", [
    ("produced_sha", BASE, "prepr_empty_commit"),
    ("issue_number", True, "prepr_issue_invalid"),
    ("changed_paths", [], "prepr_paths_invalid"),
    ("changed_paths", ["../secret"], "prepr_path_invalid"),
    ("rules_sha", "no", "prepr_sha_invalid"),
    ("artifact", None, "durable_artifact_invalid"),
])
def test_invalid_input_never_reads_external(field, value, reason):
    req, _, _, _, r, calls = fixture()
    req[field] = value
    with pytest.raises(NativeExecutorBlocked, match=reason):
        check(req, **r)
    assert calls == []

@pytest.mark.parametrize("field,value", [
    ("main_sha", HEAD), ("issue_state", "closed"),
    ("base_is_ancestor", False), ("changed_paths", []),
])
def test_live_drift_fails(field, value):
    req, live, _, _, r, _ = fixture()
    live[field] = value
    with pytest.raises(NativeExecutorBlocked, match="prepr_github_drift"):
        check(req, **r)

@pytest.mark.parametrize("field,value", [
    ("model_calls", 0), ("model_digest", "bad"),
    ("baseline_failures", 0), ("source", "untrusted"),
    ("produced_sha", BASE),
])
def test_model_evidence_is_mandatory(field, value):
    req, _, model, _, r, _ = fixture()
    model[field] = value
    with pytest.raises(NativeExecutorBlocked, match="prepr_model_unverified"):
        check(req, **r)

def test_cas_mutation_and_loss_fail():
    req, _, _, _, r, _ = fixture()
    r["cas_reader"] = lambda _: b"altered"
    with pytest.raises(NativeExecutorBlocked, match="prepr_cas_integrity"):
        check(req, **r)
    r["cas_reader"] = lambda _: None
    with pytest.raises(NativeExecutorBlocked, match="prepr_cas_integrity"):
        check(req, **r)

@pytest.mark.parametrize("field,value", [
    ("independent_checkout", False), ("materialized_git_bundle", False),
    ("tests_skipped", 1), ("tests_failed", 1),
    ("tests_total", 43), ("builder_identity", "validator-two"),
    ("produced_sha", BASE),
])
def test_validator_must_be_independent_and_complete(field, value):
    req, _, _, validator, r, _ = fixture()
    validator[field] = value
    with pytest.raises(NativeExecutorBlocked, match="prepr_validator_unverified"):
        check(req, **r)

def test_unknown_cost_fails_closed():
    req, _, _, _, r, _ = fixture()
    r["cost_attestor"] = lambda _: None
    with pytest.raises(NativeExecutorBlocked, match="prepr_cost_unknown"):
        check(req, **r)

def test_git_bytes_changed_without_attestation_blocked():
    req, _, _, _, r, _ = fixture()
    r["source_reader"] = lambda _sha, _path: b"modified"
    with pytest.raises(NativeExecutorBlocked, match="prepr_source_integrity"):
        check(req, **r)

def test_readback_detects_moving_head():
    req, live, _, _, r, _ = fixture()
    visits = 0
    def live_drift(*args):
        nonlocal visits
        visits += 1
        result = copy.deepcopy(live)
        if visits == 2:
            result["branch_head_sha"] = BASE
        return result
    r["live_github"] = live_drift
    with pytest.raises(NativeExecutorBlocked, match="prepr_github_drift"):
        check(req, **r)

def test_unavailable_adapter_cannot_prove_ready():
    req, _, _, _, r, calls = fixture()
    r["model_attestor"] = None
    with pytest.raises(NativeExecutorBlocked, match="prepr_adapter_missing"):
        check(req, **r)
    assert calls == []
