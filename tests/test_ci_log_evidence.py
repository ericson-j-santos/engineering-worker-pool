"""CI log capsules are lossless, SHA-bound, and fail closed without artifact storage."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import sys
from pathlib import Path
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# While testing this standalone copy, use a local import location.
try:
    from ci_evidence_log import EvidenceRejected, MAX_SIZE, emit, pack
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ci_evidence_log import EvidenceRejected, MAX_SIZE, emit, pack

SHA = "a" * 40


def evidence(tmp_path):
    root = tmp_path / "artifacts" / "structured-code-e2e"
    root.mkdir(parents=True)
    (root / "before.py").write_bytes(b"def f(x):\n    return x\n")
    (root / "task.json").write_text(json.dumps({
        "task_id": "test", "cases": [{"args": [1], "expected": 1}],
        "before_sha256": hashlib.sha256((root / "before.py").read_bytes()).hexdigest(),
    }))
    (root / "bootstrap.json").write_text(json.dumps({"head": SHA, "result": "SESSION_LAUNCH_OK"}))
    (root / "failure.json").write_text(json.dumps({"expected_sha": SHA, "reason_code": "candidate_tests_failed"}))
    return root


def test_capsule_round_trip_and_repeat(tmp_path):
    root = evidence(tmp_path)
    first = pack(root, SHA)
    assert first == pack(root, SHA)
    output = io.StringIO()
    with redirect_stdout(output):
        emit("structured-code-e2e", first, SHA)
    lines = output.getvalue().splitlines()
    parts = lines[0].split()
    assert parts == ["EVIDENCE_V1_BEGIN", "structured-code-e2e", SHA,
                     hashlib.sha256(first).hexdigest(), str(len(first))]
    assert lines[-1] == "EVIDENCE_V1_END structured-code-e2e"
    compressed = base64.b64decode("".join(row.split(" ", 1)[1] for row in lines[1:-1]), validate=True)
    assert compressed == first
    with zipfile.ZipFile(io.BytesIO(compressed)) as capsule:
        assert capsule.testzip() is None
        for source in root.iterdir():
            assert capsule.read(source.name) == source.read_bytes()


@pytest.mark.parametrize("bad_sha", ["", "bad", "0" * 40])
def test_reject_bad_or_mismatched_sha(tmp_path, bad_sha):
    root = evidence(tmp_path)
    with pytest.raises(EvidenceRejected):
        pack(root, bad_sha)


@pytest.mark.parametrize("filename", ["bootstrap.json", "task.json"])
def test_missing_receipt_is_error(tmp_path, filename):
    root = evidence(tmp_path)
    (root / filename).unlink()
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_unknown_filename_cannot_leak(tmp_path):
    root = evidence(tmp_path)
    (root / "secrets.env").write_text("PRIVATE_VALUE")
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_symlink_and_directory_rejected(tmp_path):
    root = evidence(tmp_path)
    try:
        (root / "after.py").symlink_to(root / "before.py")
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
    else:
        with pytest.raises(EvidenceRejected):
            pack(root, SHA)
        (root / "after.py").unlink()
    (root / "after.py").mkdir()
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_oversized_payload_rejected(tmp_path):
    root = evidence(tmp_path)
    (root / "after.py").write_bytes(b"x" * (MAX_SIZE + 1))
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_forged_failure_head_rejected(tmp_path):
    root = evidence(tmp_path)
    (root / "failure.json").write_text(json.dumps({"expected_sha": "b" * 40}))
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_invalid_task_does_not_enter_log(tmp_path):
    root = evidence(tmp_path)
    (root / "task.json").write_text("PRIVATE_TOKEN")
    with pytest.raises(EvidenceRejected):
        pack(root, SHA)


def test_unexpected_label_rejected():
    with pytest.raises(EvidenceRejected):
        emit("unknown", b"data", SHA)
