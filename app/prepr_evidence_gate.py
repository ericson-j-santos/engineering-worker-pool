"""Stage-only, independent pre-PR verification; reuses existing Worker Pool CAS.

Trusted adapters must independently read GitHub, native model, Git CAS,
source bytes, Validator and cost. This module has no write or READY_FOR_PR path.
"""
import hashlib
import re
from pathlib import PurePosixPath
from app.native_one_shot import DurableArtifact, NativeExecutorBlocked

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")


def need(ok, code):
    if not ok:
        raise NativeExecutorBlocked(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check(request, *, live_github, model_attestor, cas_reader,
          source_reader, independent_validator, cost_attestor):
    """Only trusted, authenticated adapters may implement the six readers."""
    need(type(request) is dict and request.get("schema_version") == 1,
         "prepr_request_invalid")
    need(type(request.get("repository")) is str and
         re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                      request["repository"]) is not None, "prepr_repo_invalid")
    need(type(request.get("issue_number")) is int and
         request["issue_number"] > 0, "prepr_issue_invalid")
    for key in ("base_sha", "produced_sha", "rules_sha"):
        need(type(request.get(key)) is str and
             SHA40.fullmatch(request[key]) is not None, "prepr_sha_invalid")
    need(request["base_sha"] != request["produced_sha"], "prepr_empty_commit")
    for key in ("request_id", "correlation_id", "idempotency_key"):
        need(type(request.get(key)) is str and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z",
                          request[key]) is not None, "prepr_identity_invalid")
    paths = request.get("changed_paths")
    need(type(paths) is list and 1 <= len(paths) <= 8, "prepr_paths_invalid")
    for p in paths:
        need(type(p) is str and 3 <= len(p) <= 200 and "\\" not in p and
             not p.startswith(".") and not PurePosixPath(p).is_absolute() and
             len(PurePosixPath(p).parts) >= 2 and
             not any(x in (".", "..") or x.startswith(".")
                     for x in PurePosixPath(p).parts), "prepr_path_invalid")
    need(paths == sorted(set(paths)), "prepr_paths_invalid")
    for field in ("before_sha256", "after_sha256"):
        hashes = request.get(field)
        need(type(hashes) is dict and set(hashes) == set(paths) and
             all(type(v) is str and SHA64.fullmatch(v) is not None
                 for v in hashes.values()), "prepr_hashes_invalid")
    # Validate the full CAS descriptor before invoking any external authority.
    # Untrusted task input must not cause GitHub or model callbacks on failure.
    artifact = DurableArtifact.parse(request.get("artifact"))
    need(artifact.artifact_kind == "git_bundle_v1" and
         artifact.artifact_ref.startswith("cas+"), "prepr_cas_kind")
    patch_sha = request.get("patch_sha256")
    need(type(patch_sha) is str and SHA64.fullmatch(patch_sha) is not None,
         "prepr_patch_missing")
    need(all(callable(cb) for cb in (
        live_github, model_attestor, cas_reader, source_reader,
        independent_validator, cost_attestor)), "prepr_adapter_missing")

    repo, number = request["repository"], request["issue_number"]
    base, head = request["base_sha"], request["produced_sha"]
    def git_readback():
        value = live_github(repo, number)
        need(type(value) is dict and
             value.get("repository") == repo and
             value.get("issue_number") == number and
             value.get("issue_state") == "open" and
             value.get("main_sha") == base and
             value.get("branch_head_sha") == head and
             value.get("base_is_ancestor") is True and
             value.get("changed_paths") == paths, "prepr_github_drift")
        return value

    first = git_readback()
    model = model_attestor(request["request_id"])
    need(type(model) is dict and
         model.get("source") == "attested_ollama_runtime" and
         model.get("request_id") == request["request_id"] and
         model.get("produced_sha") == head and
         type(model.get("model")) is str and
         model["model"].startswith("qwen") and
         type(model.get("model_digest")) is str and
         SHA64.fullmatch(model["model_digest"]) is not None and
         type(model.get("model_calls")) is int and
         model["model_calls"] >= 1 and
         type(model.get("baseline_failures")) is int and
         model["baseline_failures"] >= 1, "prepr_model_unverified")

    before = cas_reader(artifact.artifact_ref)
    need(type(before) is bytes and 0 < len(before) <= 524288 and
         digest(before) == artifact.artifact_sha256,
         "prepr_cas_integrity")
    again = cas_reader(artifact.artifact_ref)
    need(type(again) is bytes and again == before, "prepr_cas_reread")
    different = False
    for path in paths:
        old = source_reader(base, path)
        new = source_reader(head, path)
        need(type(old) is bytes and type(new) is bytes and
             0 < len(old) <= 524288 and 0 < len(new) <= 524288 and
             digest(old) == request["before_sha256"][path] and
             digest(new) == request["after_sha256"][path],
             "prepr_source_integrity")
        different = different or old != new
    need(different, "prepr_empty_patch")
    valid = independent_validator(request["request_id"])
    need(type(valid) is dict and
         valid.get("source") == "independent_sha_bound_validator" and
         valid.get("request_id") == request["request_id"] and
         valid.get("base_sha") == base and
         valid.get("produced_sha") == head and
         valid.get("correlation_id") == request["correlation_id"] and
         valid.get("artifact_sha256") == artifact.artifact_sha256 and
         valid.get("patch_sha256") == patch_sha and
         valid.get("materialized_git_bundle") is True and
         valid.get("independent_checkout") is True and
         type(valid.get("builder_identity")) is str and
         type(valid.get("validator_identity")) is str and
         valid["builder_identity"] != valid["validator_identity"] and
         type(valid.get("tests_passed")) is int and
         type(valid.get("tests_total")) is int and
         valid["tests_passed"] == valid["tests_total"] > 0 and
         valid.get("tests_failed") == 0 and
         valid.get("tests_skipped") == 0,
         "prepr_validator_unverified")
    need(cost_attestor(request["request_id"]) is True, "prepr_cost_unknown")
    need(git_readback() == first, "prepr_github_changed")
    return {
        "result": "PREPR_EVIDENCE_VERIFIED_ONLY",
        "request_id": request["request_id"], "produced_sha": head,
        "artifact_sha256": artifact.artifact_sha256,
        "ready_for_pr": False, "draft_pr_created": False,
        "host_e2e_proved": False,
    }
