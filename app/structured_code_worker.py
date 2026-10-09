"""Bounded JSON-function repairs in an isolated Git copy, never a general agent.

The trusted caller owns the path, function, inputs, expected outputs and executor.
The model can only replace that function. Only JSON values and a small set of
builtins/methods are executable, after AST validation and in a limited subprocess.
"""
from __future__ import annotations

import ast
import configparser
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

BUILTINS = {"isinstance": isinstance, "str": str, "dict": dict, "list": list,
            "len": len, "bool": bool, "int": int, "float": float}
METHODS = {"get", "strip", "join", "append"}
NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.If, ast.For,
    ast.Return, ast.Assign, ast.Expr, ast.Call, ast.Attribute, ast.Name,
    ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.Compare, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.IfExp,
    ast.BinOp, ast.Add, ast.GeneratorExp, ast.comprehension,
}


class StructuredBlocked(RuntimeError):
    """Fixed reason codes; never include provider, task or process output."""


def need(ok: bool, code: str) -> None:
    if not ok:
        raise StructuredBlocked(code)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_value(value, depth: int = 0) -> None:
    need(depth <= 8, "json_depth")
    if type(value) is dict:
        need(len(value) <= 64 and all(type(k) is str and len(k) <= 128 for k in value),
             "json_object")
        for child in value.values():
            json_value(child, depth + 1)
    elif type(value) is list:
        need(len(value) <= 64, "json_array")
        for child in value:
            json_value(child, depth + 1)
    else:
        need(value is None or type(value) in (str, bool, int, float), "json_type")
        if type(value) is str:
            need(len(value) <= 4096, "json_string")
        if type(value) in (int, float):
            import math
            need(abs(value) <= 1_000_000 and math.isfinite(value), "json_number")


def task_spec(task: dict) -> dict:
    fields = {"task_id", "path", "function", "parameters", "instruction",
              "base_sha", "before_sha256", "cases"}
    need(type(task) is dict and set(task) == fields, "task_schema")
    for key, pattern in (
        ("task_id", r"[a-zA-Z0-9_-]{1,128}"),
        ("path", r"(?:app|src)/[a-z][a-z0-9_]{0,60}\.py"),
        ("function", r"_?[a-z][a-z0-9_]{0,60}"),
        ("base_sha", r"[0-9a-f]{40}"),
        ("before_sha256", r"[0-9a-f]{64}"),
    ):
        need(type(task[key]) is str and re.fullmatch(pattern, task[key]) is not None,
             "task_" + key)
    parameters = task["parameters"]
    need(type(parameters) is list and 1 <= len(parameters) <= 4
         and all(type(p) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", p)
                 for p in parameters)
         and len(set(parameters)) == len(parameters)
         and not set(parameters).intersection(BUILTINS), "task_parameters")
    need(task["function"] not in BUILTINS, "reserved_function")
    need(type(task["instruction"]) is str and 1 <= len(task["instruction"]) <= 4000,
         "task_instruction")
    need(type(task["cases"]) is list and 2 <= len(task["cases"]) <= 100, "task_cases")
    for case in task["cases"]:
        need(type(case) is dict and set(case) == {"args", "expected"}
             and type(case["args"]) is list and len(case["args"]) == len(parameters),
             "task_case")
        json_value(case)
    need(len(json.dumps(task, allow_nan=False).encode()) <= 100_000, "task_size")
    return task


def function_tree(source: str, name: str) -> ast.FunctionDef:
    need(type(source) is str and len(source.encode()) <= 262144, "module_size")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise StructuredBlocked("module_syntax") from None
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == name]
    need(len(functions) == 1, "function_identity")
    function = functions[0]
    need(not function.decorator_list, "function_decorator")
    return function


def extracted(source: str, task: dict) -> str:
    function = function_tree(source, task["function"])
    need([a.arg for a in function.args.args] == task["parameters"]
         and not function.args.defaults and not function.args.kwonlyargs
         and not function.args.posonlyargs and function.args.vararg is None
         and function.args.kwarg is None and not getattr(function, "type_params", []),
         "function_signature")
    function = copy.deepcopy(function)
    # Never evaluate annotations from an existing module or import that module.
    function.returns = None
    for arg in function.args.args:
        arg.annotation = None
    return ast.unparse(function) + "\n"


