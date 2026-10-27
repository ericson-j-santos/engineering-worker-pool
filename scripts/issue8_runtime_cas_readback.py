"""Read-only, SHA-pinned cross-repository handoff of a REAL Qwen7B Git bundle.

This is an independent preflight, NOT a Worker Pool completion receipt or a
READY_FOR_PR grant. It cannot enqueue, change a lease, write CAS, or open a PR.
The production pins refer exclusively to the already validated issue #8 run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable

import httpx

WORKER = "ericson-j-santos/engineering-worker-pool"
RUNTIME = "ericson-j-santos/desktop-pc24x7-runtime"
BASE = "87a8de64ea5ed2e4aef1a9cde51861edb61ffe5e"
RUNTIME_BASE = "005cc39d38f90e5d7ae4b3f6e23d4e119ce4120f"
RUNTIME_HEAD = "d359ca203937b00d320860e8bf3035b6bed97b32"
CAS_HEAD = "4d3bebb941aa7c41aa7d3fd86ebe22e0674f46de"
PRODUCED = "537d3ce125a2438b4d9a714ae2b90024186c8986"
CAS_SHA256 = "7eb3ec958b4e214eda0cf0e8c66548e138f120530597b3d6e809609ef2576900"
BEFORE_SHA256 = "c7ebe58abec32fea2f0e56a270b8aa48d37d464fa2177799a285d443a17e4e72"
AFTER_SHA256 = "1f42824250fbaf7d68993b7b238d065621c067d06bd911361ef98bcac94e571f"
PATCH_SHA256 = "3876fd53c300e6434b3abce57a4effa25b3966e5c83f03dc43984bb6e7a6484f"
SOURCE_BLOB = "d9a25c574163c49f695e774609949160d94f4490"
CAS_BRANCH = "agent/issue8-qwen-bundle-87a8de64ea5e"
CAS_PATH = f"evidence/issue8/sha256/{CAS_SHA256[:2]}/{CAS_SHA256}.bundle"
RUN_ID = 40425454934
OWNER_COMMENT = 6430299388
PATH = "app/portfolio_bridge.py"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class HandoffBlocked(RuntimeError):
    """Sanitized reason, never a remote response or an executable command."""


def need(value: bool, reason: str) -> None:
    if not value:
        raise HandoffBlocked(reason)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git_blob(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def decode_cas_file(record: dict[str, Any]) -> bytes:
    need(type(record) is dict and record.get("type") == "file"
         and record.get("path") == CAS_PATH and record.get("encoding") == "base64"
         and type(record.get("content")) is str and type(record.get("size")) is int
         and type(record.get("sha")) is str, "issue8_cas_file_invalid")
    try:
        encoded = "".join(record["content"].split())
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HandoffBlocked("issue8_cas_base64_invalid") from None
    need(200 < len(raw) <= 96_000 and len(raw) == record["size"]
         and base64.b64encode(raw).decode("ascii") == encoded
         and record["sha"] == git_blob(raw) and digest(raw) == CAS_SHA256,
         "issue8_cas_digest_mismatch")
    return raw


class ReadOnlyGitHub:
    """Only GETs to the official GitHub API, no follow-redirect or proxies."""

    def __init__(self, token: str):
        self.client = httpx.Client(timeout=12, trust_env=False, follow_redirects=False)
        self.token = token

    def close(self) -> None:
        self.client.close()

    def __call__(self, route: str) -> Any:
        allowed = {
            f"/repos/{WORKER}/git/ref/heads/main",
            f"/repos/{WORKER}/issues/8",
            f"/repos/{WORKER}/issues/comments/{OWNER_COMMENT}",
            f"/repos/{RUNTIME}/git/ref/heads/main",
            f"/repos/{RUNTIME}/git/ref/heads/{CAS_BRANCH}",
            f"/repos/{RUNTIME}/compare/{RUNTIME_BASE}...{CAS_HEAD}",
            f"/repos/{RUNTIME}/actions/runs/{RUN_ID}",
            f"/repos/{RUNTIME}/actions/runs/{RUN_ID}/jobs",
            f"/repos/{RUNTIME}/contents/{CAS_PATH}?ref={CAS_BRANCH}",
        }
        need(route in allowed, "issue8_cas_route_invalid")
        try:
            response = self.client.get(
                "https://api.github.com" + route,
                headers={"Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28",
                         **({"Authorization": "Bearer " + self.token}
                            if self.token else {})},
            )
        except (httpx.HTTPError, OSError):
            raise HandoffBlocked("issue8_github_unavailable") from None
        need(response.status_code == 200 and not response.history
             and response.url.host == "api.github.com"
             and len(response.content) <= 262144
             and response.headers.get("content-type", "").split(";")[0].strip()
             == "application/json", "issue8_github_response_invalid")
        try:
            result = response.json()
        except ValueError:
            raise HandoffBlocked("issue8_github_json_invalid") from None
        need(type(result) in (dict, list), "issue8_github_json_invalid")
        return result


def remote_readback(get: Callable[[str], Any]) -> bytes:
    """Prove source/owner/run/branch/file from different GitHub API reads."""
    worker_ref = get(f"/repos/{WORKER}/git/ref/heads/main")
    issue = get(f"/repos/{WORKER}/issues/8")
    owner = get(f"/repos/{WORKER}/issues/comments/{OWNER_COMMENT}")
    runtime_ref = get(f"/repos/{RUNTIME}/git/ref/heads/main")
    cas_ref = get(f"/repos/{RUNTIME}/git/ref/heads/{CAS_BRANCH}")
    compare = get(f"/repos/{RUNTIME}/compare/{RUNTIME_BASE}...{CAS_HEAD}")
    run = get(f"/repos/{RUNTIME}/actions/runs/{RUN_ID}")
    jobs = get(f"/repos/{RUNTIME}/actions/runs/{RUN_ID}/jobs")
    need(type(worker_ref) is dict and worker_ref.get("object", {}).get("sha") == BASE
         and type(issue) is dict and issue.get("state") == "open"
         and issue.get("number") == 8 and not issue.get("pull_request")
         and type(owner) is dict and owner.get("id") == OWNER_COMMENT
         and owner.get("user", {}).get("login") == "ericson-j-santos"
         and owner.get("author_association") == "OWNER"
         and owner.get("issue_url") == f"https://api.github.com/repos/{WORKER}/issues/8"
         and owner.get("body") == f"/local-agent authorize bridge.property.v1 sha:{BASE}",
         "issue8_owner_or_source_drift")
    need(type(runtime_ref) is dict and runtime_ref.get("object", {}).get("sha") == RUNTIME_BASE
         and type(cas_ref) is dict and cas_ref.get("object", {}).get("sha") == CAS_HEAD
         and type(compare) is dict and compare.get("status") == "ahead"
         and compare.get("ahead_by") == compare.get("total_commits") == 1
         and compare.get("behind_by") == 0
         and type(compare.get("files")) is list and len(compare["files"]) == 1
         and compare["files"][0].get("filename") == CAS_PATH
         and compare["files"][0].get("status") == "added",
         "issue8_cas_branch_drift")
    required = {
        "Owner issue and SHA admission (no writes)",
        "Dell Qwen7B issue8 fenced Builder and independent checkout",
        "Publish one real model Git bundle in runtime CAS",
        "Independent hosted Validator reads durable CAS and executes real tests",
    }
    need(type(run) is dict and run.get("id") == RUN_ID
         and run.get("head_sha") == RUNTIME_HEAD
         and run.get("status") == "completed" and run.get("conclusion") == "success"
         and type(jobs) is dict and type(jobs.get("jobs")) is list
         and len(jobs["jobs"]) <= 100,
         "issue8_producer_run_unverified")
    observed = [x for x in jobs["jobs"] if type(x) is dict and x.get("name") in required]
    need(len(observed) == len(required) and {x["name"] for x in observed} == required
         and all(x.get("status") == "completed" and x.get("conclusion") == "success"
                 for x in observed), "issue8_producer_gates_unverified")
    physical = next(x for x in observed if x["name"].startswith("Dell Qwen7B"))
    need(physical.get("runner_name") == "DESKTOP-PDQK954-runtime"
         and type(physical.get("runner_id")) is int and physical["runner_id"] > 0,
         "issue8_physical_identity_invalid")
    return decode_cas_file(get(
        f"/repos/{RUNTIME}/contents/{CAS_PATH}?ref={CAS_BRANCH}"
    ))


def safe_git(root: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", os.defpath),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file", "LC_ALL": "C"}
    try:
        p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                           cwd=root, env=env, text=True, capture_output=True,
                           timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HandoffBlocked("issue8_git_unavailable") from None
    need(p.returncode == 0 and len(p.stdout) <= 1_048_576,
         "issue8_git_validation_failed")
    return p.stdout.strip()


def validate_bundle(source_root: Path, raw: bytes) -> dict[str, Any]:
    """Real Git + acceptance + regression verification with zero model calls."""
    need(source_root.is_dir() and not source_root.is_symlink(),
         "issue8_source_root_invalid")
    source_root = source_root.resolve(strict=True)
    need(200 < len(raw) <= 96000 and digest(raw) == CAS_SHA256,
         "issue8_handoff_input_invalid")
    need(safe_git(source_root, "rev-parse", "HEAD") == BASE
         and safe_git(source_root, "status", "--porcelain") == ""
         and safe_git(source_root, "rev-parse", "HEAD:" + PATH) == SOURCE_BLOB,
         "issue8_source_checkout_invalid")
    original = (source_root / PATH).read_bytes()
    need(digest(original) == BEFORE_SHA256,
         "issue8_source_bytes_changed")
    from app.issue_draft_pr import IssueBinding, trusted_task
    from app.structured_code_worker import validate
    from scripts.native_structured_worker_e2e import run_bridge_regressions
    binding = IssueBinding(8, BASE, OWNER_COMMENT,
        "issue-" + digest(f"{WORKER}#8:bridge.property.v1:{BASE}".encode()),
        "qwen7b-issue-8-" + BASE[:12],
        "agent/qwen7b-issue-8-" + BASE[:12])
    task = trusted_task(binding, original)
    baseline = validate(original.decode("utf-8"), task)
    need(baseline["passed"] == 26 and baseline["total"] == 42,
         "issue8_baseline_changed")
    with tempfile.TemporaryDirectory(prefix="issue8-cas-consumer-") as folder:
        home = Path(folder)
        bundle = home / "change.bundle"
        bundle.write_bytes(raw)
        checkout = home / "independent"
        safe_git(home, "clone", "--no-hardlinks", str(source_root), str(checkout))
        safe_git(checkout, "remote", "remove", "origin")
        safe_git(checkout, "bundle", "verify", str(bundle))
        safe_git(checkout, "fetch", "--no-tags", str(bundle), "HEAD")
        safe_git(checkout, "checkout", "--detach", PRODUCED)
        need(safe_git(checkout, "rev-parse", "HEAD") == PRODUCED
             and safe_git(checkout, "rev-list", "--parents", "-n", "1", PRODUCED)
                 == PRODUCED + " " + BASE
             and safe_git(checkout, "rev-list", "--count", BASE + ".." + PRODUCED) == "1"
             and safe_git(checkout, "diff", "--name-only", BASE, PRODUCED) == PATH
             and safe_git(checkout, "status", "--porcelain") == "",
             "issue8_commit_or_scope_invalid")
        after = (checkout / PATH).read_bytes()
        need(after != original and digest(after) == AFTER_SHA256,
             "issue8_patch_bytes_invalid")
        patch = safe_git(checkout, "diff", BASE, PRODUCED, "--", PATH) + "\n"
        need(digest(patch.encode()) == PATCH_SHA256,
             "issue8_patch_digest_invalid")
        cases = validate(after.decode("utf-8"), task)
        need(cases == {"passed": 42, "total": 42, "errors": 0, "failures": []},
             "issue8_acceptance_failed")
        try:
            regression = run_bridge_regressions(checkout, home)
        except Exception:
            raise HandoffBlocked("issue8_regression_execution_blocked") from None
        need(regression.get("category") == "passed"
             and regression.get("exit_code") == 0
             and regression.get("passed_count") >= 52
             and regression.get("failed_count") == 0
             and regression.get("error_count") == 0,
             "issue8_regressions_failed")
        need(safe_git(source_root, "rev-parse", "HEAD") == BASE
             and safe_git(source_root, "status", "--porcelain") == ""
             and digest((source_root / PATH).read_bytes()) == BEFORE_SHA256,
             "issue8_source_integrity_failed")
        return {"result": "ISSUE8_CROSS_REPO_CAS_READBACK_VERIFIED",
                "base_sha": BASE, "produced_sha": PRODUCED,
                "bundle_sha256": CAS_SHA256, "accepted_cases": 42,
                "regressions_passed": regression["passed_count"],
                "model_calls": 0, "cas_read_only": True,
                "worker_pool_authority_completed": False,
                "ready_for_pr": False, "draft_pr_created": False}


def main() -> int:
    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    reader = ReadOnlyGitHub(os.environ.get("GITHUB_TOKEN", ""))
    try:
        raw = remote_readback(reader)
        evidence = validate_bundle(args.source_root, raw)
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except (HandoffBlocked, OSError, UnicodeError, ValueError) as error:
        code = str(error) if type(error) is HandoffBlocked else "issue8_handoff_unverified"
        print(json.dumps({"result": "ISSUE8_CAS_HANDOFF_BLOCKED", "reason": code,
                          "ready_for_pr": False, "draft_pr_created": False}))
        return 2
    except Exception:
        print(json.dumps({"result": "ISSUE8_CAS_HANDOFF_BLOCKED",
                          "reason": "issue8_unexpected_validation_error",
                          "ready_for_pr": False, "draft_pr_created": False}))
        return 2
    finally:
        reader.close()


if __name__ == "__main__":
    sys.exit(main())
