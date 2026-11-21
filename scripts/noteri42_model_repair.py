#!/usr/bin/env python3
"""Noteri #42: one local-Qwen repair of a fixed diagnostic Gateway block.

Only call within a separately authenticated Builder lease/Session Launcher/
Command Gateway Risk2. This script alone does NOT attest the lease, installed
model, durable CAS, validator, zero cost or readiness for PR.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
BASE_SHA = "ce2be12647f27220a40d2ae39188d52f517f31df"
SOURCE = ".github/workflows/noteri-control-plane-probe.yml"
MODEL = "qwen2.5-coder:7b"
ENDPOINT = "http://127.0.0.1:11434"
TASK = re.compile(r"cwp-[0-9a-f]{24}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
OLD = (
    "          if ($LASTEXITCODE -ne 0 -or -not $raw) { throw 'NOTERI_219_GATEWAY_BLOCKED' }\n"
    "          $envelope = @($raw)[-1] | ConvertFrom-Json -ErrorAction Stop"
)
SIMPLE = {
    "$gatewayExit = $LASTEXITCODE",
    "$envelope = @($raw)[-1] | ConvertFrom-Json -ErrorAction Stop",
    "try {", "catch {", "}",
}
THROWS = {
    "throw 'NOTERI_219_GATEWAY_ENVELOPE_MISSING'",
    "throw 'NOTERI_219_GATEWAY_ENVELOPE_INVALID'",
    "throw 'NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED'",
    'throw "NOTERI_219_GATEWAY_POLICY_EXIT_$($envelope.gateway_exit_code)"',
}
IF_ALLOWED = re.compile(
    r"if \((?:[A-Za-z0-9$\s()._-]|@(20|21|22|23|24|25|26)|,)*\) \{\Z"
)


class Blocked(RuntimeError):
    pass


def need(condition: bool, code: str) -> None:
    if not condition:
        raise Blocked(code)


def git(root: Path, *args: str) -> bytes:
    try:
        p = subprocess.run(
            ["git", *args], cwd=root, check=False,
            capture_output=True, timeout=15, env={
                "PATH": os.environ.get("PATH", os.defpath),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("NOTERI42_BUILDER_GIT_UNAVAILABLE") from None
    need(p.returncode == 0 and len(p.stdout) <= 2000000,
         "NOTERI42_BUILDER_GIT_FAILED")
    return p.stdout


def candidate(before: bytes, replacement: str) -> bytes:
    """Reject anything except a bounded model-authored PowerShell diagnostic."""
    need(type(before) is bytes and 100 <= len(before) <= 150000
         and before.count(OLD.encode()) == 1
         and b"rules219_readonly" in before
         and type(replacement) is str and 200 <= len(replacement) <= 3200
         and "\r" not in replacement and "\x00" not in replacement,
         "NOTERI42_MODEL_SOURCE_OR_SHAPE_INVALID")
    lines = replacement.splitlines()
    need(7 <= len(lines) <= 35 and replacement.endswith("}")
         and all(line.startswith("          ") and
                 len(line) <= 170 for line in lines),
         "NOTERI42_MODEL_INDENT_INVALID")
    nest = 0
    for line in lines:
        statement = line.strip()
        if statement in SIMPLE or statement in THROWS:
            pass
        elif IF_ALLOWED.fullmatch(statement) is not None:
            # Only known variables; no command or dynamic expression dispatch.
            names = re.findall(r"\$[A-Za-z_][A-Za-z_0-9]*", statement)
            need(all(name in {"$gatewayExit", "$LASTEXITCODE", "$raw", "$envelope"}
                     for name in names)
                 and not any(token in statement for token in (
                     "$(", ";", "|", "&", "\x60", "invoke", "exit",
                 )), "NOTERI42_MODEL_CONDITION_UNSAFE")
        else:
            raise Blocked("NOTERI42_MODEL_STATEMENT_UNSAFE")
        nest += statement.count("{") - statement.count("}")
        need(0 <= nest <= 5, "NOTERI42_MODEL_BLOCK_UNBALANCED")
    need(nest == 0, "NOTERI42_MODEL_BLOCK_UNBALANCED")
    joined = "\n".join(lines)
    need(all(required in joined for required in (
        "$LASTEXITCODE", "$gatewayExit", "$envelope",
        "ConvertFrom-Json -ErrorAction Stop",
        "NOTERI_219_GATEWAY_POLICY_EXIT_",
        "NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED",
        "if (",
    )), "NOTERI42_MODEL_DIAGNOSTIC_INCOMPLETE")
    after = before.replace(OLD.encode(), replacement.encode("utf-8"))
    need(after != before and after.count(replacement.encode("utf-8")) == 1,
         "NOTERI42_MODEL_EMPTY_REPAIR")
    return after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_kw):
        return None


def ollama(opener, endpoint: str, payload: dict | None = None) -> dict:
    need(endpoint in ("/api/tags", "/api/show", "/api/chat", "/api/ps"),
         "NOTERI42_OLLAMA_PATH_DENIED")
    body = None if payload is None else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    need(body is None or len(body) <= 16384, "NOTERI42_OLLAMA_REQUEST_TOO_LARGE")
    req = urllib.request.Request(
        ENDPOINT + endpoint, data=body,
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=90) as response:
            need(response.geturl() == req.full_url
                 and response.status == 200,
                 "NOTERI42_OLLAMA_REDIRECT_OR_STATUS")
            raw = response.read(65537)
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        raise Blocked("NOTERI42_OLLAMA_UNAVAILABLE") from None
    need(0 < len(raw) <= 65536, "NOTERI42_OLLAMA_REPLY_TOO_LARGE")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Blocked("NOTERI42_OLLAMA_INVALID_JSON") from None
    need(type(value) is dict, "NOTERI42_OLLAMA_BAD_BODY")
    return value


def context(env: dict[str, str]) -> tuple[str, str]:
    """Shape only. Authoritative task/lease/model evidence is external."""
    task, digest = env.get("NOTERI42_TASK_ID", ""), env.get("NOTERI42_MODEL_DIGEST", "")
    need(TASK.fullmatch(task) is not None
         and HEX64.fullmatch(digest) is not None
         and env.get("NOTERI42_BASE_SHA") == BASE_SHA
         and env.get("NOTERI42_BUILDER_ROLE") == "builder"
         and env.get("NOTERI42_GATEWAY_BOUND") == "true"
         and env.get("NOTERI42_BASELINE_READBACK") == "verified"
         and HEX64.fullmatch(env.get("NOTERI42_BASELINE_EVIDENCE_SHA256", "")) is not None
         and env.get("NOTERI42_COST_ZERO_ATTESTED") == "true",
         "NOTERI42_BUILDER_CONTEXT_UNATTESTED")
    return task, digest


def main() -> int:
    need(len(sys.argv) == 1, "NOTERI42_BUILDER_ARGUMENTS_FORBIDDEN")
    task, digest = context(dict(os.environ))
    root = Path.cwd().resolve()
    need(root.is_dir() and not root.is_symlink()
         and Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
         == root and git(root, "rev-parse", "HEAD").decode().strip() == BASE_SHA
         and git(root, "status", "--porcelain", "--untracked-files=all") == b"",
         "NOTERI42_BUILDER_WORKTREE_UNTRUSTED")
    target = root / SOURCE
    need(target.is_file() and not target.is_symlink() and target.stat().st_nlink == 1,
         "NOTERI42_BUILDER_TARGET_UNTRUSTED")
    before = target.read_bytes()
    need(git(root, "show", "HEAD:" + SOURCE) == before,
         "NOTERI42_BUILDER_SOURCE_DRIFT")
    location = Path(git(root, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
    state = location / "noteri42"
    need(not state.is_symlink() and not state.exists(),
         "NOTERI42_BUILDER_ALREADY_ATTEMPTED")
    # No proxy or redirects; no model pull and no remote endpoints.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    models = ollama(opener, "/api/tags").get("models")
    need(type(models) is list and len(models) <= 64 and
         len([m for m in models if type(m) is dict and m.get("name") == MODEL
              and m.get("digest") == digest
              and not m.get("remote_host") and not m.get("remote_model")]) == 1,
         "NOTERI42_MODEL_NOT_PINNED_LOCAL")
    meta = ollama(opener, "/api/show", {"model": MODEL})
    need(type(meta.get("details")) is dict and meta["details"].get("format") == "gguf"
         and not meta.get("remote_host") and not meta.get("remote_model"),
         "NOTERI42_MODEL_WEIGHTS_NOT_LOCAL")
    answer = ollama(opener, "/api/chat", {
        "model": MODEL, "stream": False, "keep_alive": "5m",
        "messages": [
            {"role": "system", "content": (
                "Repair only the PowerShell Gateway failure classification. "
                "Return JSON only with a replacement string. Do not output "
                "commands, arbitrary actions, diagnostics containing secrets, "
                "or code outside the reviewed block."
            )},
            {"role": "user", "content": (
                "The existing workflow hides the reason for a failed Command "
                "Gateway risk1 operation. Replace ONLY the two-line block "
                "starting 'if ($LASTEXITCODE -ne 0 -or -not $raw)' and ending "
                "'$envelope = @($raw)[-1] | ConvertFrom-Json -ErrorAction Stop'. "
                "Produce a 7-35 line PowerShell replacement with ten leading "
                "spaces per line, using ONLY $gatewayExit = $LASTEXITCODE, "
                "$envelope = @($raw)[-1] | ConvertFrom-Json -ErrorAction Stop, "
                "if/try/catch, and throw with fixed sanitized codes "
                "NOTERI_219_GATEWAY_ENVELOPE_MISSING, "
                "NOTERI_219_GATEWAY_ENVELOPE_INVALID, "
                "NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED, or "
                "NOTERI_219_GATEWAY_POLICY_EXIT_$($envelope.gateway_exit_code). "
                "Classify a nonzero Gateway exit and require a valid envelope. "
                "Never print stdout, stderr, paths, tokens or source data. "
                "Do not weaken the existing downstream Risk1 receipt guards."
            )},
        ],
        "format": {"type": "object", "properties": {
            "replacement": {"type": "string"}
        }, "required": ["replacement"], "additionalProperties": False},
        "options": {"temperature": 0, "seed": 7, "num_predict": 1000},
    })
    need(answer.get("done") is True and answer.get("model") == MODEL
         and type(answer.get("eval_count")) is int and answer["eval_count"] > 0,
         "NOTERI42_MODEL_INFERENCE_UNPROVED")
    running = ollama(opener, "/api/ps").get("models")
    need(type(running) is list and any(
        type(x) is dict and x.get("name") == MODEL and x.get("digest") == digest
        and type(x.get("size")) is int and x["size"] > 0
        and not x.get("remote_host") and not x.get("remote_model") for x in running
    ), "NOTERI42_MODEL_PROCESS_UNPROVED")
    message = answer.get("message")
    need(type(message) is dict and type(message.get("content")) is str
         and len(message["content"]) <= 6000, "NOTERI42_MODEL_REPLY_MISSING")
    try:
        response = json.loads(message["content"])
    except ValueError:
        raise Blocked("NOTERI42_MODEL_REPLY_INVALID_JSON") from None
    need(type(response) is dict and set(response) == {"replacement"},
         "NOTERI42_MODEL_REPLY_INVALID")
    changed = candidate(before, response["replacement"])
    need(target.read_bytes() == before
         and git(root, "status", "--porcelain", "--untracked-files=all") == b"",
         "NOTERI42_MODEL_SOURCE_MUTATED_PREWRITE")
    # The permanent task controller must read this evidence and independently
    # reconcile the fenced lease before CAS; a sidecar alone is NOT durability.
    state.mkdir(mode=0o700)
    receipt = {
        "result": "NOTERI42_MODEL_PROPOSAL_SEALED",
        "task_id": task, "base_sha": BASE_SHA, "model": MODEL,
        "model_digest": digest, "model_calls_observed": 1,
        "gateway_attested": False, "cas_verified": False,
        "ready_for_pr": False, "source_path": SOURCE,
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(changed).hexdigest(),
        "reply_sha256": hashlib.sha256(message["content"].encode()).hexdigest(),
    }
    with (state / "proposal.json").open("xb") as stream:
        stream.write(json.dumps(receipt, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    # A post-write crash is ambiguous: never rerun; reconcile with the task
    # lease and existing proposal before any new inference.
    target.write_bytes(changed)
    need(target.read_bytes() == changed and
         git(root, "diff", "--name-only", "--no-renames", "-z") == (SOURCE + "\0").encode()
         and git(root, "diff", "--cached", "--name-only", "-z") == b""
         and git(root, "ls-files", "--others", "--exclude-standard", "-z") == b"",
         "NOTERI42_MODEL_APPLY_SCOPE_DRIFT")
    print(json.dumps({
        **receipt, "result": "NOTERI42_MODEL_APPLIED_ONCE",
        "state_changed": True, "secrets_read": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError, UnicodeError) as exc:
        reason = str(exc) if type(exc) is Blocked else "NOTERI42_MODEL_BLOCKED"
        print(json.dumps({
            "result": "NOTERI42_MODEL_BLOCKED", "reason_code": reason,
            "ready_for_pr": False, "secrets_read": False,
            "state_changed_unverified": True,
        }, sort_keys=True))
        raise SystemExit(2) from None
