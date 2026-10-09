from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from structured_acceptance import cases, INSTRUCTION
from app import structured_code_worker as worker

BEFORE = 'def _property(properties: dict[str, Any], name: str) -> str:\n    data = properties.get(name) or {}\n    if not isinstance(data, dict):\n        return ""\n    if data.get("type") == "select":\n        return str((data.get("select") or {}).get("name") or "").strip()\n    if data.get("type") == "url":\n        return str(data.get("url") or "").strip()\n    if data.get("type") == "rich_text":\n        return "".join(\n            str(v.get("plain_text") or (v.get("text") or {}).get("content") or "")\n            for v in data.get("rich_text", []) if isinstance(v, dict)\n        ).strip()\n    return ""\n'
FIXED = 'def _property(properties, name):\n    if not isinstance(properties, dict):\n        return ""\n    data = properties.get(name)\n    if not isinstance(data, dict):\n        return ""\n    kind = data.get("type")\n    if kind == "select":\n        selected = data.get("select")\n        if not isinstance(selected, dict):\n            return ""\n        value = selected.get("name")\n        if not isinstance(value, str):\n            return ""\n        return value.strip()\n    if kind == "url":\n        value = data.get("url")\n        if not isinstance(value, str):\n            return ""\n        return value.strip()\n    if kind == "rich_text":\n        items = data.get("rich_text")\n        if not isinstance(items, list):\n            return ""\n        parts = []\n        for item in items:\n            if not isinstance(item, dict):\n                return ""\n            value = item.get("plain_text")\n            if value is None:\n                text = item.get("text")\n                if not isinstance(text, dict):\n                    return ""\n                value = text.get("content")\n            if not isinstance(value, str):\n                return ""\n            parts.append(value)\n        return "".join(parts).strip()\n    return ""\n'


def task(base_sha="a" * 40, source=BEFORE):
    return {"task_id": "structured-property-test", "path": "app/portfolio_bridge.py",
            "function": "_property", "parameters": ["properties", "name"],
            "instruction": INSTRUCTION, "base_sha": base_sha,
            "before_sha256": worker.sha256(source.encode()), "cases": cases()}


def test_real_decoder_regression_and_positive():
    contract = task()
    before = worker.validate(BEFORE, contract)
    assert before["passed"] < before["total"] and before["errors"] > 0
    after = worker.render(BEFORE, contract, json.dumps({"function": FIXED}))
    actual = worker.validate(after, contract)
    assert actual == {"passed": len(cases()), "total": len(cases()), "errors": 0}
    assert before["passed"] < actual["passed"]


@pytest.mark.parametrize("statement", [
    "import os\n    return os.getcwd()",
    "return __import__('os').getcwd()",
    "return open('/etc/passwd').read()",
    "return properties.__class__",
    "return eval('1')",
    "return getattr(properties, 'keys')()",
    "while True:\n        pass",
    "return _property(properties, name)",
    "return globals()",
    "return (lambda: 1)()",
    "return name.format_map(properties)",
    "return [x for x in properties]",
    "str = 1\n    return ''",
    "return {**properties}",
    "return properties.get(**name)",
    "return properties.get(*name)",
])
def test_unsafe_candidate_never_executes(statement):
    source = "def _property(properties, name):\n    " + statement + "\n"
    with pytest.raises(worker.StructuredBlocked):
        worker.render(BEFORE, task(), json.dumps({"function": source}))


@pytest.mark.parametrize("source", [
    "def _property(properties, name):\n    return ''\nimport os\n",
    "def _property(properties, name=print('BAD')):\n    return ''\n",
    "@print('BAD')\ndef _property(properties, name):\n    return ''\n",
    "def other(properties, name):\n    return ''\n",
    "not valid syntax",
])
def test_function_boundary_and_signature(source):
    with pytest.raises(worker.StructuredBlocked):
        worker.render(BEFORE, task(), json.dumps({"function": source}))


def test_module_bytes_outside_function_preserved_without_importing():
    source = "# header\nraise RuntimeError('MUST_NOT_IMPORT_MODULE')\n\n" + BEFORE + "\n# tail\nOTHER = 17\n"
    output = worker.render(source, task(source=source), json.dumps({"function": FIXED}))
    assert output.startswith("# header\nraise RuntimeError('MUST_NOT_IMPORT_MODULE')\n\n")
    assert output.endswith("\n# tail\nOTHER = 17\n")
    assert worker.validate(output, task(source=source))["passed"] == len(cases())


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), 10**20, set(), b"binary"])
def test_non_json_or_oversized_input_rejected(value):
    contract = task()
    contract["cases"][0]["args"][0] = value
    with pytest.raises(worker.StructuredBlocked):
        worker.task_spec(contract)


def repository(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    source = "# real surrounding module bytes\n" + BEFORE + "\nOTHER = 17\n"
    (root / "app/portfolio_bridge.py").write_text(source)
    worker.git(root, "init", "-b", "worker/structured")
    worker.git(root, "add", ".")
    worker.git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "baseline")
    contract = task(worker.git(root, "rev-parse", "HEAD"), source)
    return root, tmp_path / "state", contract


