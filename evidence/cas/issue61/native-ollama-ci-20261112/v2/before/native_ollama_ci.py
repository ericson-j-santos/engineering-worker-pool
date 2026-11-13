"""Isolated, fail-closed, native Ollama CI lifecycle; no Docker or host install.

Only the trusted same-repository PR job may start this disposable service. The
owner's CI controls exact head SHA, disk capacity, process identity and model IDs.
This tool does not itself grant READY_FOR_PR or attest a Validator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

REPO = "ericson-j-santos/engineering-worker-pool"
URL = "https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-linux-amd64.tar.zst"
ARCHIVE_SHA256 = "87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d"
MODEL_PINS = {"initial": ("qwen2.5-coder:1.5b", "d7372fd82851"),
              "structured": ("qwen2.5-coder:7b", "dae161e27b0e")}
HOST = "http://127.0.0.1:11434"
MAX_ARCHIVE_BYTES = 1_700_000_000
MIN_DISK_BYTES = 12 * (1024 ** 3)
SHA40 = re.compile(r"^[a-f0-9]{40}$")
SHA64 = re.compile(r"^[a-f0-9]{64}$")


class Blocked(RuntimeError):
    """Constant failure vocabulary; never forward secrets or raw API responses."""


def need(ok: bool, code: str) -> None:
    if not ok:
        raise Blocked(code)


def admission(env: dict[str, str], *, head: str) -> Path:
    need(env.get("GITHUB_ACTIONS") == "true", "NOT_GITHUB_ACTIONS")
    need(env.get("GITHUB_REPOSITORY") == REPO, "REPOSITORY_NOT_ADMITTED")
    need(env.get("GITHUB_EVENT_NAME") == "pull_request", "EVENT_NOT_ADMITTED")
    need(env.get("GITHUB_ACTOR") == "ericson-j-santos", "ACTOR_NOT_ADMITTED")
    expected = env.get("EXPECTED_SHA", "")
    need(bool(SHA40.fullmatch(expected)), "EXPECTED_HEAD_SHA_INVALID")
    need(head == expected, "HEAD_CHANGED")
    event_path = Path(env.get("GITHUB_EVENT_PATH", ""))
    need(event_path.is_file(), "EVENT_PAYLOAD_MISSING")
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Blocked("EVENT_PAYLOAD_INVALID") from None
    pr = event.get("pull_request", {})
    need(isinstance(pr, dict)
         and pr.get("head", {}).get("sha") == head
         and pr.get("head", {}).get("repo", {}).get("full_name") == REPO
         and pr.get("base", {}).get("ref") == "main", "PR_HEAD_OR_BASE_NOT_ADMITTED")
    need(platform.system() == "Linux" and platform.machine() == "x86_64", "NATIVE_PLATFORM_INVALID")
    tmp = env.get("RUNNER_TEMP", "")
    need(bool(tmp), "RUNNER_TEMP_MISSING")
    root = Path(tmp).resolve(strict=True) / "ewp-native-ollama-ci"
    need(not root.is_symlink(), "ROOT_SYMLINK_DENIED")
    return root


def api(path: str, payload: dict | None = None, timeout: int = 15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(HOST + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if payload is None else "POST")
    with opener.open(request, timeout=timeout) as res:
        need(res.status == 200, "LOCAL_API_RESPONSE_INVALID")
        return json.loads(res.read(128_001))


def process_starttime(pid: int) -> str:
    # Linux /proc/stat field 22; only a numeric token, never log process arguments.
    raw = Path(f"/proc/{pid}/stat").read_text("utf-8")
    return raw.rsplit(")", 1)[1].split()[19]


def process_identity(pid: int, binary: Path, ticks: str = "") -> bool:
    try:
        return (pid > 1 and Path(f"/proc/{pid}/exe").resolve(strict=True) == binary.resolve(strict=True)
                and (not ticks or process_starttime(pid) == ticks))
    except (OSError, ValueError):
        return False


def state(root: Path) -> tuple[int, Path]:
    try:
        obj = json.loads((root / "state.json").read_text("utf-8"))
        pid, binary = obj["pid"], root / "binary" / "bin" / "ollama"
        ticks = obj["start_ticks"]
        need(type(pid) is int and type(ticks) is str and ticks.isdecimal()
             and obj["head_sha"] == os.environ["EXPECTED_SHA"]
             and process_identity(pid, binary, ticks), "OWNED_PROCESS_NOT_VERIFIED")
    except (OSError, ValueError, KeyError, TypeError):
        raise Blocked("OWNED_PROCESS_NOT_VERIFIED") from None
    return pid, binary


def streamed_archive(target: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    # Fixed HTTPS URL and pinned complete archive SHA; no installers are executed.
    with urllib.request.urlopen(URL, timeout=75) as res, target.open("xb") as dst:
        need(res.status == 200, "ARCHIVE_HTTP_DENIED")
        while True:
            chunk = res.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            need(size <= MAX_ARCHIVE_BYTES, "ARCHIVE_TOO_LARGE")
            dst.write(chunk)
            digest.update(chunk)
    need(digest.hexdigest() == ARCHIVE_SHA256, "ARCHIVE_HASH_MISMATCH")


def start(root: Path) -> None:
    need(not root.exists(), "NATIVE_SESSION_ALREADY_EXISTS")
    need(shutil.disk_usage(root.parent).free >= MIN_DISK_BYTES, "DISK_CAPACITY_UNPROVED")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port:
        port.settimeout(1)
        need(port.connect_ex(("127.0.0.1", 11434)) != 0, "LOOPBACK_PORT_ALREADY_OWNED")
    root.mkdir(mode=0o700)
    archive = root / "ollama.tar.zst"
    streamed_archive(archive)
    folder = root / "binary"
    folder.mkdir(mode=0o700)
    subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(folder)], check=True, timeout=120)
    archive.unlink()
    binary = folder / "bin" / "ollama"
    need(binary.is_file() and os.access(binary, os.X_OK), "PINNED_BINARY_MISSING")
    env = os.environ.copy()
    env.update({"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_MODELS": str(root / "models"), "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1", "RUNNER_TRACKING_ID": ""})
    with (root / "native.log").open("wb") as log:
        proc = subprocess.Popen([str(binary), "serve"], cwd=root, env=env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (root / "state.json").write_text(json.dumps({"pid": proc.pid, "start_ticks": process_starttime(proc.pid),
                                                 "head_sha": os.environ["EXPECTED_SHA"]}), "utf-8")
    for _ in range(45):
        need(proc.poll() is None, "NATIVE_DAEMON_EXITED")
        try:
            api("/api/tags", timeout=2)
            need(process_identity(proc.pid, binary), "NATIVE_DAEMON_IDENTITY_INVALID")
            with Path(os.environ["GITHUB_PATH"]).open("a", encoding="utf-8") as out:
                out.write(str(binary.parent) + "\n")
            print("NATIVE_OLLAMA_STARTED_VERIFIED")
            return
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(1)
    raise Blocked("NATIVE_DAEMON_NOT_READY")


def model_digest(model: str, prefix: str) -> str:
    catalog = api("/api/tags")
    models = catalog.get("models", [])
    found = [v for v in models if v.get("name") == model]
    need(len(found) == 1, "MODEL_NOT_IN_INVENTORY")
    digest = found[0].get("digest", "")
    need(isinstance(digest, str) and SHA64.fullmatch(digest) is not None
         and digest.startswith(prefix), "MODEL_DIGEST_PIN_MISMATCH")
    return digest


def pull_model(root: Path, profile: str) -> None:
    state(root)
    model, prefix = MODEL_PINS[profile]
    # CLI inherits the daemon's exact loopback and private model directory.
    env = os.environ.copy()
    env.update({"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_MODELS": str(root / "models")})
    if profile == "structured":
        binary = root / "binary" / "bin" / "ollama"
        need(model_digest(*MODEL_PINS["initial"]).startswith(MODEL_PINS["initial"][1]),
             "INITIAL_MODEL_NO_LONGER_VALID")
        subprocess.run([str(binary), "stop", MODEL_PINS["initial"][0]],
                       env=env, check=True, timeout=90, stdout=subprocess.DEVNULL)
        subprocess.run([str(binary), "rm", MODEL_PINS["initial"][0]],
                       env=env, check=True, timeout=90, stdout=subprocess.DEVNULL)
    binary = root / "binary" / "bin" / "ollama"
    subprocess.run([str(binary), "pull", model], env=env, check=True, timeout=900,
                   stdout=subprocess.DEVNULL)
    digest = model_digest(model, prefix)
    print("NATIVE_MODEL_PIN_VERIFIED=" + profile + ":" + digest[:12])


def stop(root: Path) -> None:
    pid, _ = state(root)
    ticks = json.loads((root / "state.json").read_text("utf-8"))["start_ticks"]
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not Path(f"/proc/{pid}").exists() or not process_identity(pid, root / "binary" / "bin" / "ollama", ticks):
            shutil.rmtree(root)
            print("NATIVE_SESSION_STOPPED_AND_REMOVED")
            return
        time.sleep(0.5)
    raise Blocked("OWNED_PROCESS_STILL_RUNNING")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("start", "initial", "structured", "stop"))
    args = ap.parse_args()
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=15).strip()
        root = admission(os.environ, head=sha)
        if args.action == "start":
            start(root)
        elif args.action == "stop":
            stop(root)
        else:
            pull_model(root, args.action)
        return 0
    except (Blocked, OSError, subprocess.SubprocessError, ValueError) as err:
        code = str(err) if isinstance(err, Blocked) else "NATIVE_OPERATION_FAILED"
        print("NATIVE_CI_BLOCKED=" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
