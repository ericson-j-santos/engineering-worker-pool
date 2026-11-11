"""Read-only real Qwen7B repair probe on a temporary GitHub-hosted VM."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from scripts.issue61_qwen_patch_contract import PatchRejected, extract_model_patch

REPO = "ericson-j-santos/engineering-worker-pool"
REF = "refs/heads/cas/issue61-gate-contract-20261111-v1"
URL = "https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-linux-amd64.tar.zst"
BIN_SHA = "87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d"
MODEL = "qwen2.5-coder:7b"
MODEL_PREFIX = "dae161e27b0e"
HOST = "http://127.0.0.1:11434"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def preflight(env):
    if (env.get("GITHUB_REPOSITORY") != REPO
            or env.get("GITHUB_REF") != REF
            or env.get("GITHUB_ACTOR") != "ericson-j-santos"
            or env.get("GITHUB_EVENT_NAME") != "push"
            or env.get("GITHUB_ACTIONS") != "true"
            or not re.fullmatch("[a-f0-9]{40}", env.get("GITHUB_SHA", ""))):
        raise RuntimeError("OWNER_EVENT_SCOPE_DENIED")
    original = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,191}\Z")
    if original.fullmatch("TODO-24") or original.fullmatch("TODO-54"):
        raise RuntimeError("BASELINE_DEFECT_NOT_REPRODUCED")


def call(path, data=None, timeout=20):
    raw = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(HOST + path, data=raw, method="GET" if data is None else "POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("OLLAMA_LOCAL_HTTP_ERROR")
        return json.loads(response.read(200000))


def download(target):
    h = hashlib.sha256()
    count = 0
    with urllib.request.urlopen(URL, timeout=60) as reader, target.open("xb") as writer:
        if reader.status != 200:
            raise RuntimeError("OLLAMA_BINARY_DOWNLOAD_FAILED")
        while True:
            block = reader.read(1048576)
            if not block:
                break
            count += len(block)
            if count > 1700000000:
                raise RuntimeError("OLLAMA_BINARY_SIZE_LIMIT")
            h.update(block)
            writer.write(block)
    if h.hexdigest() != BIN_SHA:
        raise RuntimeError("OLLAMA_BINARY_SHA_MISMATCH")


def pull():
    body = json.dumps({"model": MODEL, "stream": True}).encode()
    req = urllib.request.Request(HOST + "/api/pull", data=body, method="POST")
    final = False
    with urllib.request.urlopen(req, timeout=150) as stream:
        while True:
            line = stream.readline()
            if not line:
                break
            if len(line) > 20000:
                raise RuntimeError("OLLAMA_PULL_INVALID")
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError("OLLAMA_PULL_FAILED")
            if data.get("status") == "success":
                final = True
    if not final:
        raise RuntimeError("OLLAMA_PULL_INCOMPLETE")


def failed_cases(pattern):
    if not isinstance(pattern, str) or len(pattern) > 90 or "\n" in pattern:
        return ["INVALID_PATTERN_FORMAT"]
    try:
        rx = re.compile(pattern)
    except re.error:
        return ["INVALID_PATTERN_SYNTAX"]
    good = ("TODO-1", "TODO-24", "TODO-54", "TODO-999")
    bad = ("TODO-0", "TODO-54-validation", "TODO-54/../", "todo-54", "TODO-54\n", "")
    errors = ["MISSING_VALID_" + repr(value) for value in good
              if rx.fullmatch(value) is None]
    errors.extend("ACCEPTED_INVALID_" + repr(value) for value in bad
                  if rx.fullmatch(value) is not None)
    return errors


def accepted(pattern):
    return not failed_cases(pattern)


def main():
    preflight(os.environ)
    with tempfile.TemporaryDirectory(prefix="ewp61-ollama-") as rootname:
        root = Path(rootname)
        if shutil.disk_usage(root).free < 9000000000:
            raise RuntimeError("FREE_HOSTED_DISK_INSUFFICIENT")
        archive = root / "ollama.tar.zst"
        target = root / "binary"
        target.mkdir()
        download(archive)
        subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(target)], timeout=90, check=True)
        binary = target / "bin" / "ollama"
        if not binary.is_file():
            raise RuntimeError("OLLAMA_BINARY_NOT_FOUND")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_MODELS": str(root / "models"),
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_DEBUG": "false",
        }
        with (root / "ollama.log").open("wb") as log:
            server = subprocess.Popen([str(binary), "serve"], cwd=str(root), env=env,
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(60):
                    if server.poll() is not None:
                        raise RuntimeError("OLLAMA_SERVER_EXIT")
                    try:
                        call("/api/tags", timeout=2)
                        break
                    except (OSError, ValueError):
                        time.sleep(1)
                else:
                    raise RuntimeError("OLLAMA_SERVER_UNAVAILABLE")
                pull()
                models = call("/api/tags").get("models", [])
                model = next((x for x in models if x.get("name") == MODEL), None)
                digest = model.get("digest", "") if model else ""
                if not re.fullmatch("[a-f0-9]{64}", digest) or not digest.startswith(MODEL_PREFIX):
                    raise RuntimeError("QWEN_DIGEST_PIN_INVALID")
                # New substantive output contract: author a replacement Python
                # assignment (not a generic regex). Original broken source remains
                # unchanged; output is AST-inspected, never evaluated or executed.
                prompt = (
                    "You are repairing one REAL Python validation bug. "
                    "In app/pre_pr_cas_gate.py the old validator is "
                    "IDENT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,191}\\Z'), "
                    "which incorrectly rejects canonical identifiers TODO-24 "
                    "and TODO-54. Generate one new Python assignment named TODO_ID "
                    "that uses re.compile with a literal regex. "
                    "Do not alter unrelated validators. "
                    "The regex must accept TODO-1, TODO-24, TODO-54, "
                    "TODO-999 and TODO-123456; reject TODO-0, TODO-00, "
                    "TODO-01, TODO-54-validation, TODO-54/.., lowercase todo-54, "
                    "TODO-54 followed by a newline, TODO-54 followed by a space, "
                    "TODO--1, and the empty string. "
                    "Your replacement_line should look like a single Python "
                    "assignment to TODO_ID, with a raw string regex inside "
                    "re.compile; your own pattern must be inferred from tests. "
                    "Return only a JSON object with exactly replacement_line."
                )
                schema = {
                    "type": "object",
                    "properties": {"replacement_line": {"type": "string"}},
                    "required": ["replacement_line"],
                    "additionalProperties": False,
                }
                request = {
                    "model": MODEL,
                    "system": "You are a Python bug repair model. Return only the requested JSON schema. No prose, Markdown or hidden commands.",
                    "prompt": prompt, "stream": False, "format": schema,
                    "options": {"temperature": 0, "num_predict": 256,
                                "num_ctx": 2048, "num_thread": 4,
                                "seed": 6111}
                }
                print("QWEN_BINARY_AND_MODEL_DIGEST_VERIFIED=" + digest, flush=True)
                result = call("/api/generate", request, timeout=300)
                if result.get("done") is not True or result.get("model") != MODEL:
                    raise RuntimeError("QWEN_INFERENCE_UNCONFIRMED")
                if not isinstance(result.get("eval_count"), int) or result["eval_count"] <= 0:
                    raise RuntimeError("QWEN_NO_GENERATED_TOKENS")
                reply = result.get("response", "")
                if not isinstance(reply, str) or len(reply) > 1500:
                    raise RuntimeError("QWEN_REPLY_UNTRUSTED")
                try:
                    replacement_line, pattern = extract_model_patch(reply)
                except PatchRejected as exc:
                    error_code = str(exc)
                    print("QWEN_CODE_REPAIR_REJECTED=" + json.dumps({
                        "error_code": error_code,
                        "model_digest": digest,
                        "reply_sha256": sha(reply.encode()),
                        "eval_count": result["eval_count"],
                        "source_sha": "2504c5e123f6e7cd607ea77fde80cbebf46d2790",
                    }, sort_keys=True), flush=True)
                    raise RuntimeError("QWEN_MODEL_CODE_PATCH_FAILED_ACCEPTANCE") from None
                # This is the model-authored code, safe only after AST and tests.
                # It is NOT an authorized publication or a Validator attestation.
                print("QWEN_AUTHORED_PATCH_LINE=" + replacement_line, flush=True)
                receipt = {
                    "status": "REAL_QWEN_REPAIR_PROBE_SUCCESS",
                    "issue": 61, "not_todo54_admission": True,
                    "model": MODEL, "model_digest": digest,
                    "ollama_release": "v0.40.16", "ollama_release_sha256": BIN_SHA,
                    "run_id": os.environ.get("GITHUB_RUN_ID"),
                    "github_sha": os.environ["GITHUB_SHA"],
                    "prompt_sha256": sha(prompt.encode()),
                    "reply_sha256": sha(reply.encode()),
                    "regex_sha256": sha(pattern.encode()),
                    "replacement_line_sha256": sha(replacement_line.encode()),
                    "authored_replacement_line": replacement_line,
                    "eval_count": result["eval_count"], "inference_attempts": 1,
                    "baseline_failed": True,
                    "positive_negative_tests_pass": True,
                    "builder_code_not_published": True,
                    "source_sha": "2504c5e123f6e7cd607ea77fde80cbebf46d2790",
                    "validator_independent_not_attested": True,
                    "ready_for_pr": False,
                }
                print("EWP61_MODEL_RECEIPT=" + json.dumps(receipt, sort_keys=True), flush=True)
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                    summary.write("## Issue 61: real native Qwen7B probe\n\n")
                    summary.write("Model digest: " + digest + "\n\n")
                    summary.write("Generated tokens: " + str(result["eval_count"]) + "\n\n")
                    summary.write("Positive/negative tests: PASS\n\n")
                    summary.write("READY_FOR_PR=false; no TODO-54 admission or independent Validator\n")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    main()