def guarded(source: str, task: dict) -> ast.Module:
    normalized = extracted(source, task)
    tree = ast.parse(normalized)
    nodes = list(ast.walk(tree))
    need(len(nodes) <= 350 and all(type(node) in NODES for node in nodes),
         "candidate_grammar")
    need(sum(isinstance(node, ast.FunctionDef) for node in nodes) == 1,
         "nested_function")
    need(sum(isinstance(node, (ast.For, ast.comprehension)) for node in nodes) <= 1,
         "candidate_loops")
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        need(depth <= 24, "candidate_depth")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        if isinstance(node, ast.Name):
            need(not node.id.startswith("__"), "candidate_name")
            if isinstance(node.ctx, ast.Store):
                need(node.id not in BUILTINS and node.id != task["function"],
                     "reserved_assignment")
        if isinstance(node, ast.Assign):
            need(len(node.targets) == 1 and type(node.targets[0]) is ast.Name,
                 "assignment_target")
        if isinstance(node, ast.For):
            need(type(node.target) is ast.Name and not node.orelse, "loop_target")
        if isinstance(node, ast.comprehension):
            need(type(node.target) is ast.Name and not node.is_async, "generator_target")
        if isinstance(node, ast.Call):
            need(not node.keywords and all(not isinstance(a, ast.Starred) for a in node.args),
                 "call_expansion")
            need((type(node.func) is ast.Name and node.func.id in BUILTINS)
                 or (type(node.func) is ast.Attribute and node.func.attr in METHODS),
                 "candidate_call")
        if isinstance(node, ast.Attribute):
            need(node.attr in METHODS and type(node.ctx) is ast.Load,
                 "candidate_attribute")
        if isinstance(node, ast.Dict):
            need(all(key is not None for key in node.keys), "dictionary_expansion")
        if isinstance(node, ast.Constant):
            json_value(node.value)
    return tree


def render(before: str, task: dict, reply: str) -> str:
    task_spec(task)
    need(type(reply) is str and len(reply.encode()) <= 20000, "reply_size")
    try:
        proposed = json.loads(reply)
    except ValueError:
        raise StructuredBlocked("reply_json") from None
    need(type(proposed) is dict and set(proposed) == {"function"}
         and type(proposed["function"]) is str, "reply_schema")
    try:
        tree = ast.parse(proposed["function"])
    except (SyntaxError, ValueError, RecursionError):
        raise StructuredBlocked("candidate_syntax") from None
    need(len(tree.body) == 1 and type(tree.body[0]) is ast.FunctionDef,
         "candidate_function_only")
    candidate = guarded(proposed["function"], task).body[0]
    original = function_tree(before, task["function"])
    extracted(before, task)  # Verify the original signature too.
    lines = before.splitlines(keepends=True)
    # Preserve the reviewed signature/annotations. Limit this version to one-line signatures.
    header = lines[original.lineno - 1]
    need(header.rstrip().endswith(":") and header.lstrip().startswith("def "),
         "multiline_signature_not_supported")
    body = ast.unparse(candidate).splitlines(keepends=True)[1:]
    replacement = header + "".join(body).rstrip("\n") + "\n"
    after = "".join(lines[:original.lineno - 1]) + replacement + "".join(lines[original.end_lineno:])
    ast.parse(after)
    return after



OBSERVATION_TYPES = {"NoneType", "str", "bool", "int", "float", "list", "dict"}
EXCEPTION_TYPES = {"AttributeError", "TypeError", "ValueError", "KeyError", "IndexError",
                   "NameError", "UnboundLocalError", "OverflowError", "RuntimeError",
                   "RecursionError", "MemoryError", "InvalidResult", "Exception"}
FEEDBACK_LIMIT = 12000