def test_git_repair_patch_independent_checkout_and_replay(tmp_path):
    root, state, contract = repository(tmp_path)
    calls = []
    def propose(task, before):
        calls.append(task["task_id"])
        return json.dumps({"function": FIXED})
    result = worker.repair(root, state, contract, propose)
    assert result["result"] == "STRUCTURED_CODE_REPAIRED"
    assert result["produced_sha"] != contract["base_sha"]
    clone = tmp_path / "validator"
    worker.git(tmp_path, "clone", "--no-hardlinks", str(root), str(clone))
    worker.git(clone, "checkout", "--detach", result["produced_sha"])
    assert worker.validate((clone / contract["path"]).read_text(), contract)["passed"] == len(cases())
    replay = worker.repair(root, state, contract, propose)
    assert replay["model_calls"] == 0 and replay["replayed"] is True
    assert len(calls) == 1
    assert worker.git(root, "diff", "--name-only", contract["base_sha"], result["produced_sha"]) == contract["path"]
    assert "OTHER = 17" in (root / contract["path"]).read_text()


@pytest.mark.parametrize("condition", ["dirty", "remote", "hook", "config", "lock", "sha", "hash", "symlink"])
def test_preconditions_block_before_inference(tmp_path, condition):
    root, state, contract = repository(tmp_path)
    if condition == "dirty":
        (root / "unrelated.txt").write_text("preserve")
    elif condition == "remote":
        worker.git(root, "remote", "add", "origin", "https://example.invalid/not-used")
    elif condition == "hook":
        (root / ".git/hooks/pre-commit").write_text("MUST_NOT_RUN")
    elif condition == "config":
        with (root / ".git/config").open("a") as f:
            f.write("\n[alias]\n    status = !echo MUST_NOT_RUN\n")
    elif condition == "lock":
        state.mkdir()
        (state / (worker.sha256(str(root.resolve()).encode()) + ".lock")).touch()
    elif condition == "sha":
        contract["base_sha"] = "b" * 40
    elif condition == "hash":
        contract["before_sha256"] = "f" * 64
    else:
        original = root / contract["path"]
        original.rename(root / "original.py")
        original.symlink_to(root / "original.py")
    with pytest.raises(worker.StructuredBlocked):
        worker.repair(root, state, contract, lambda *_: pytest.fail("inference_not_allowed"))


def test_concurrent_change_is_preserved(tmp_path):
    root, state, contract = repository(tmp_path)
    def propose(*_):
        (root / "other.txt").write_text("someone else's work")
        return json.dumps({"function": FIXED})
    with pytest.raises(worker.StructuredBlocked, match="concurrent_change"):
        worker.repair(root, state, contract, propose)
    assert (root / "other.txt").read_text() == "someone else's work"
    assert worker.sha256((root / contract["path"]).read_bytes()) == contract["before_sha256"]


def test_failing_candidate_never_changes_git(tmp_path):
    root, state, contract = repository(tmp_path)
    with pytest.raises(worker.StructuredBlocked, match="candidate_tests_failed"):
        worker.repair(root, state, contract, lambda *_: json.dumps(
            {"function": "def _property(properties, name):\n    return ''\n"}))
    assert worker.git(root, "rev-parse", "HEAD") == contract["base_sha"]
    assert worker.git(root, "status", "--porcelain") == ""


def test_tampered_receipt_replay_rejected(tmp_path):
    root, state, contract = repository(tmp_path)
    worker.repair(root, state, contract, lambda *_: json.dumps({"function": FIXED}))
    (state / "change.patch").write_text("tampered")
    with pytest.raises(worker.StructuredBlocked, match="replay_patch_changed"):
        worker.repair(root, state, contract, lambda *_: pytest.fail("no_new_inference"))


def test_source_hash_sha_lock_and_negative_control_remain_effective(tmp_path):
    root, state, contract = repository(tmp_path)
    modified = dict(contract)
    modified["cases"] = contract["cases"][:2]
    bad = worker.render(BEFORE, task(), json.dumps({"function": FIXED}))
    assert worker.validate(bad, task())["passed"] == len(cases())
    mutated = dict(task())
    mutated["cases"] = [{"args": [{"F": {"type": "url", "url": "ok"}}, "F"],
                         "expected": "WRONG"}] * 2
    result = worker.validate(bad, mutated)
    assert result["passed"] == 0
    assert worker.validate(bad, task())["passed"] == len(cases())


def test_validator_runs_without_site_packages_and_credentials(monkeypatch):
    observed = {}
    real = subprocess.run
    def run(argv, **kwargs):
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return real(argv, **kwargs)
    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.validate(FIXED, task())
    assert "-I" in observed["argv"] and "-S" in observed["argv"]
    assert set(observed["env"]) == {"PATH"}


def test_validator_timeout_is_sanitized(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("PRIVATE_VALUE", 5)
    monkeypatch.setattr(worker.subprocess, "run", timeout)
    with pytest.raises(worker.StructuredBlocked, match="^validator_unavailable$") as error:
        worker.validate(FIXED, task())
    assert "PRIVATE_VALUE" not in str(error.value)
