"""CI-only native Ollama candidate. Runs an actual model, never Docker or a PC.

The canonical PR gate remains unchanged until equivalence is proven. Both
binary and model are independently checked by digest before any inference.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

REPOSITORY = "ericson-j-santos/engineering-worker-pool"
BRANCH = "feat/task-routing-pinned-or-pool-20261105"
RELEASE_URL = (
    "https://github.com/ollama/ollama/releases/download/v0.34.3/"
    "ollama-linux-amd64.tar.zst"
)
RELEASE_SHA256 = "e83a089fd0cd2f79ee2933cca2085846a2065f497adbc6467c402177c68423f9"
MODEL = "qwen2.5-coder:7b"
MODEL_DIGEST_PREFIX = "dae161e27b0e"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class NativeCiBlocked(RuntimeError):
    """Fixed, sanitized diagnostic codes only."""


def need(ok: bool, reason: str) -> None:
    if not ok:
        raise NativeCiBlocked(reason)


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         timeout: int = 60) -> None:
    """Never reproduce potentially untrusted installer/model/server output."""
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, timeout=timeout, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeCiBlocked("native_process_unavailable_or_timed_out") from None
    need(result.returncode == 0, "native_command_failed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def preflight() -> str:
    """Only the fixed public repository + fixed source branch on hosted Linux."""
    need(os.environ.get("GITHUB_ACTIONS") == "true"
         and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
         and os.environ.get("GITHUB_REF") == "refs/heads/" + BRANCH
         and os.environ.get("GITHUB_EVENT_NAME") == "push"
         and os.environ.get("RUNNER_OS") == "Linux", "ci_identity_rejected")
    sha = os.environ.get("GITHUB_SHA", "")
    need(HEX40.fullmatch(sha) is not None, "ci_sha_invalid")
    need(ROOT.is_dir() and (ROOT / ".git").exists(), "ci_source_missing")
    try:
        observed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeCiBlocked("ci_checkout_invalid") from None
    need(observed.returncode == 0 and observed.stdout.strip() == sha,
         "ci_checkout_sha_mismatch")
    tmp = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
    need(tmp.is_dir() and not tmp.is_symlink()
         and shutil.disk_usage(tmp).free >= 12_000_000_000,
         "ci_scratch_space_unqualified")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", 11434))
        except OSError:
            raise NativeCiBlocked("ci_loopback_busy") from None
    return sha


def _ready(process: subprocess.Popen[bytes]) -> bool:
    with httpx.Client(
        base_url="http://127.0.0.1:11434", timeout=2, trust_env=False,
        follow_redirects=False,
    ) as client:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            need(process.poll() is None, "ollama_process_exited")
            try:
                response = client.get("/api/tags")
                if response.status_code == 200 and type(response.json()) is dict:
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
    return False


def verify_model() -> str:
    with httpx.Client(
        base_url="http://127.0.0.1:11434", timeout=10, trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            response = client.get("/api/tags")
            need(response.status_code == 200, "model_inventory_unavailable")
            models = response.json().get("models")
            need(type(models) is list and len(models) == 1,
                 "model_inventory_ambiguous")
            item = models[0]
            digest = item.get("digest")
            need(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest)
                 and digest.startswith(MODEL_DIGEST_PREFIX)
                 and item.get("name") == MODEL and not item.get("remote_host")
                 and not item.get("remote_model"), "model_identity_unverified")
            details = item.get("details")
            need(type(details) is dict and details.get("format") == "gguf",
                 "model_format_unverified")
            return digest
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise NativeCiBlocked("model_attestation_unavailable") from None



def verify_readonly_rag_e2e(env: dict[str, str]) -> dict[str, object]:
    """Run real PR9 RAG, verify sanitized proof, never surface model/source text.

    The source PR's reviewed head is independently checked by its own module.
    This is NOT the separate Issue #8 owner authorization gate.
    """
    need(env.get("NATIVE_OLLAMA_DEV") == "1"
         and env.get("NATIVE_MODEL_DIGEST_PREFIX") == MODEL_DIGEST_PREFIX
         and env.get("OLLAMA_NO_CLOUD") == "1", "rag_native_context_invalid")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.rag_real_qwen_e2e"],
            cwd=ROOT, env=env, timeout=350, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeCiBlocked("rag_real_process_unavailable_or_timed_out") from None
    need(result.returncode == 0, "rag_real_process_failed")
    need(type(result.stdout) is str and len(result.stdout) <= 16384,
         "rag_receipt_size_invalid")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    need(len(lines) == 1 and len(lines[0]) <= 8192,
         "rag_receipt_shape_invalid")
    try:
        evidence = json.loads(lines[0])
    except (ValueError, TypeError):
        raise NativeCiBlocked("rag_receipt_json_invalid") from None
    need(type(evidence) is dict
         and evidence.get("rag_real_github_and_native_qwen_e2e") is True
         and evidence.get("pr_head_sha") ==
             "b083a23af7c55f96ee1c7f8daabbd51167bf4de6"
         and evidence.get("model_digest_prefix") == MODEL_DIGEST_PREFIX
         and type(evidence.get("model_calls")) is int
         and evidence["model_calls"] >= 2
         and evidence.get("independent_pr_and_source_readback") is True
         and evidence.get("stale_head_negative_no_model_calls") is True
         and evidence.get("no_git_writes_or_worker_dispatch") is True,
         "rag_readonly_evidence_invalid")
    # Print only fixed, evidence-derived structured fields; no source or replies.
    verified = {
        "result": "HOSTED_NATIVE_QWEN7B_PR_RAG_VERIFIED",
        "pr_head_sha": evidence["pr_head_sha"],
        "model_digest_prefix": MODEL_DIGEST_PREFIX,
        "model_calls": evidence["model_calls"],
        "independent_readback": True,
        "read_only": True,
    }
    print(json.dumps(verified, sort_keys=True), flush=True)
    return verified



def run() -> None:
    sha = preflight()
    runner = Path(os.environ["RUNNER_TEMP"]).resolve()
    with tempfile.TemporaryDirectory(prefix="native-model-ci-", dir=runner) as value:
        root = Path(value)
        archive = root / "ollama.tar.zst"
        binary = root / "bin" / "ollama"
        # No root privileges, install script, user account mutation or Docker.
        _run([
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https",
            "--max-time", "240", "--output", str(archive), RELEASE_URL,
        ], cwd=root, timeout=250)
        need(archive.is_file() and 1_400_000_000 <= archive.stat().st_size
             <= 1_450_000_000, "release_size_invalid")
        need(_sha256(archive) == RELEASE_SHA256, "release_digest_invalid")
        print("NATIVE_RELEASE_SHA256_VERIFIED", flush=True)
        _run(["tar", "--zstd", "-xf", str(archive), "-C", str(root)],
             cwd=root, timeout=140)
        archive.unlink()
        need(binary.is_file() and not binary.is_symlink(),
             "native_binary_missing")
        print("NATIVE_BINARY_EXTRACTED", flush=True)
        os.chmod(binary, 0o700)
        models_path = root / "models"
        models_path.mkdir(mode=0o700)
        daemon_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(root),
            "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": str(models_path),
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
        }
        with (root / "ollama.log").open("wb") as output:
            daemon = subprocess.Popen(
                [str(binary), "serve"], cwd=root, env=daemon_env,
                stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                need(_ready(daemon), "native_service_not_ready")
                print("NATIVE_OLLAMA_SERVICE_READY", flush=True)
                # Public GGUF tag is allowed only after immutable binary verification.
                _run([str(binary), "pull", MODEL], cwd=root, env=daemon_env,
                     timeout=295)
                digest = verify_model()
                print("NATIVE_OLLAMA_MODEL_VERIFIED", flush=True)
                app_env = dict(os.environ)
                app_env.update({
                    "PATH": str(binary.parent) + os.pathsep
                    + os.environ.get("PATH", os.defpath),
                    "OLLAMA_NO_CLOUD": "1",
                    "OLLAMA_HOST": "127.0.0.1:11434",
                    "OLLAMA_MODELS": str(models_path),
                    "NATIVE_OLLAMA_DEV": "1",
                    "NATIVE_MODEL_DIGEST_PREFIX": MODEL_DIGEST_PREFIX,
                    "HOSTED_NATIVE_OLLAMA_CI": "1",
                    "EXPECTED_SHA": sha,
                })
                _run([sys.executable, "scripts/structured_worker_e2e.py"],
                     cwd=ROOT, env=app_env, timeout=475)
                from scripts.structured_acceptance import cases
                evidence = ROOT / "artifacts" / "structured-code-e2e" / "evidence.json"
                need(evidence.is_file(), "structured_receipt_missing")
                receipt = json.loads(evidence.read_text(encoding="utf-8"))
                need(
                    receipt.get("result") == "STRUCTURED_CODE_E2E_PASSED"
                    and receipt.get("source_sha") == sha
                    and receipt.get("model_digest") == digest
                    and receipt.get("independent_validator") is True
                    and receipt.get("replayed") is True
                    and len(cases()) == 42
                    and receipt.get("cloud_disabled") is True,
                    "structured_receipt_unverified",
                )
                # Independent GitHub PR context retrieval, with a real model,
                # in the same native loopback environment and immutable source.
                verify_readonly_rag_e2e(app_env)
                print(json.dumps({
                    "result": "HOSTED_NATIVE_QWEN7B_E2E_VERIFIED",
                    "source_sha": sha, "model_digest": digest,
                    "case_count": 42, "independent_readback": True,
                    "docker_used": False, "host_machine_touched": False,
                }, sort_keys=True), flush=True)
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=8)


if __name__ == "__main__":
    try:
        run()
    except (NativeCiBlocked, OSError, ValueError, KeyError,
            subprocess.SubprocessError, json.JSONDecodeError) as error:
        code = str(error) if isinstance(error, NativeCiBlocked) else type(error).__name__
        print("HOSTED_NATIVE_QWEN7B_E2E_BLOCKED " + code, flush=True)
        raise SystemExit(2) from None
