"""Governed, exact Rules #219 manifest command binding. No runtime activation."""
from __future__ import annotations

from app.native_gateway_commands import GovernedCommand
from app.native_one_shot import AdapterDescriptor, NativeExecutorBlocked

REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
RULES_HEAD = "e4f624579eb44f941c11b2cfb35ab65bfa39e8a7"
WRAPPER_PATH = "scripts/builder_manifest_stage.py"
WRAPPER_SHA256 = "422924c9bb5a409135328d41c1d5508ccd1e88da7fae02dc8beead5294ea1010"
COMMAND_KEY = "rules219.manifest.stage"
ADAPTER_ID = "rules219.builder.v1"


def make_stage_spec(*, repository: str, rules_head: str,
                    role: str, descriptor: AdapterDescriptor) -> GovernedCommand:
    """Bind a fixed Builder command; caller must independently attest inputs.

    This validates a command SHAPE, not a trusted Gateway, lease, Builder,
    model, CAS, Validator or READY_FOR_PR. Never use user/issue data as proof.
    """
    if not (type(repository) is str and repository == REPOSITORY
            and type(rules_head) is str and rules_head == RULES_HEAD
            and role == "builder"
            and type(descriptor) is AdapterDescriptor
            and descriptor.adapter_id == ADAPTER_ID
            and descriptor.roles == ("builder", "validator")
            and COMMAND_KEY in descriptor.builder_commands
            and "MANIFEST.json" in descriptor.allowed_paths
            and COMMAND_KEY not in descriptor.validator_commands):
        raise NativeExecutorBlocked("rules219_manifest_binding_not_admitted")
    descriptor.normalized()
    spec = GovernedCommand(
        key=COMMAND_KEY, role="builder",
        argv=("python", WRAPPER_PATH), risk=2,
        timeout_seconds=30, allow_dirty=True,
        allow_head_change=False, script_sha256=WRAPPER_SHA256,
    )
    spec.validate(descriptor)
    return spec
