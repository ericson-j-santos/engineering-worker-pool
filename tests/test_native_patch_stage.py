"""Safety tests for stage-only patch CAS, with real isolated Git repositories."""
from pathlib import Path
import hashlib
import json
import subprocess
import zipfile

import pytest
from scripts.native_patch_stage import StageBlocked, stage_validated_patch


def git(path: Path, *argv: str) -> str:
    p = subprocess.run(["git", "-C", str(path), *argv], text=True,
                       capture_output=True, check=True)
    return p.stdout.strip()


def setup(tmp_path):
    repo = tmp_path / "builder"
    (repo / "app").mkdir(parents=True)
    (repo / "app/portfolio_bridge.py").write_text("def _property(x):\n    return 0\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "commit", "-qm", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "worker/model")
    (repo / "app/portfolio_bridge.py").write_text("def _property(x):\n    return 1\n")
    patch = (git(repo, "diff", "--", "app/portfolio_bridge.py") + "\n").encode()
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "commit", "-qm", "model repair")
    produced = git(repo, "rev-parse", "HEAD")
    val = tmp_path / "validator"
    git(tmp_path, "clone", "-q", str(repo), str(val))
    git(val, "checkout", "-q", "--detach", produced)
    state = tmp_path / "state"
    state.mkdir()
    (state / "change.patch").write_bytes(patch)
    out = tmp_path / "stage"
    out.mkdir()
    args = dict(builder=repo,validator=val,state=state,output_root=out,
                source_sha=base,base_sha=base,produced_sha=produced,
                patch_sha256=hashlib.sha256(patch).hexdigest(),
                correlation_id="worker-cas-test-a1",passed=42,total=42,
                regression_passed=52)
    return args


def test_positive_and_replay_no_duplication(tmp_path):
    args=setup(tmp_path)
    first=stage_validated_patch(**args)
    second=stage_validated_patch(**args)
    assert first["result"]=="NATIVE_PATCH_STAGED_NOT_DURABLE"
    assert not first["replayed"] and second["replayed"]
    assert first["artifact_sha256"]==second["artifact_sha256"]
    assert first["produced_sha"] != args["base_sha"]
    assert len(list(args["output_root"].glob("*.zip")))==1
    with zipfile.ZipFile(first["artifact_path"]) as z:
        assert set(z.namelist())=={"receipt.json","change.patch","result.bundle"}
        assert z.read("change.patch")== (args["state"] / "change.patch").read_bytes()
        rec=json.loads(z.read("receipt.json"))
        assert not rec["ready_for_pr"] and rec["stage_only_not_durable"]
        assert rec["produced_sha"]==args["produced_sha"]
        assert hashlib.sha256(z.read("result.bundle")).hexdigest()==rec["bundle_sha256"]
        assert args["produced_sha"] in z.read("result.bundle").decode("latin-1", "ignore")[:500]


def test_noop_commit_is_rejected_before_staging(tmp_path):
    args=setup(tmp_path)
    args["produced_sha"]=args["base_sha"]
    with pytest.raises(StageBlocked, match="not_a_real_patch"):
        stage_validated_patch(**args)
    assert list(args["output_root"].iterdir())==[]


def test_model_patch_tampering_blocked(tmp_path):
    args=setup(tmp_path)
    (args["state"]/"change.patch").write_bytes(b"different")
    with pytest.raises(StageBlocked, match="patch_bytes_invalid"):
        stage_validated_patch(**args)


def test_false_validator_sha_blocked(tmp_path):
    args=setup(tmp_path)
    git(args["validator"], "checkout", "--detach", args["base_sha"])
    with pytest.raises(StageBlocked, match="validator_commit_mismatch"):
        stage_validated_patch(**args)


def test_42_threshold_and_regression_required(tmp_path):
    args=setup(tmp_path)
    args["passed"]=41
    with pytest.raises(StageBlocked, match="independent_tests_not_proven"):
        stage_validated_patch(**args)


def test_cas_tamper_replay_must_not_rewrite(tmp_path):
    args=setup(tmp_path)
    first=stage_validated_patch(**args)
    Path(first["artifact_path"]).write_bytes(b"tampered")
    with pytest.raises(StageBlocked, match="stage_cas_conflict"):
        stage_validated_patch(**args)
    assert Path(first["artifact_path"]).read_bytes()==b"tampered"


def test_scope_is_single_file(tmp_path):
    args=setup(tmp_path)
    (args["builder"] / "other.txt").write_text("unexpected")
    with pytest.raises(StageBlocked, match="checkout_dirty"):
        stage_validated_patch(**args)


def test_output_root_link_is_rejected(tmp_path):
    args=setup(tmp_path)
    alias=tmp_path / "alias"
    alias.symlink_to(args["output_root"], target_is_directory=True)
    args["output_root"]=alias
    with pytest.raises(StageBlocked, match="stage_root_unavailable"):
        stage_validated_patch(**args)
