"""Read-only CAS verifier for issue #61; never grants READY_FOR_PR.

The verifier is deliberately kept on the CAS evidence ref, not main.
It materializes only exact allowlisted paths after validating the whole payload.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CAS_PATH = Path("cas/issue61/20261111/prepr-artifact.json")
EXPECTED_CAS_SHA256 = "dcb44976e3fcb77251d0cf5f19fa56053aaeb1ae3015a8eb97c1985289841289"
EXPECTED_SOURCE_SHA = "2504c5e123f6e7cd607ea77fde80cbebf46d2790"
EXPECTED_PRODUCED_SHA = "90f79146deec86b7d1265daf3bb9bb3b5879f3bd"
EXPECTED_PATCH_SHA256 = "52cf5a61cb6f41214cdaf64023043a4f209585f1b1b2cc4a7db91a422c2cc1c3"
ALLOWED_PATHS = (
    "app/pre_pr_cas_gate.py",
    "tests/test_pre_pr_cas_gate.py",
    "docs/PRE_PR_CAS_VALIDATOR.md",
)


class CASInvalid(ValueError):
    """Fail closed without printing payload bytes or secrets."""


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def git_blob(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def verify(raw: bytes, *, expected_sha: str = EXPECTED_CAS_SHA256) -> dict[str, Any]:
    if digest(raw) != expected_sha:
        raise CASInvalid("CAS_SHA256_DRIFT")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CASInvalid("CAS_JSON_INVALID") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CASInvalid("CAS_SCHEMA_INVALID")
    if (data.get("source_sha") != EXPECTED_SOURCE_SHA
            or data.get("produced_sha") != EXPECTED_PRODUCED_SHA
            or data.get("source_sha") == data.get("produced_sha")):
        raise CASInvalid("CAS_SHA_SCOPE_MISMATCH")
    if (data.get("ready_for_pr") is not False
            or data.get("validator_verified") is not False
            or data.get("branch_published") is not False):
        raise CASInvalid("CAS_UNAUTHORIZED_READY_STATE")
    if (data.get("model_digest") is not None
            or data.get("model_run_id") is not None):
        raise CASInvalid("CAS_MODEL_PROVENANCE_UNEXPECTED")
    files = data.get("files")
    if (not isinstance(files, list) or len(files) != len(ALLOWED_PATHS)
            or [f.get("path") for f in files if isinstance(f, dict)]
            != list(ALLOWED_PATHS)
            or data.get("changed_paths") != list(ALLOWED_PATHS)):
        raise CASInvalid("CAS_PATH_SCOPE_INVALID")
    patches: list[str] = []
    for file in files:
        path = file["path"]
        before = file.get("before")
        after = file.get("after")
        if not isinstance(before, str) or not isinstance(after, str) or before == after:
            raise CASInvalid("CAS_CONTENT_NOT_CHANGED")
        for label, content in (("before", before), ("after", after)):
            payload = content.encode("utf-8")
            if digest(payload) != file.get(f"{label}_sha256"):
                raise CASInvalid("CAS_CONTENT_DIGEST_MISMATCH")
            if git_blob(payload) != file.get(f"{label}_blob_sha1"):
                raise CASInvalid("CAS_GIT_BLOB_MISMATCH")
        patch = file.get("patch")
        if not isinstance(patch, str) or not patch:
            raise CASInvalid("CAS_PATCH_MISSING")
        patches.append(f"diff --git a/{path} b/{path}\n{patch}\n")
    if digest("\n".join(patches).encode("utf-8")) != data.get("patch_sha256"):
        raise CASInvalid("CAS_PATCH_DIGEST_MISMATCH")
    if data.get("patch_sha256") != EXPECTED_PATCH_SHA256:
        raise CASInvalid("CAS_PATCH_NOT_CANONICAL")
    return data


def materialize(data: dict[str, Any], root: Path) -> None:
    """Write allowlisted files only AFTER complete verification."""
    if root.exists():
        raise CASInvalid("CAS_OUTPUT_ALREADY_EXISTS")
    for file in data["files"]:
        output = root / file["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(file["after"].encode("utf-8"))
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")


def main() -> None:
    raw = CAS_PATH.read_bytes()
    data = verify(raw)
    materialize(data, Path(".issue61-independent-checkout"))
    print("ISSUE61_CAS_READBACK_VERIFIED")
    print(f"ARTIFACT_SHA256={digest(raw)}")
    print(f"PATCH_SHA256={data['patch_sha256']}")
    print(f"PRODUCED_SHA={data['produced_sha']}")
    print("READY_FOR_PR=false")


if __name__ == "__main__":
    main()
