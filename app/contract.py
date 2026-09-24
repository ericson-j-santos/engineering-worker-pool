from __future__ import annotations

CONTRACT_NAME = "engineering-worker-pool"
CONTRACT_VERSION = "v1"
SERVICE_NAME = "codex-worker-pool"
COMPATIBILITY_POLICY = "backward-compatible-additive"


def descriptor() -> dict[str, str]:
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "service_name": SERVICE_NAME,
        "compatibility_policy": COMPATIBILITY_POLICY,
    }
