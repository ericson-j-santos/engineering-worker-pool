"""Rules #213: admit the existing fenced executor and Git-bundle CAS, never a new queue.

This is a strict composition boundary, not a deployment, a GitHub token client,
proof of live authority, model inference or a READY_FOR_PR decision. Providers
must be trusted native/HTTPS/Gateway integrations; task/issue text cannot supply
their readers or artifact configuration.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.native_one_shot import (
    AdapterResult, DurableArtifact, ExecutionBoundary, LocalWorkspace,
    NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore, TaskManifest,
    WorkerApi,
)
from app.rules213_native_adapter import (
    NATIVE_DESCRIPTOR, NATIVE_PROFILE, Rules213NativeAdapter,
)
from app.rules213_updater_profile import BASE_SHA, ISSUE, REPOSITORY, SOURCE_FUNCTIONS
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport

_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")


def _need(ok: bool, reason: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(reason)


@dataclass(frozen=True)
class Rules213RuntimeAuthority:
    """Trusted readback callbacks must be supplied by deployment, never a task.

    Shape checks are not cryptographic attestations of the readers. Deployment
    must separately verify authenticated GitHub, Gateway, DB authority, CAS
    provider, cost and exclusive host/lease before using this composition.
    """

    main_sha_reader: Callable[[], str]
    issue_reader: Callable[[], Mapping[str, Any]]
    worker_reader: Callable[[], Mapping[str, Any]]
    cost_reader: Callable[[], Mapping[str, Any]]
    expected_worker_sha: str

    def check(self) -> None:
        _need(type(self.expected_worker_sha) is str
              and _SHA40.fullmatch(self.expected_worker_sha) is not None,
              "rules213_worker_source_unpinned")
        _need(self.main_sha_reader() == BASE_SHA,
              "rules213_main_drift")
        issue = self.issue_reader()
        _need(type(issue) is dict
              and issue.get("repository") == REPOSITORY
              and type(issue.get("number")) is int and issue["number"] == ISSUE
              and issue.get("state") == "open",
              "rules213_issue_unverified")
        worker = self.worker_reader()
        _need(type(worker) is dict
              and worker.get("source_sha") == self.expected_worker_sha
              and worker.get("fenced_cutover_enabled") is True
              and worker.get("legacy_claimers_drained") is True
              and worker.get("gateway_verified") is True
              and worker.get("state_validated") is True
              and worker.get("session_launch_ok") is True,
              "rules213_worker_authority_unverified")
        identity = worker.get("authority_id")
        try:
            _need(type(identity) is str
                  and str(uuid.UUID(identity)) == identity,
                  "rules213_worker_identity_unverified")
        except (ValueError, AttributeError):
            raise NativeExecutorBlocked("rules213_worker_identity_unverified") from None
        cost = self.cost_reader()
        _need(type(cost) is dict
              and type(cost.get("incremental_brl")) is int
              and cost["incremental_brl"] == 0
              and cost.get("provider_verified") is True
              and cost.get("hard_stop_verified") is True,
              "rules213_cost_unverified")


class Rules213ScopedCAS:
    """Only Rules213 can use the already configured durable Git-bundle transport.

    Does not create a bucket, branch, token, object store or fallback provider.
    Each call rechecks live source/authority/cost before delegating to the
    existing CAS's conditional writes and independent readback.
    """

    def __init__(self, transport: S3GitBundleArtifactTransport,
                 authority: Rules213RuntimeAuthority):
        _need(type(transport) is S3GitBundleArtifactTransport
              and type(authority) is Rules213RuntimeAuthority,
              "rules213_cas_configuration_invalid")
        _need(transport.config.key_prefix == "rules213-cas-v1",
              "rules213_foreign_cas_profile")
        self.transport = transport
        self.authority = authority

    def _manifest(self, manifest: TaskManifest, task_id: str, *,
                  produced_sha: str | None = None, builder: bool = False) -> None:
        self.authority.check()
        _need(type(manifest) is TaskManifest
              and manifest.task_id == task_id
              and manifest.repository == REPOSITORY
              and manifest.base_sha == BASE_SHA
              and manifest.adapter_id == NATIVE_PROFILE
              and manifest.adapter_contract_sha256 == NATIVE_DESCRIPTOR.contract_sha256
              and set(manifest.before_sha256) == set(SOURCE_FUNCTIONS)
              and all(type(digest) is str and _SHA64.fullmatch(digest)
                      for digest in manifest.before_sha256.values()),
              "rules213_cas_manifest_untrusted")
        if builder:
            _need(manifest.workspace_sha == BASE_SHA,
                  "rules213_cas_builder_source_drift")
        else:
            _need(type(produced_sha) is str
                  and _SHA40.fullmatch(produced_sha) is not None
                  and produced_sha != BASE_SHA
                  and manifest.workspace_sha in {BASE_SHA, produced_sha},
                  "rules213_cas_candidate_drift")

    def preflight(self) -> None:
        self.authority.check()
        self.transport.preflight()

    def publish(self, *, task_id: str, manifest: TaskManifest,
                lease_generation: int, attempt_id: str, effect_id: str,
                workspace: LocalWorkspace, result: AdapterResult) -> DurableArtifact:
        self._manifest(manifest, task_id, builder=True)
        _need(type(result) is AdapterResult
              and type(result.produced_sha) is str
              and _SHA40.fullmatch(result.produced_sha) is not None
              and result.produced_sha != BASE_SHA
              and bool(result.changed_paths)
              and set(result.changed_paths).issubset(SOURCE_FUNCTIONS)
              and len(set(result.changed_paths)) == len(result.changed_paths),
              "rules213_cas_patch_scope_invalid")
        return self.transport.publish(
            task_id=task_id, manifest=manifest, lease_generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id, workspace=workspace,
            result=result,
        )

    def verify(self, *, task_id: str, manifest: TaskManifest,
               workspace: LocalWorkspace, produced_sha: str,
               artifact: DurableArtifact) -> None:
        self._manifest(manifest, task_id, produced_sha=produced_sha)
        self.transport.verify(
            task_id=task_id, manifest=manifest, workspace=workspace,
            produced_sha=produced_sha, artifact=artifact,
        )

    def materialize(self, *, task_id: str, manifest: TaskManifest,
                    workspace: LocalWorkspace, artifact: DurableArtifact) -> AdapterResult:
        self._manifest(manifest, task_id, builder=True)
        result = self.transport.materialize(
            task_id=task_id, manifest=manifest, workspace=workspace,
            artifact=artifact,
        )
        _need(type(result) is AdapterResult
              and result.produced_sha != BASE_SHA
              and bool(result.changed_paths)
              and set(result.changed_paths).issubset(SOURCE_FUNCTIONS),
              "rules213_cas_recovered_scope_invalid")
        return result


def compose_rules213_executor(
    *, api: WorkerApi, workspace: LocalWorkspace, receipts: ReceiptStore,
    adapter: Rules213NativeAdapter, commands: Mapping[str, Callable[[], Any]],
    boundary: ExecutionBoundary, cas: Rules213ScopedCAS,
) -> NativeOneShotExecutor:
    """Bind exactly the existing fenced task pipeline; never claim at composition."""
    _need(type(adapter) is Rules213NativeAdapter
          and type(cas) is Rules213ScopedCAS
          and type(boundary) is ExecutionBoundary
          and type(commands) is dict
          and set(commands) == set(NATIVE_DESCRIPTOR.command_keys)
          and all(callable(fn) for fn in commands.values()),
          "rules213_binding_invalid")
    _need(boundary.expected_head == BASE_SHA
          and boundary.state_validated is True
          and boundary.gateway_verified is True,
          "rules213_boundary_unverified")
    boundary.validate()
    cas.authority.check()
    return NativeOneShotExecutor(
        api=api, workspace=workspace, receipts=receipts,
        adapters={NATIVE_PROFILE: adapter},
        command_registries={NATIVE_PROFILE: commands},
        boundary=boundary, artifact_transport=cas,
        fenced_adapter_ids=frozenset({NATIVE_PROFILE}),
    )
