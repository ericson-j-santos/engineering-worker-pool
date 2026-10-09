"""Prepare the existing pinned CI image; no installation, inference or cloud fallback.

One retry is allowed only for an observed registry timeout and after a fresh
read of the public authorization endpoint. Other failures stop immediately.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tempfile

import httpx

DIGEST = "sha256:7ab595e4ead391f6818c7215297781282babe0701f6d9a9f8862ac591360a58b"
IMAGE = "ollama/ollama:0.34.3@" + DIGEST
REPO_DIGEST = "ollama/ollama@" + DIGEST
AUTH_URL = "https://auth.docker.io/token"
PARAMS = {"service": "registry.docker.io", "scope": "repository:ollama/ollama:pull"}


class PreparationBlocked(RuntimeError):
    """Sanitized fixed reasons only."""


def command(arguments):
    try:
        # Public image only: do not inherit runner login, credential helpers,
        # remote Docker contexts or user-specific client configuration.
        with tempfile.TemporaryDirectory(prefix="worker-public-image-") as config:
            return subprocess.run(arguments, capture_output=True, text=True,
                                  timeout=75, check=False,
                                  env={"PATH": os.environ.get("PATH", os.defpath),
                                       "DOCKER_CONFIG": config})
    except (OSError, subprocess.TimeoutExpired):
        raise PreparationBlocked("image_process_unavailable") from None


def authorization_ready():
    # This requests public pull readiness, not a user's credentials. Never log its body.
    try:
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", AUTH_URL, params=PARAMS) as response:
                if response.status_code != 200:
                    return False
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 16384:
                        return False
            data = json.loads(body)
            return (type(data) is dict and type(data.get("token")) is str
                    and 0 < len(data["token"]) <= 8192
                    and type(data.get("expires_in")) is int and data["expires_in"] > 0)
    except (httpx.HTTPError, ValueError):
        return False


def transient_timeout(result):
    text = (result.stderr + result.stdout).lower()
    # Never retry authorization, quota, content or digest errors.
    if any(word in text for word in ("unauthorized", "denied", "toomanyrequests",
                                     "manifest unknown", "not found", "digest mismatch")):
        return False
    return ("auth.docker.io/" in text or "registry-1.docker.io/" in text) and any(
        word in text for word in ("context deadline exceeded", "client.timeout exceeded")
    )


def prepare():
    for attempt in (1, 2):
        pulled = command(["docker", "pull", IMAGE])
        if pulled.returncode == 0:
            observed = command(["docker", "image", "inspect", "--format",
                                "{{json .RepoDigests}}", IMAGE])
            try:
                digests = json.loads(observed.stdout)
            except (TypeError, ValueError):
                raise PreparationBlocked("image_digest_unavailable") from None
            if (observed.returncode != 0 or type(digests) is not list
                    or REPO_DIGEST not in digests):
                raise PreparationBlocked("image_digest_mismatch")
            return attempt
        transient = transient_timeout(pulled)
        if attempt == 2 or not transient:
            raise PreparationBlocked("image_pull_timeout" if transient else "image_pull_failed")
        time.sleep(2)
        if not authorization_ready():
            raise PreparationBlocked("image_registry_not_ready")
        print("IMAGE_REGISTRY_READINESS_RESTORED", flush=True)
    raise PreparationBlocked("image_pull_failed")


def main():
    if (sys.argv[1:] or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY")
            != "ericson-j-santos/engineering-worker-pool"):
        print("IMAGE_PREPARATION_BLOCKED ci_identity")
        return 2
    try:
        attempts = prepare()
        print("PINNED_IMAGE_VERIFIED attempts=" + str(attempts))
        return 0
    except PreparationBlocked as error:
        print("IMAGE_PREPARATION_BLOCKED " + str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
