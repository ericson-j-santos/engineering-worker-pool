"""Contract tests are synthetic; no Qwen/CAS/host is claimed."""
import copy
import hashlib
import pytest
from app.native_one_shot import NativeExecutorBlocked, TaskManifest, AdapterResult, NativeOneShotExecutor
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport, ARTIFACT_REF_PREFIX
from app.prepr_evidence_gate import check

def sha(x):
    return hashlib.sha256(x).hexdigest()

OLD, NEW, OBJ = b"data = 1\n", b"data = 2\n", b"fixture bundle body (not real git)"
BASE, HEAD, SOURCE = "a"*40, "b"*40, "c"*40
TASK = "cwp-" + "1"*24
PATCH = "d"*64

def fixture_envelope():
    manifest = TaskManifest.parse(dict(
        schema_version=1, task_id=TASK,
        repository="ericson-j-santos/sample", base_sha=SOURCE,
        workspace_sha=SOURCE, adapter_id="ci.patch.v1",
        adapter_contract_sha256="e"*64,
        before_sha256={"app/code.py": sha(OLD)},
    ))
    result = AdapterResult(
        produced_sha=HEAD, changed_paths=("app/code.py",),
        invoked_commands=("ci.fix",), evidence_sha256=PATCH,
    )
    header = S3GitBundleArtifactTransport._header(
        manifest=manifest, lease_generation=1, attempt_id="attempt-1",
        effect_id=NativeOneShotExecutor._artifact_effect_id(TASK, HEAD),
        result=result, bundle=OBJ,
    )
    raw = S3GitBundleArtifactTransport._encode(header, OBJ, 8192)
    return manifest, raw

def fixture():
    manifest, envelope = fixture_envelope()
    req = dict(schema_version=1, repository="ericson-j-santos/sample",
               issue_number=42, base_sha=BASE, source_sha=SOURCE,
               produced_sha=HEAD,
               rules_sha="c"*40, request_id="issue42-prepr-task",
               correlation_id="issue42-prepr-corr",
               idempotency_key="issue42:prepr:once",
               changed_paths=["app/code.py"],
               before_sha256={"app/code.py":sha(OLD)},
               after_sha256={"app/code.py":sha(NEW)},
               task_manifest=dict(
                   schema_version=manifest.schema_version,
                   task_id=manifest.task_id,
                   repository=manifest.repository, base_sha=manifest.base_sha,
                   workspace_sha=manifest.workspace_sha,
                   adapter_id=manifest.adapter_id,
                   adapter_contract_sha256=manifest.adapter_contract_sha256,
                   before_sha256=dict(manifest.before_sha256),
               ),
               artifact=dict(artifact_kind="git_bundle_v1",
                             artifact_ref=ARTIFACT_REF_PREFIX+sha(envelope),
                             artifact_sha256=sha(envelope)),
               patch_sha256=PATCH)
    live = dict(repository=req["repository"], issue_number=42,
                issue_state="open", main_sha=BASE,
                branch_head_sha=SOURCE, base_is_ancestor=True,
                changed_paths=["app/code.py"])
    model = dict(source="attested_ollama_runtime", request_id=req["request_id"],
                 produced_sha=HEAD, source_sha=SOURCE, task_id=TASK,
                 model="qwen2.5-coder:7b", model_digest="e"*64, model_calls=1, baseline_failures=2)
    validator = dict(source="independent_sha_bound_validator",
                     request_id=req["request_id"], task_id=TASK,
                     lease_generation=1, attempt_id="attempt-1",
                     effect_id=NativeOneShotExecutor._artifact_effect_id(TASK, HEAD),
                     base_sha=BASE, source_sha=SOURCE,
                     produced_sha=HEAD, correlation_id=req["correlation_id"],
                     artifact_sha256=sha(envelope),
                     patch_sha256=req["patch_sha256"],
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
        return envelope
    def source_read(commit, path):
        calls.append("source")
        return OLD if commit == SOURCE else NEW
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
    ("source_sha", "invalid", "prepr_sha_invalid"),
    ("artifact", None, "durable_artifact_invalid"),
    ("task_manifest", None, "manifest_schema"),
])
def test_invalid_input_never_reads_external(field, value, reason):
    req, _, _, _, r, calls = fixture()
    req[field] = value
    with pytest.raises(NativeExecutorBlocked, match=reason):
        check(req, **r)
    assert calls == []

