"""Publish existing E2E evidence losslessly through GitHub Actions job logs.

The job log and summary do not count toward Actions artifact storage.
No new network operations, permissions or secrets are needed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

LABELS = ("local-code-e2e", "structured-code-e2e")
PERMITTED = frozenset({
    "before.py", "after.py", "change.patch", "bootstrap.json",
    "evidence.json", "failure.json", "inner-failure.json", "task.json",
    "proposal.json", "revision-diagnostic.json", "model-progress.json",
})
MAX_SIZE = 128 * 1024
MAX_ZIP_SIZE = 96 * 1024


class EvidenceRejected(ValueError):
    """Never propagate an exception that may contain evidence text."""


def pack(folder: Path, head: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise EvidenceRejected("bad_sha")
    if folder.is_symlink() or not folder.is_dir():
        raise EvidenceRejected("missing_directory")
    files = sorted(folder.iterdir(), key=lambda p: p.name)
    if not files or len(files) > len(PERMITTED):
        raise EvidenceRejected("file_count")
    content = {}
    total = 0
    for file in files:
        if file.name not in PERMITTED or file.is_symlink() or not file.is_file():
            raise EvidenceRejected("unexpected_file")
        data = file.read_bytes()
        total += len(data)
        if total > MAX_SIZE:
            raise EvidenceRejected("too_large")
        content[file.name] = data
    if "bootstrap.json" not in content or "task.json" not in content:
        raise EvidenceRejected("receipt_missing")
    try:
        task = json.loads(content["task.json"])
    except (ValueError, UnicodeError):
        raise EvidenceRejected("bad_task") from None
    if (not isinstance(task, dict) or not isinstance(task.get("task_id"), str)
            or not isinstance(task.get("cases"), list)
            or not re.fullmatch(r"[0-9a-f]{64}", str(task.get("before_sha256", "")))):
        raise EvidenceRejected("bad_task")
    for name, expected_key in (("bootstrap.json", "head"),
                               ("evidence.json", "expected_sha"),
                               ("failure.json", "expected_sha"),
                               ("proposal.json", "expected_sha")):
        if name in content:
            try:
                obj = json.loads(content[name])
            except (ValueError, UnicodeError):
                raise EvidenceRejected("bad_receipt") from None
            if not isinstance(obj, dict) or obj.get(expected_key) != head:
                raise EvidenceRejected("sha_mismatch")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name, data in content.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    result = stream.getvalue()
    if len(result) > MAX_ZIP_SIZE:
        raise EvidenceRejected("zip_too_large")
    return result


def emit(label: str, data: bytes, head: str) -> None:
    if label not in LABELS:
        raise EvidenceRejected("bad_label")
    digest = hashlib.sha256(data).hexdigest()
    print(f"EVIDENCE_V1_BEGIN {label} {head} {digest} {len(data)}")
    encoded = base64.b64encode(data).decode("ascii")
    for i in range(0, len(encoded), 96):
        print("EVIDENCE_V1_DATA " + encoded[i:i+96])
    print(f"EVIDENCE_V1_END {label}")


def main() -> int:
    try:
        head = os.environ.get("EXPECTED_SHA", "")
        root = Path("artifacts")
        # Gather and validate both before printing anything: no partial proof.
        payloads = [(label, pack(root / label, head)) for label in LABELS]
        for label, data in payloads:
            emit(label, data, head)
        return 0
    except (EvidenceRejected, OSError):
        print("EVIDENCE_V1_BLOCKED")
        return 2


if __name__ == "__main__":
    sys.exit(main())