def observation(value) -> dict:
    """Bounded JSON observation; never a repr of an arbitrary Python object."""
    json_value(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 512:
        return {"kind": "value", "type": type(value).__name__, "truncated": True,
                "bytes": len(encoded), "sha256": sha256(encoded)}
    return {"kind": "value", "type": type(value).__name__, "value": value}


def validate_observation(value: dict) -> None:
    need(type(value) is dict, "observation_schema")
    if value.get("kind") == "exception":
        need(set(value) == {"kind", "type"} and type(value["type"]) is str
             and value["type"] in EXCEPTION_TYPES,
             "observation_exception")
        return
    need(value.get("kind") == "value" and type(value.get("type")) is str
         and value["type"] in OBSERVATION_TYPES,
         "observation_type")
    if "value" in value:
        need(set(value) == {"kind", "type", "value"}
             and type(value["value"]).__name__ == value["type"], "observation_value")
        need(observation(value["value"]) == value, "observation_size")
    else:
        need(set(value) == {"kind", "type", "truncated", "bytes", "sha256"}
             and value.get("truncated") is True
             and type(value.get("bytes")) is int and value["bytes"] > 512
             and type(value.get("sha256")) is str
             and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None,
             "observation_digest")


def validate_report(evidence: dict, task: dict) -> dict:
    """Validate the child-process protocol, including totals and failure indices."""
    need(type(evidence) is dict and set(evidence) == {"passed", "total", "errors", "failures"},
         "validator_evidence")
    need(all(type(evidence[k]) is int for k in ("passed", "total", "errors"))
         and evidence["total"] == len(task["cases"])
         and 0 <= evidence["passed"] <= evidence["total"]
         and 0 <= evidence["errors"] <= evidence["total"] - evidence["passed"],
         "validator_counts")
    failures = evidence["failures"]
    need(type(failures) is list
         and len(failures) == evidence["total"] - evidence["passed"], "validator_failures")
    indices = []
    for failure in failures:
        need(type(failure) is dict and set(failure) == {"case_index", "observed"}
             and type(failure["case_index"]) is int
             and 0 <= failure["case_index"] < evidence["total"], "validator_case")
        indices.append(failure["case_index"])
        validate_observation(failure["observed"])
        observed = failure["observed"]
        if "value" in observed:
            expected = task["cases"][failure["case_index"]]["expected"]
            need(type(observed["value"]) is not type(expected) or observed["value"] != expected,
                 "validator_false_failure")
    need(indices == sorted(set(indices)), "validator_case_order")
    need(sum(f["observed"]["kind"] == "exception" for f in failures) == evidence["errors"],
         "validator_error_count")
    return evidence


def candidate_fingerprint(source: str, task: dict) -> str:
    """Ignore comments, quote style and whitespace, but never bypass the AST guard."""
    return sha256(ast.dump(guarded(source, task), include_attributes=False).encode())


def proposal_spec(proposal: dict) -> dict:
    """Internal proposal envelope; the public/trusted task schema remains unchanged."""
    need(type(proposal) is dict, "proposal_schema")
    task = {key: value for key, value in proposal.items() if key != "feedback"}
    task_spec(task)
    if "feedback" not in proposal:
        return task
    feedback = proposal["feedback"]
    need(type(feedback) is dict
         and set(feedback) == {"schema_version", "candidate_sha256", "failed_count", "failures"}
         and type(feedback["schema_version"]) is int and feedback["schema_version"] == 1
         and type(feedback["candidate_sha256"]) is str
         and re.fullmatch(r"[0-9a-f]{64}", feedback["candidate_sha256"]) is not None
         and type(feedback["failed_count"]) is int
         and 1 <= feedback["failed_count"] <= len(task["cases"]),
         "feedback_schema")
    failures = feedback["failures"]
    need(type(failures) is list and 1 <= len(failures) <= min(8, feedback["failed_count"]),
         "feedback_failures")
    indices = []
    for failure in failures:
        need(type(failure) is dict
             and set(failure) == {"case_index", "args", "expected", "observed"}
             and type(failure["case_index"]) is int
             and 0 <= failure["case_index"] < len(task["cases"]), "feedback_case")
        case = task["cases"][failure["case_index"]]
        # Compare JSON, not Python equality (True must not substitute 1).
        need(json.dumps({"args": failure["args"], "expected": failure["expected"]},
                        sort_keys=True, allow_nan=False)
             == json.dumps(case, sort_keys=True, allow_nan=False), "feedback_case_changed")
        validate_observation(failure["observed"])
        indices.append(failure["case_index"])
    need(indices == sorted(set(indices)), "feedback_case_order")
    need(len(json.dumps(feedback, ensure_ascii=False).encode()) <= FEEDBACK_LIMIT,
         "feedback_size")
    return task


def semantic_feedback(task: dict, source: str, report: dict) -> dict:
    validate_report(report, task)
    failures = report["failures"]
    need(bool(failures), "semantic_feedback_missing")
    feedback = {"schema_version": 1, "candidate_sha256": candidate_fingerprint(source, task),
                "failed_count": len(failures), "failures": []}
    # Bounded model context, complete count retained; all cases are still validated.
    for failure in failures[:8]:
        case = task["cases"][failure["case_index"]]
        entry = {"case_index": failure["case_index"], **copy.deepcopy(case),
                 "observed": copy.deepcopy(failure["observed"])}
        trial = {**feedback, "failures": feedback["failures"] + [entry]}
        if len(json.dumps(trial, ensure_ascii=False).encode()) > FEEDBACK_LIMIT:
            break
        feedback = trial
    proposal_spec({**task, "feedback": feedback})
    return feedback


def validate(source: str, task: dict) -> dict:
    """Independent interpreter process, no imports of candidate modules."""
    task_spec(task)
    guarded(source, task)
    payload = json.dumps({"source": extracted(source, task), "task": task}, allow_nan=False)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--validate"],
            input=payload, capture_output=True, text=True, timeout=5, check=False,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StructuredBlocked("validator_unavailable") from None
    need(result.returncode == 0 and len(result.stdout) <= 131072, "validator_process")
    try:
        evidence = json.loads(result.stdout)
    except ValueError:
        raise StructuredBlocked("validator_json") from None
    return validate_report(evidence, task)


def _validate_child() -> None:
    # This subprocess has no provider credentials. AST controls are repeated here.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (2, 3))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except ImportError:
        raise StructuredBlocked("resource_limits_required") from None
    raw = sys.stdin.read(125001)
    need(len(raw) <= 125000, "validator_input_size")
    data = json.loads(raw)
    task = task_spec(data["task"])
    tree = guarded(data["source"], task)
    scope = {"__builtins__": BUILTINS.copy()}
    exec(compile(tree, "<guarded-json-function>", "exec"), scope)
    passed = errors = 0
    failures = []
    for index, case in enumerate(task["cases"]):
        try:
            result = scope[task["function"]](*copy.deepcopy(case["args"]))
            json_value(result)
            if type(result) is type(case["expected"]) and result == case["expected"]:
                passed += 1
                continue
            observed = observation(result)
        except Exception as error:
            errors += 1
            category = "InvalidResult" if isinstance(error, StructuredBlocked) else type(error).__name__
            observed = {"kind": "exception",
                        "type": category if category in EXCEPTION_TYPES else "Exception"}
        failures.append({"case_index": index, "observed": observed})
    print(json.dumps({"passed": passed, "total": len(task["cases"]), "errors": errors,
                      "failures": failures}, ensure_ascii=True))


def git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True,
            timeout=15, check=False,
            env={"PATH": os.environ.get("PATH", os.defpath),
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                 "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StructuredBlocked("git_unavailable") from None
    need(result.returncode == 0 and len(result.stdout) <= 1_048_576, "git_operation")
    return result.stdout.strip()


def clean_git_configuration(root: Path) -> None:
    """Reject executable Git configuration before running Git in the copy."""
    folder = root / ".git"
    need(folder.is_dir() and not folder.is_symlink(), "isolated_git_directory")
    config = folder / "config"
    need(config.is_file() and not config.is_symlink() and config.stat().st_size <= 16384,
         "git_config_file")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(config.read_text(encoding="utf-8"))
    except (OSError, configparser.Error):
        raise StructuredBlocked("git_configuration_invalid") from None
    allowed = {"repositoryformatversion", "filemode", "bare", "logallrefupdates",
               "ignorecase", "precomposeunicode"}
    for section in parser.sections():
        if section == "core":
            need(set(parser[section]) <= allowed, "git_configuration_unsafe")
            need(parser[section].get("bare", "false") == "false", "bare_repository")
        else:
            # A clone may retain a tracking branch after its remote is removed.
            need(section.startswith('branch "') and set(parser[section]) <= {"remote", "merge"},
                 "git_configuration_unsafe")
    hooks = folder / "hooks"
    need(not hooks.is_symlink() and (not hooks.exists() or
         all(item.is_file() and item.name.endswith(".sample")
             for item in hooks.iterdir())), "active_git_hooks")
    need(not (folder / "objects/info/alternates").exists(), "shared_git_objects")


def regular(root: Path, relative: str) -> Path:
    path = root / relative
    need(root.is_dir() and not root.is_symlink()
         and path.parent.is_dir() and not path.parent.is_symlink()
         and path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1
         and path.parent.parent.resolve() == root.resolve(), "target_not_regular")
    return path


def repair(root: Path, state: Path, task: dict, propose: Callable[[dict, str], str]) -> dict:
    """Modify only a trusted function, in an originless, clean worker/* Git copy."""
    task_spec(task)
    need(not root.is_symlink() and not state.is_symlink(), "symlink_root")
    root, state = root.resolve(), state.resolve()
    need(root != state and root not in state.parents and state not in root.parents,
         "state_location")
    state.mkdir(parents=True, exist_ok=True)
    identity = sha256(str(root).encode())
    lock, receipt = state / (identity + ".lock"), state / (identity + ".json")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StructuredBlocked("worker_busy") from None
    os.close(fd)
    task_hash = sha256(json.dumps(task, sort_keys=True).encode())
    try:
        path = regular(root, task["path"])
        clean_git_configuration(root)
        need(git(root, "remote") == "", "remote_repository")
        need(git(root, "branch", "--show-current").startswith("worker/"), "worker_branch")
        need(git(root, "status", "--porcelain", "--untracked-files=all") == "", "dirty_worktree")
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            need(previous.get("task_hash") == task_hash
                 and previous.get("produced_sha") == git(root, "rev-parse", "HEAD")
                 and previous.get("after_sha256") == sha256(path.read_bytes()), "replay_changed")
            checked = validate(path.read_text(), task)
            need(checked["passed"] == checked["total"], "replay_validation")
            need(sha256((state / "change.patch").read_bytes()) == previous["patch_sha256"],
                 "replay_patch_changed")
            return {**previous, "replayed": True, "model_calls": 0}
        before_bytes = path.read_bytes()
        before = before_bytes.decode("utf-8")
        need(git(root, "rev-parse", "HEAD") == task["base_sha"]
             and sha256(before_bytes) == task["before_sha256"], "source_changed")
        baseline = validate(before, task)
        need(baseline["passed"] < baseline["total"], "no_failing_baseline")
        # One initial proposal and at most one correction after REAL semantic failures.
        # Syntax, grammar, safety, no-change and infrastructure failures are terminal.
        proposal_task = copy.deepcopy(task)
        proposal_source = extracted(before, task)
        attempts = []
        seen_candidates = set()
        for attempt in (1, 2):
            need(git(root, "rev-parse", "HEAD") == task["base_sha"]
                 and git(root, "status", "--porcelain", "--untracked-files=all") == ""
                 and sha256(path.read_bytes()) == task["before_sha256"], "concurrent_change")
            after = render(before, task, propose(copy.deepcopy(proposal_task), proposal_source))
            need(after != before, "no_change")
            fingerprint = candidate_fingerprint(after, task)
            need(fingerprint not in seen_candidates, "candidate_repeated")
            seen_candidates.add(fingerprint)
            checked = validate(after, task)
            attempts.append({"attempt": attempt, "candidate_sha256": sha256(after.encode()),
                             "validation": checked})
            if checked["passed"] == checked["total"] and checked["errors"] == 0:
                break
            if attempt == 2:
                raise StructuredBlocked("candidate_tests_failed")
            # Reuse observed results from the same validation pass, not 42 subprocess reruns.
            # Keep the authoritative instruction and the ordered acceptance cases intact.
            proposal_task = {**copy.deepcopy(task),
                             "feedback": semantic_feedback(task, after, checked)}
            proposal_source = extracted(after, task)
        need(git(root, "rev-parse", "HEAD") == task["base_sha"]
             and git(root, "status", "--porcelain", "--untracked-files=all") == ""
             and sha256(path.read_bytes()) == task["before_sha256"], "concurrent_change")
        regular(root, task["path"])
        path.write_text(after, encoding="utf-8", newline="")
        need(git(root, "diff", "--name-only") == task["path"], "change_scope")
        git(root, "diff", "--check")
        patch = git(root, "diff", "--", task["path"]) + "\n"
        git(root, "add", "--", task["path"])
        need(git(root, "diff", "--cached", "--name-only") == task["path"], "staged_scope")
        git(root, "-c", "user.name=Structured Code Worker", "-c", "user.email=worker@localhost",
            "commit", "-m", "fix: validated structured-function repair")
        produced = git(root, "rev-parse", "HEAD")
        need(produced != task["base_sha"] and git(root, "status", "--porcelain") == ""
             and git(root, "diff", "--name-only", task["base_sha"], produced) == task["path"],
             "commit_not_proved")
        need(validate(path.read_text(), task) == checked, "post_commit_validation")
        result = {
            "result": "STRUCTURED_CODE_REPAIRED", "task_id": task["task_id"],
            "task_hash": task_hash, "base_sha": task["base_sha"], "produced_sha": produced,
            "path": task["path"], "function": task["function"],
            "before_sha256": sha256(before_bytes), "after_sha256": sha256(path.read_bytes()),
            "baseline": baseline, "validation": checked, "patch_sha256": sha256(patch.encode()),
            "replayed": False, "model_calls": len(attempts), "attempts": attempts,
        }
        (state / "change.patch").write_text(patch, encoding="utf-8")
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        temporary.replace(receipt)
        return result
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        need(sys.argv[1:] == ["--validate"], "validator_arguments")
        _validate_child()
    except Exception:
        print('{"result":"STRUCTURED_VALIDATION_BLOCKED"}')
        raise SystemExit(2)