@pytest.mark.parametrize("field,value", [
    ("main_sha", HEAD), ("issue_state", "closed"),
    ("branch_head_sha", HEAD),
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
    ("produced_sha", BASE), ("task_id", "cwp-" + "9"*24),
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
    ("produced_sha", BASE), ("task_id", "cwp-" + "9"*24),
    ("lease_generation", 2), ("attempt_id", "other-attempt"),
    ("effect_id", "other-effect"),
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

def test_cas_descriptor_rejects_wrong_ref_before_readback():
    req, _, _, _, adapters, calls = fixture()
    req["artifact"]["artifact_ref"] = "cas+git://sha256/" + "f"*64
    with pytest.raises(NativeExecutorBlocked, match="prepr_cas_kind"):
        check(req, **adapters)
    assert calls == []

def test_manifest_conflicts_before_external_readback():
    req, _, _, _, adapters, calls = fixture()
    req["task_manifest"]["repository"] = "ericson-j-santos/different"
    with pytest.raises(NativeExecutorBlocked, match="prepr_manifest_binding_invalid"):
        check(req, **adapters)
    assert calls == []

def test_cas_envelope_rejects_forged_patch_even_same_envelope_hash():
    req, _, _, _, adapters, _ = fixture()
    req["patch_sha256"] = "f"*64
    with pytest.raises(NativeExecutorBlocked, match="prepr_cas_result_mismatch"):
        check(req, **adapters)

def test_raw_bytes_without_canonical_envelope_rejected():
    req, _, _, _, adapters, _ = fixture()
    value = b"not a git bundle envelope"
    req["artifact"]["artifact_sha256"] = sha(value)
    req["artifact"]["artifact_ref"] = ARTIFACT_REF_PREFIX + sha(value)
    adapters["cas_reader"] = lambda _: value
    with pytest.raises(NativeExecutorBlocked, match="artifact_integrity_invalid"):
        check(req, **adapters)

def test_bundle_envelope_proof_not_model_or_real_git_e2e():
    req, _, _, _, adapters, _ = fixture()
    result = check(req, **adapters)
    assert result["result"] == "PREPR_EVIDENCE_VERIFIED_ONLY"
    assert result["host_e2e_proved"] is False
    assert result["ready_for_pr"] is False

def test_branch_source_distinct_from_main_and_unpublished_produced_sha():
    req, live, model, validator, cb, effects = fixture()
    assert req["base_sha"] != req["source_sha"]
    assert req["produced_sha"] != req["source_sha"]
    assert live["branch_head_sha"] == req["source_sha"]
    result = check(req, **cb)
    assert result["base_sha"] == req["base_sha"]
    assert result["source_sha"] == req["source_sha"]
    assert result["produced_sha"] == req["produced_sha"]
    assert result["ready_for_pr"] is False

def test_fake_git_bundle_effect_identity_rejected():
    req, _, _, _, cb, _ = fixture()
    manifest, _ = fixture_envelope()
    fake_result = AdapterResult(
        produced_sha=HEAD, changed_paths=("app/code.py",),
        invoked_commands=("ci.fix",), evidence_sha256=PATCH,
    )
    forged_header = S3GitBundleArtifactTransport._header(
        manifest=manifest, lease_generation=1,
        attempt_id="attempt-1", effect_id="an-unrelated-effect",
        result=fake_result, bundle=OBJ,
    )
    bad = S3GitBundleArtifactTransport._encode(forged_header, OBJ, 8192)
    req["artifact"]["artifact_sha256"] = sha(bad)
    req["artifact"]["artifact_ref"] = ARTIFACT_REF_PREFIX + sha(bad)
    cb["cas_reader"] = lambda _: bad
    with pytest.raises(NativeExecutorBlocked, match="prepr_cas_result_mismatch"):
        check(req, **cb)

def test_source_git_branch_advance_fails_closed_before_model():
    req, live, _, _, cb, events = fixture()
    live["branch_head_sha"] = HEAD
    with pytest.raises(NativeExecutorBlocked, match="prepr_github_drift"):
        check(req, **cb)
    assert events == ["live"]
