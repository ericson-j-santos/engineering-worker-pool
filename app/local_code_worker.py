"""Bounded local-code worker: one pure numeric function in an isolated Git branch.

No shell, remote Git, imports or arbitrary calls, unbounded retry or merge.
The caller must supply a trusted task and run through its governed executor.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENT = re.compile(r"[a-z][a-z0-9_]{0,40}")
MODEL = "qwen2.5-coder:1.5b"
OLLAMA_URL = "http://127.0.0.1:11434"
CONTAINER = "worker-pool-local-code"
NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


class WorkerBlocked(RuntimeError):
    """Closed reason codes only; provider/process output must not enter errors."""


def require(ok: bool, code: str) -> None:
    if not ok:
        raise WorkerBlocked(code)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def numeric(value: Any) -> bool:
    return type(value) in (int, float) and abs(value) <= 1_000_000 and math.isfinite(value)


def task_spec(task: Any) -> dict:
    required = {"task_id", "path", "function", "parameters", "instruction",
                "base_sha", "before_sha256", "cases"}
    require(type(task) is dict and set(task) == required, "task_schema")
    for field, pattern in (("task_id", r"[a-zA-Z0-9_-]{1,128}"),
                           ("base_sha", r"[0-9a-f]{40}"),
                           ("before_sha256", r"[0-9a-f]{64}"),
                           ("path", r"src/[a-z][a-z0-9_]{0,60}\.py"),
                           ("function", r"[a-z][a-z0-9_]{0,40}")):
        require(type(task[field]) is str and re.fullmatch(pattern, task[field]) is not None,
                "task_" + field)
    require(task["function"] not in {"min", "max"}, "reserved_function")
    params = task["parameters"]
    require(type(params) is list and 1 <= len(params) <= 4
            and all(type(v) is str and IDENT.fullmatch(v) for v in params)
            and len(set(params)) == len(params)
            and not set(params) & {"min", "max"}, "task_parameters")
    require(type(task["instruction"]) is str and 1 <= len(task["instruction"]) <= 2000,
            "task_instruction")
    cases = task["cases"]
    require(type(cases) is list and 2 <= len(cases) <= 200, "task_cases")
    for case in cases:
        require(type(case) is dict and set(case) == {"args", "expected"}
                and type(case["args"]) is list and len(case["args"]) == len(params)
                and all(numeric(v) for v in case["args"]) and numeric(case["expected"]),
                "task_case")
    return task


def expression_tree(expression: str, parameters: list[str]) -> ast.Expression:
    require(type(expression) is str and 1 <= len(expression.encode()) <= 1024,
            "candidate_size")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise WorkerBlocked("candidate_syntax") from None
    nodes = list(ast.walk(tree))
    require(len(nodes) <= 96 and all(type(n) in NODES for n in nodes), "candidate_grammar")
    call_names = set()
    for node in nodes:
        if isinstance(node, ast.Call):
            require(type(node.func) is ast.Name and node.func.id in {"min", "max"}
                    and len(node.args) == 2 and not node.keywords, "candidate_call")
            call_names.add(id(node.func))
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        require(depth <= 16, "candidate_depth")
        pending.extend((n, depth + 1) for n in ast.iter_child_nodes(node))
        if isinstance(node, ast.Name):
            require(node.id in parameters or id(node) in call_names, "candidate_name")
        if isinstance(node, ast.Constant):
            require(numeric(node.value), "candidate_constant")
    return tree


def source_for(task: dict, reply: str) -> str:
    task_spec(task)
    require(type(reply) is str and len(reply.encode()) <= 4096, "provider_reply_size")
    try:
        candidate = json.loads(reply)
    except (ValueError, TypeError):
        raise WorkerBlocked("provider_reply_json") from None
    require(type(candidate) is dict and set(candidate) == {"expression"}, "candidate_schema")
    tree = expression_tree(candidate["expression"], task["parameters"])
    return (f"def {task['function']}({', '.join(task['parameters'])}):\n"
            f"    return {ast.unparse(tree.body)}\n")


def check_source(source: str, task: dict) -> int:
    """Run a bounded expression; only pure two-number min/max primitives."""
    task_spec(task)
    require(type(source) is str and len(source.encode()) <= 2048, "source_size")
    try:
        tree = ast.parse(source)
        require(len(tree.body) == 1 and type(tree.body[0]) is ast.FunctionDef, "source_function")
        fn = tree.body[0]
        require(fn.name == task["function"] and not fn.decorator_list and fn.returns is None
                and not fn.args.defaults and not fn.args.kw_defaults and not fn.args.kwonlyargs
                and not fn.args.posonlyargs and fn.args.vararg is None and fn.args.kwarg is None
                and [a.arg for a in fn.args.args] == task["parameters"]
                and all(a.annotation is None for a in fn.args.args)
                and not getattr(fn, "type_params", [])
                and len(fn.body) == 1 and type(fn.body[0]) is ast.Return
                and fn.body[0].value is not None, "source_signature")
        expression_tree(ast.unparse(fn.body[0].value), task["parameters"])
        scope: dict[str, Any] = {"__builtins__": {}, "min": min, "max": max}
        exec(compile(tree, "<validated-numeric-function>", "exec"), scope)
        passed = 0
        for case in task["cases"]:
            value = scope[task["function"]](*case["args"])
            require(type(value) in (int, float) and math.isfinite(value), "non_numeric_result")
            passed += value == case["expected"]
        return passed
    except WorkerBlocked:
        raise
    except (SyntaxError, ValueError, TypeError, ArithmeticError, RecursionError):
        raise WorkerBlocked("candidate_execution") from None


def process(argv: list[str], cwd: Path, timeout: int = 15) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerBlocked("process_unavailable") from None
    require(result.returncode == 0, "process_failed")
    require(len(result.stdout) <= 1_048_576, "process_output_size")
    return result.stdout.strip()


def git(root: Path, *args: str) -> str:
    return process(["git", *args], root)


def safe_target(root: Path, task: dict) -> Path:
    path = root / task["path"]
    require(root.is_dir() and not root.is_symlink()
            and (root / "src").is_dir() and not (root / "src").is_symlink()
            and path.is_file() and not path.is_symlink()
            and path.stat().st_nlink == 1 and path.resolve().parent == (root / "src").resolve(),
            "target_not_regular")
    return path


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_independently(root: Path, task: dict, state: Path) -> int:
    spec = state / "validation-task.json"
    atomic_json(spec, task)
    result = process([sys.executable, str(Path(__file__).resolve()), "validate",
                      str(safe_target(root, task)), str(spec)], root)
    try:
        evidence = json.loads(result)
    except ValueError:
        raise WorkerBlocked("validator_json") from None
    require(evidence == {"passed": len(task["cases"]), "result": "VALIDATED"},
            "validator_rejected")
    return evidence["passed"]


def repair(root: Path, state: Path, task: dict, propose: Callable[[dict, str], str]) -> dict:
    """One admitted task, at most two validated proposals; replay performs no inference.

    The repository must be an isolated worker branch with no remote. This first
    version produces a local commit/patch, never publishes to a user's repository.
    """
    task_spec(task)
    require(not root.is_symlink() and not state.is_symlink(), "symlink_root")
    root, state = root.resolve(), state.resolve()
    require(root != state and root not in state.parents, "state_must_be_outside_repo")
    state.mkdir(parents=True, exist_ok=True)
    task_hash = digest(json.dumps(task, sort_keys=True).encode())
    identity = digest(str(root).encode())
    lock, receipt = state / (identity + ".lock"), state / (identity + ".json")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise WorkerBlocked("worker_busy") from None
    os.close(descriptor)
    try:
        path = safe_target(root, task)
        require(git(root, "remote") == "", "remote_repository_blocked")
        hooks = Path(git(root, "rev-parse", "--git-path", "hooks"))
        if not hooks.is_absolute():
            hooks = root / hooks
        require(not hooks.is_symlink() and (not hooks.exists() or not any(
            p.is_file() and not p.name.endswith(".sample") for p in hooks.iterdir())),
            "active_git_hooks")
        require(git(root, "branch", "--show-current").startswith("worker/"),
                "worker_branch_required")
        require(git(root, "status", "--porcelain", "--untracked-files=all") == "",
                "dirty_worktree")
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            require(previous.get("task_hash") == task_hash, "replay_task_mismatch")
            require(previous.get("produced_sha") == git(root, "rev-parse", "HEAD")
                    and previous.get("after_sha256") == digest(path.read_bytes()), "replay_changed")
            validate_independently(root, task, state)
            return {**previous, "replayed": True, "model_calls": 0}
        require(git(root, "rev-parse", "HEAD") == task["base_sha"], "base_sha_changed")
        before = path.read_text(encoding="utf-8")
        require(digest(path.read_bytes()) == task["before_sha256"], "source_changed")
        baseline_passed = check_source(before, task)
        require(baseline_passed < len(task["cases"]), "no_failing_baseline")
        proposal_source = before
        proposal_task = dict(task)
        attempts = []
        after = before
        for attempt in (1, 2):
            require(git(root, "rev-parse", "HEAD") == task["base_sha"]
                    and git(root, "status", "--porcelain", "--untracked-files=all") == "",
                    "concurrent_repo_change")
            after = source_for(task, propose(proposal_task, proposal_source))
            require(after != before, "no_code_change")
            candidate_passed = check_source(after, task)
            attempts.append({"attempt": attempt, "candidate_sha256": digest(after.encode()),
                             "passed": candidate_passed, "total": len(task["cases"])})
            if candidate_passed == len(task["cases"]):
                break
            if attempt == 2:
                raise WorkerBlocked("candidate_tests_failed")
            # A second proposal is allowed only after an actual failed validation.
            # Give the model the rejected source and prioritize its failing cases,
            # preserving the trusted instruction and the complete original test set.
            failing, passing = [], []
            for case in task["cases"]:
                probe = {**task, "cases": [case, case]}
                (passing if check_source(after, probe) == 2 else failing).append(case)
            require(bool(failing), "failure_feedback_missing")
            proposal_source = after
            proposal_task = {**task, "cases": failing + passing}
        require(git(root, "rev-parse", "HEAD") == task["base_sha"]
                and git(root, "status", "--porcelain", "--untracked-files=all") == "",
                "concurrent_repo_change")
        require(digest(path.read_bytes()) == task["before_sha256"], "concurrent_source_change")
        path.write_text(after, encoding="utf-8")
        try:
            passed = validate_independently(root, task, state)
            require(git(root, "diff", "--name-only") == task["path"], "change_scope")
        except WorkerBlocked:
            if path.read_text(encoding="utf-8") == after:
                path.write_text(before, encoding="utf-8")
            raise
        patch = git(root, "diff", "--", task["path"]) + "\n"
        git(root, "add", "--", task["path"])
        git(root, "-c", "user.name=Local Code Worker", "-c", "user.email=worker@localhost",
            "commit", "-m", "fix: bounded local model repair")
        produced = git(root, "rev-parse", "HEAD")
        require(SHA.fullmatch(produced) is not None and produced != task["base_sha"],
                "produced_commit_missing")
        require(git(root, "diff", "--name-only", task["base_sha"], produced) == task["path"]
                and git(root, "status", "--porcelain", "--untracked-files=all") == "",
                "commit_scope")
        validate_independently(root, task, state)
        result = {"result": "LOCAL_CODE_REPAIRED", "task_id": task["task_id"],
                  "task_hash": task_hash, "base_sha": task["base_sha"], "produced_sha": produced,
                  "before_sha256": task["before_sha256"], "after_sha256": digest(path.read_bytes()),
                  "baseline_passed": baseline_passed, "passed": passed, "path": task["path"],
                  "patch_sha256": digest(patch.encode()), "replayed": False,
                  "model_calls": len(attempts), "attempts": attempts}
        (state / "change.patch").write_text(patch, encoding="utf-8")
        atomic_json(receipt, result)
        return result
    finally:
        lock.unlink(missing_ok=True)


class LocalOllama:
    """Local proposal provider for the disposable CI runtime, not a physical host."""
    def __init__(self) -> None:
        self.model_digest = ""
        self.process_verified = False

    @staticmethod
    def no_cloud() -> None:
        raw = process(["docker", "inspect", "--format", "{{json .Config.Env}}", CONTAINER],
                      Path.cwd())
        try:
            values = json.loads(raw)
        except ValueError:
            raise WorkerBlocked("runtime_configuration_json") from None
        require(isinstance(values, list)
                and [s for s in values if isinstance(s, str) and s.startswith("OLLAMA_NO_CLOUD=")]
                == ["OLLAMA_NO_CLOUD=1"], "cloud_not_disabled")

    @staticmethod
    def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict:
        try:
            with client.stream(method, path, **kwargs) as response:
                require(response.status_code == 200, "ollama_http")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    require(len(body) <= 65536, "ollama_response_size")
            value = json.loads(body)
            require(isinstance(value, dict), "ollama_json_object")
            return value
        except (httpx.HTTPError, ValueError):
            raise WorkerBlocked("ollama_unavailable") from None

    def __call__(self, task: dict, before: str) -> str:
        task_spec(task)
        self.no_cloud()
        with httpx.Client(base_url=OLLAMA_URL, timeout=120, trust_env=False,
                          follow_redirects=False) as client:
            tags = self.request(client, "GET", "/api/tags").get("models")
            require(type(tags) is list and len(tags) == 1 and isinstance(tags[0], dict),
                    "model_inventory")
            item = tags[0]
            require(item.get("name") == MODEL and not item.get("remote_host")
                    and not item.get("remote_model"), "remote_model_blocked")
            model_digest = item.get("digest")
            require(type(model_digest) is str and DIGEST.fullmatch(model_digest) is not None,
                    "model_digest")
            show = self.request(client, "POST", "/api/show", json={"model": MODEL})
            require(not show.get("remote_host") and not show.get("remote_model")
                    and (show.get("details") or {}).get("format") == "gguf", "weights_unproved")
            prompt = ("Repair this function; the current implementation fails its tests.\n"
                      + task["instruction"] + "\nCurrent DEFECTIVE code:\n" + before
                      + "\nTrusted input/output acceptance examples:\n"
                      + json.dumps(task["cases"][:8], separators=(",", ":"))
                      + "\nReturn JSON with only the key expression. Its value must be a CORRECTED Python "
                      "expression, not a copy of the defective return value. "
                      "Use function parameters, numeric literals, comparisons, +, -, *, "
                      "conditional expressions (x if condition else y), and optionally min or max "
                      "with exactly two numeric arguments. No other calls, imports, attributes, "
                      "function definitions, return statements or Markdown.")
            response = self.request(client, "POST", "/api/chat", json={
                "model": MODEL, "messages": [
                    {"role": "system", "content": "Fix the code to satisfy the input/output tests. "
                     "Output only the requested JSON; do not explain or reproduce the known defect."},
                    {"role": "user", "content": prompt}],
                "stream": False, "keep_alive": "5m",
                "format": {"type": "object", "properties": {"expression": {"type": "string"}},
                           "required": ["expression"], "additionalProperties": False},
                "options": {"temperature": 0, "seed": 7, "num_predict": 192, "num_ctx": 2048},
            })
            require(response.get("done") is True and response.get("model") == MODEL
                    and type(response.get("eval_count")) is int and response["eval_count"] > 0,
                    "inference_unproved")
            running = self.request(client, "GET", "/api/ps").get("models")
            require(type(running) is list and any(
                isinstance(m, dict) and m.get("name") == MODEL and m.get("digest") == model_digest
                and type(m.get("size")) is int and m["size"] > 0
                and not m.get("remote_host") and not m.get("remote_model") for m in running),
                "local_process_unproved")
            self.no_cloud()
            self.model_digest, self.process_verified = model_digest, True
            message = response.get("message")
            require(type(message) is dict and type(message.get("content")) is str,
                    "provider_content")
            return message["content"]


def main() -> int:
    # This CLI only validates; mutation is exposed through the trusted repair API.
    try:
        require(len(sys.argv) == 4 and sys.argv[1] == "validate", "validator_arguments")
        task = task_spec(json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")))
        passed = check_source(Path(sys.argv[2]).read_text(encoding="utf-8"), task)
        require(passed == len(task["cases"]), "validator_tests_failed")
        print(json.dumps({"passed": passed, "result": "VALIDATED"}))
        return 0
    except (WorkerBlocked, OSError, ValueError):
        print('{"result":"VALIDATION_BLOCKED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
