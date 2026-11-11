"""Positive and negative regression tests for the independent CAS verifier."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.issue61_cas_validator import (
    ALLOWED_PATHS,
    CASInvalid,
    CAS_PATH,
    EXPECTED_CAS_SHA256,
    digest,
    git_blob,
    materialize,
    verify,
)


@pytest.fixture
def raw() -> bytes:
    return CAS_PATH.read_bytes()


def reencode(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_original_cas_digest_and_scope(raw):
    data = verify(raw)
    assert digest(raw) == EXPECTED_CAS_SHA256
    assert data["ready_for_pr"] is False
    assert data["validator_verified"] is False
    assert data["branch_published"] is False
    assert tuple(f["path"] for f in data["files"]) == ALLOWED_PATHS


def test_materializes_only_verified_candidate_in_isolated_directory(raw, tmp_path):
    data = verify(raw)
    output = tmp_path / "candidate"
    materialize(data, output)
    for record in data["files"]:
        path = output / record["path"]
        assert path.read_bytes() == record["after"].encode("utf-8")
        assert git_blob(path.read_bytes()) == record["after_blob_sha1"]
    with pytest.raises(CASInvalid, match="CAS_OUTPUT_ALREADY_EXISTS"):
        materialize(data, output)


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(ready_for_pr=True), "CAS_UNAUTHORIZED_READY_STATE"),
    (lambda d: d.update(validator_verified=True), "CAS_UNAUTHORIZED_READY_STATE"),
    (lambda d: d.update(branch_published=True), "CAS_UNAUTHORIZED_READY_STATE"),
    (lambda d: d.update(model_digest="unverified"), "CAS_MODEL_PROVENANCE_UNEXPECTED"),
    (lambda d: d.update(source_sha="b" * 40), "CAS_SHA_SCOPE_MISMATCH"),
    (lambda d: d["files"][0].update(path="../secret.py"), "CAS_PATH_SCOPE_INVALID"),
    (lambda d: d["files"].append(copy.deepcopy(d["files"][0])), "CAS_PATH_SCOPE_INVALID"),
    (lambda d: d["files"][0].update(after="modified"), "CAS_CONTENT_DIGEST_MISMATCH"),
    (lambda d: d["files"][0].update(after_blob_sha1="0" * 40), "CAS_GIT_BLOB_MISMATCH"),
    (lambda d: d["files"][0].update(patch=""), "CAS_PATCH_MISSING"),
    (lambda d: d["files"][0].update(patch="fake hunk"), "CAS_PATCH_DIGEST_MISMATCH"),
])
def test_mutated_cas_is_rejected_even_with_recomputed_container_digest(raw, mutate, code):
    obj = json.loads(raw)
    mutate(obj)
    altered = reencode(obj)
    with pytest.raises(CASInvalid, match=code):
        verify(altered, expected_sha=digest(altered))


def test_random_corruption_fails_before_parse(raw):
    with pytest.raises(CASInvalid, match="CAS_SHA256_DRIFT"):
        verify(raw + b"X")


def test_invalid_json_never_materializes(tmp_path):
    invalid = b"{"
    with pytest.raises(CASInvalid, match="CAS_JSON_INVALID"):
        verify(invalid, expected_sha=digest(invalid))
    assert not list(tmp_path.iterdir())


def test_git_blob_hash_uses_actual_nul_separator():
    content = b"abc"
    expected = hashlib.sha1(b"blob 3\0abc").hexdigest()
    assert git_blob(content) == expected
    assert git_blob(content) != hashlib.sha1(b"blob 3\\0abc").hexdigest()
