ny]:
        _require(type(lease_generation) is int
                 and not isinstance(lease_generation, bool)
                 and lease_generation >= 1
                 and type(attempt_id) is str
                 and bool(SAFE_ID.fullmatch(attempt_id))
                 and type(effect_id) is str and bool(SAFE_ID.fullmatch(effect_id)),
                 "artifact_fence_invalid")
        return {
            "schema_version": 1,
            "artifact_kind": ARTIFACT_KIND,
            "task_manifest": _task_manifest_value(manifest),
            "lease_generation": lease_generation,
            "attempt_id": attempt_id,
            "effect_id": effect_id,
            "result": cls._result_value(result),
            "bundle": {
                "sha256": hashlib.sha256(bundle).hexdigest(),
                "size": len(bundle),
            },
        }

    @staticmethod
    def _encode(header: Mapping[str, Any], bundle: bytes, maximum: int) -> bytes:
        header_bytes = _canonical(header)
        _require(0 < len(header_bytes) <= HEADER_LIMIT,
                 "artifact_manifest_invalid")
        value = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + bundle
        _require(len(value) <= maximum, "artifact_too_large")
        return value

    @classmethod
    def _decode(
        cls, value: bytes, digest: str, manifest: TaskManifest,
        produced_sha: str | None = None,
    ) -> tuple[dict[str, Any], bytes, AdapterResult]:
        _require(type(value) is bytes and hashlib.sha256(value).hexdigest() == digest
                 and value.startswith(MAGIC)
                 and len(value) >= len(MAGIC) + 4,
                 "artifact_integrity_invalid")
        offset = len(MAGIC)
        header_length = struct.unpack(">I", value[offset:offset + 4])[0]
        start = offset + 4
        end = start + header_length
        _require(0 < header_length <= HEADER_LIMIT and end < len(value),
                 "artifact_integrity_invalid")
        header_bytes, bundle = value[start:end], value[end:]
        try:
            header = json.loads(
                header_bytes.decode("ascii"),
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            _blocked("artifact_manifest_invalid")
        _require(type(header) is dict and _canonical(header) == header_bytes
                 and set(header) == {
                     "schema_version", "artifact_kind", "task_manifest",
                     "lease_generation", "attempt_id", "effect_id", "result",
                     "bundle",
                 }
                 and header["schema_version"] == 1
                 and header["artifact_kind"] == ARTIFACT_KIND
                 and header["task_manifest"] == _task_manifest_value(manifest)
                 and type(header["lease_generation"]) is int
                 and not isinstance(header["lease_generation"], bool)
                 and header["lease_generation"] >= 1
                 and type(header["attempt_id"]) is str
                 and bool(SAFE_ID.fullmatch(header["attempt_id"]))
                 and type(header["effect_id"]) is str
                 and bool(SAFE_ID.fullmatch(header["effect_id"])),
                 "artifact_manifest_invalid")
        bundle_header = header["bundle"]
        _require(type(bundle_header) is dict
                 and set(bundle_header) == {"sha256", "size"}
                 and type(bundle_header["size"]) is int
                 and not isinstance(bundle_header["size"], bool)
                 and bundle_header["size"] == len(bundle) and len(bundle) > 0
                 and type(bundle_header["sha256"]) is str
                 and bool(SHA256.fullmatch(bundle_header["sha256"]))
                 and hashlib.sha256(bundle).hexdigest() == bundle_header["sha256"],
                 "artifact_integrity_invalid")
        raw_result = header["result"]
        _require(type(raw_result) is dict and set(raw_result) == {
            "produced_sha", "changed_paths", "invoked_commands", "evidence_sha256",
        }, "artifact_manifest_invalid")
        result = AdapterResult(
            raw_result["produced_sha"], tuple(raw_result["changed_paths"])
            if type(raw_result["changed_paths"]) is list else (),
            tuple(raw_result["invoked_commands"])
            if type(raw_result["invoked_commands"]) is list else (),
            raw_result["evidence_sha256"],
        )
        cls._result_value(result)
        _require(produced_sha is None or result.produced_sha == produced_sha,
                 "artifact_result_invalid")
        return header, bundle, result

    @staticmethod
    def _write_bundle(directory: Path, bundle: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix="received-", suffix=".gitbundle", dir=directory,
        )
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o600)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    @classmethod
    def _reject_external_filters(
        cls, workspace: LocalWorkspace, hooks: Path, produced_sha: str,
    ) -> None:
        result = cls._git(
            workspace, hooks, "grep", "-n", "-I", "-E",
            r"(^|[[:space:]])filter([[:space:]]|=|$)", produced_sha, "--",
            ".gitattributes", ":(glob)**/.gitattributes",
            allowed_codes=(0, 1),
        )
        _require(result.returncode == 1, "artifact_checkout_filter_forbidden")
        local_attributes = cls._git(
            workspace, hooks, "rev-parse", "--git-path", "info/attributes",
        ).stdout.strip()
        _require(bool(local_attributes), "artifact_workspace_invalid")
        path = Path(local_attributes)
        if not path.is_absolute():
            path = workspace.root / path
        if path.exists() or path.is_symlink():
            _require(path.is_file() and not path.is_symlink()
                     and path.stat().st_size == 0,
                     "artifact_checkout_attributes_forbidden")

    @classmethod
    def _require_checkout_pristine(
        cls, workspace: LocalWorkspace, hooks: Path,
    ) -> None:
        status = cls._git(
            workspace, hooks, "status", "--porcelain=v1",
            "--untracked-files=all", "--ignored=matching",
        ).stdout
        _require(status.strip() == "", "artifact_workspace_ignored_files")

    def publish(
        self, *, task_id: str, lease_generation: int, attempt_id: str,
        effect_id: str, manifest: TaskManifest, workspace: LocalWorkspace,
        result: AdapterResult,
    ) -> DurableArtifact:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        bundle = self._bundle_bytes(
            workspace, manifest, result,
            self.config.max_artifact_bytes - len(MAGIC) - 4 - HEADER_LIMIT,
        )
        header = self._header(
            manifest=manifest, lease_generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id, result=result,
            bundle=bundle,
        )
        value = self._encode(header, bundle, self.config.max_artifact_bytes)
        digest = hashlib.sha256(value).hexdigest()
        self._put_cas(self._key(digest), value, digest)
        return DurableArtifact(
            ARTIFACT_KIND, ARTIFACT_REF_PREFIX + digest, digest,
        )

    def verify(
        self, *, task_id: str, manifest: TaskManifest,
        workspace: LocalWorkspace, produced_sha: str,
        artifact: DurableArtifact,
    ) -> None:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        digest = self._descriptor_digest(artifact)
        value = self._download(self._key(digest))
        _header, bundle, result = self._decode(
            value, digest, manifest, produced_sha,
        )
        self._validate_bundle_header(
            bundle, manifest.workspace_sha, produced_sha,
        )
        _require(workspace.status() == "" and workspace.head() == produced_sha,
                 "artifact_workspace_invalid")
        with tempfile.TemporaryDirectory(prefix="cwp-verify-") as name:
            directory = Path(name)
            hooks = directory / "hooks"
            hooks.mkdir(mode=0o700)
            path = self._write_bundle(directory, bundle)
            self._require_repository_root(workspace, hooks)
            self._verify_bundle(workspace, path, produced_sha, hooks)
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            for ancestor_sha in (manifest.base_sha, manifest.workspace_sha):
                ancestor = self._git(
                    workspace, hooks, "merge-base", "--is-ancestor",
                    ancestor_sha, produced_sha, allowed_codes=(0, 1),
                )
                _require(ancestor.returncode == 0, "artifact_base_not_ancestor")
        _require(workspace.changed_paths(manifest.base_sha, produced_sha)
                 == result.changed_paths, "artifact_result_paths")

    def materialize(
        self, *, task_id: str, manifest: TaskManifest,
        workspace: LocalWorkspace, artifact: DurableArtifact,
    ) -> AdapterResult:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        digest = self._descriptor_digest(artifact)
        value = self._download(self._key(digest))
        _header, bundle, result = self._decode(value, digest, manifest)
        self._validate_bundle_header(
            bundle, manifest.workspace_sha, result.produced_sha,
        )
        _require(workspace.root.is_dir() and not workspace.root.is_symlink()
                 and workspace.status() == ""
                 and workspace.head() == manifest.workspace_sha,
                 "artifact_workspace_invalid")
        with tempfile.TemporaryDirectory(prefix="cwp-materialize-") as name:
            directory = Path(name)
            hooks = directory / "hooks"
            hooks.mkdir(mode=0o700)
            self._require_repository_root(workspace, hooks)
            self._require_checkout_pristine(workspace, hooks)
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            path = self._write_bundle(directory, bundle)
            self._verify_bundle(workspace, path, result.produced_sha, hooks)
            self._git(
                workspace, hooks, "fetch", "--no-tags", "--no-write-fetch-head",
                str(path), "HEAD",
            )
            for ancestor_sha in (manifest.base_sha, manifest.workspace_sha):
                ancestor = self._git(
                    workspace, hooks, "merge-base", "--is-ancestor",
                    ancestor_sha, result.produced_sha, allowed_codes=(0, 1),
                )
                _require(ancestor.returncode == 0, "artifact_base_not_ancestor")
            self._reject_external_filters(workspace, hooks, result.produced_sha)
            self._git(
                workspace, hooks, "checkout", "--detach", "--force",
                result.produced_sha,
            )
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            self._require_checkout_pristine(workspace, hooks)
        _require(workspace.status() == "" and workspace.head() == result.produced_sha
                 and workspace.changed_paths(manifest.base_sha, result.produced_sha)
                 == result.changed_paths, "artifact_materialization_invalid")
        return result
