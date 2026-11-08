"""Stage-only, independent pre-PR verification; reuses existing Worker Pool CAS.

Trusted adapters must independently read GitHub, native model, Git CAS,
source bytes, Validator and cost. This module has no write or READY_FOR_PR path.
"""
import hashlib
import re
from pathlib import PurePosixPath
from app.native_one_shot import DurableArtifact, NativeExecutorBlocked, TaskManifest, NativeOneShotExecutor
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport, ARTIFACT_REF_PREFIX

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
    for key in ("base_sha", "source_sha", "produced_sha", "rules_sha"):
        need(type(request.get(key)) is str and
             SHA40.fullmatch(request[key]) is not None, "prepr_sha_invalid")
    need(request["source_sha"] != request["produced_sha"] and
         request["base_sha"] != request["produced_sha"], "prepr_empty_commit")
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
    # SOURCE is the already-authorized GitHub branch diff. MODEL paths are
    # a bounded subset in TaskManifest; source workflows may be present,
    # but cannot become executable Builder command targets.
    source_paths = request.get("source_changed_paths")
    need(type(source_paths) is list and 1 <= len(source_paths) <= 32
         and source_paths == sorted(set(source_paths)),
         "prepr_source_paths_invalid")
    for path in source_paths:
        need(type(path) is str and 3 <= len(path) <= 240
             and "\\" not in path and not PurePosixPath(path).is_absolute()
             and len(PurePosixPath(path).parts) >= 2
             and ".." not in PurePosixPath(path).parts
             and (all(not part.startswith(".") for part in PurePosixPath(path).parts)
                  or (path.startswith(".github/workflows/")
                      and len(PurePosixPath(path).parts) >= 3
                      and all(not part.startswith(".")
                              for part in PurePosixPath(path).parts[2:]))),
             "prepr_source_path_invalid")
    need(set(paths).issubset(set(source_paths)),
         "prepr_model_scope_outside_source")
    for field in ("before_sha256", "after_sha256"):
        hashes = request.get(field)
        need(type(hashes) is dict and set(hashes) == set(paths) and
             all(type(v) is str and SHA64.fullmatch(v) is not None
                 for v in hashes.values()), "prepr_hashes_invalid")
    # Validate the full CAS descriptor before invoking any external authority.
    # Untrusted task input must not cause GitHub or model callbacks on failure.
    artifact = DurableArtifact.parse(request.get("artifact"))
    need(artifact.artifact_kind == "git_bundle_v1" and
         artifact.artifact_ref == ARTIFACT_REF_PREFIX + artifact.artifact_sha256,
         "prepr_cas_kind")
    # This is the original builder manifest, not an unbound result receipt.
    manifest = TaskManifest.parse(request.get("task_manifest"))
    need(manifest.repository == request["repository"] and
         manifest.base_sha == request["source_sha"] and
         manifest.workspace_sha == request["source_sha"] and
         set(manifest.before_sha256) == set(paths) and
         dict(manifest.before_sha256) == request["before_sha256"],
         "prepr_manifest_binding_invalid")
    patch_sha = request.get("patch_sha256")
    need(type(patch_sha) is str and SHA64.fullmatch(patch_sha) is not None,
         "prepr_patch_missing")
    need(all(callable(cb) for cb in (
        live_github, model_attestor, cas_reader, source_reader,
        independent_validator, cost_attestor)), "prepr_adapter_missing")

    repo, number = request["repository"], request["issue_number"]
    base, source_head = request["base_sha"], request["source_sha"]
    head = request["produced_sha"]
    def git_readback():
        value = live_github(repo, number)
        need(type(value) is dict and
             value.get("repository") == repo and
             value.get("issue_number") == number and
             value.get("issue_state") == "open" and
             value.get("main_sha") == base and
             value.get("branch_head_sha") == source_head and
             value.get("base_is_ancestor") is True and
             value.get("changed_paths") == source_paths, "prepr_github_drift")
        return value

    first = git_readback()
    model = model_attestor(request["request_id"])
    need(type(model) is dict and
         model.get("source") == "attested_ollama_runtime" and
         model.get("request_id") == request["request_id"] and
         model.get("task_id") == manifest.task_id and
         model.get("source_sha") == source_head and
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
    # Reuse the canonical durable Git transport's strict envelope parser.
    # No second CAS format, no trust in a bare reported patch SHA.
    envelope, git_bundle, result = S3GitBundleArtifactTransport._decode(
        before, artifact.artifact_sha256, manifest, head,
    )
    expected_effect = NativeOneShotExecutor._artifact_effect_id(
        manifest.task_id, head,
    )
    need(result.changed_paths == tuple(paths) and
         envelope["effect_id"] == expected_effect and
         result.evidence_sha256 == patch_sha and
         envelope["task_manifest"]["task_id"] == manifest.task_id and
         len(git_bundle) > 0,
         "prepr_cas_result_mismatch")
    different = False
    for path in paths:
        old = source_reader(source_head, path)
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
         valid.get("task_id") == manifest.task_id and
         valid.get("lease_generation") == envelope["lease_generation"] and
         valid.get("attempt_id") == envelope["attempt_id"] and
         valid.get("effect_id") == envelope["effect_id"] and
         valid.get("base_sha") == base and
         valid.get("source_sha") == source_head and
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
        "request_id": request["request_id"],
        "base_sha": base, "source_sha": source_head, "produced_sha": head,
        "artifact_sha256": artifact.artifact_sha256,
        "ready_for_pr": False, "draft_pr_created": False,
        "host_e2e_proved": False,
    }
