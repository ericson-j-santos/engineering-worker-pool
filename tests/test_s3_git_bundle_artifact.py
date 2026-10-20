(target.root, "remote") == ""
    assert artifact.artifact_kind == ARTIFACT_KIND
    assert artifact.artifact_ref == ARTIFACT_REF_PREFIX + artifact.artifact_sha256
    assert len(client.objects) == 2
    assert client.head_calls == 0


def test_bundle_is_incremental_v2_with_exact_checkpoint_and_head(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    key = next(iter(client.objects))
    _header, bundle, decoded = transport._decode(
        client.objects[key], artifact.artifact_sha256, manifest,
    )

    transport._validate_bundle_header(
        bundle, manifest.workspace_sha, result.produced_sha,
    )
    boundary = bundle.index(b"\n\n")
    lines = bundle[:boundary].splitlines()
    assert lines[0] == b"# v2 git bundle"
    assert len(lines) == 3
    assert lines[1].startswith(b"-" + manifest.workspace_sha.encode("ascii") + b" ")
    assert lines[2] == result.produced_sha.encode("ascii") + b" HEAD"
    assert decoded == result


@pytest.mark.parametrize("mutation", ["version", "prerequisite", "reference"])
def test_bundle_header_rejects_version_prerequisite_or_ref_drift(tmp_path, mutation):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    key = next(iter(client.objects))
    _header, bundle, _decoded = transport._decode(
        client.objects[key], artifact.artifact_sha256, manifest,
    )
    if mutation == "version":
        bundle = bundle.replace(b"# v2 git bundle", b"# v3 git bundle", 1)
    elif mutation == "prerequisite":
        bundle = bundle.replace(
            b"-" + manifest.workspace_sha.encode("ascii"), b"-" + b"f" * 40, 1,
        )
    else:
        bundle = bundle.replace(
            result.produced_sha.encode("ascii") + b" HEAD",
            result.produced_sha.encode("ascii") + b" refs/heads/main", 1,
        )

    with pytest.raises(NativeExecutorBlocked, match="artifact_bundle_invalid"):
        transport._validate_bundle_header(
            bundle, manifest.workspace_sha, result.produced_sha,
        )


def test_duplicate_publish_is_idempotent_after_full_readback(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    first = publish(transport, source, manifest, result)
    second = publish(transport, source, manifest, result)

    assert second == first
    assert client.put_calls == 2
    assert len(client.objects) == 1


def test_concurrent_publishers_converge_on_one_cas_object(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(publish, transport, source, manifest, result)
            for _ in range(2)
        ]
    artifacts = [future.result() for future in futures]

    assert artifacts[0] == artifacts[1]
    assert client.put_calls == 2
    assert len(client.objects) == 1


def test_timeout_after_store_is_reconciled_by_full_readback(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    client.timeout_after_store = True

    artifact = publish(transport, source, manifest, result)

    assert artifact.artifact_ref.startswith(ARTIFACT_REF_PREFIX)
    assert len(client.objects) == 1


def test_timeout_before_store_remains_ambiguous_and_sanitized(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    client.timeout_before_store = True

    with pytest.raises(NativeExecutorBlocked) as caught:
        publish(transport, source, manifest, result)

    assert caught.value.args == ("artifact_publish_ambiguous",)
    assert "secret" not in str(caught.value)
    assert client.objects == {}


def test_existing_divergent_object_is_never_overwritten(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    key = next(iter(client.objects))
    client.objects[key] = b"divergent"

    with pytest.raises(NativeExecutorBlocked, match="artifact_cas_conflict"):
        publish(transport, source, manifest, result)

    assert client.objects[key] == b"divergent"
    assert artifact.artifact_sha256 in key


def test_tampered_object_and_reference_are_rejected(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    key = next(iter(client.objects))
    value = bytearray(client.objects[key])
    value[-1] ^= 1
    client.objects[key] = bytes(value)

    with pytest.raises(NativeExecutorBlocked, match="artifact_integrity_invalid"):
        transport.verify(
            task_id=TASK, manifest=manifest, workspace=source,
            produced_sha=result.produced_sha, artifact=artifact,
        )


def test_local_attributes_cannot_enable_checkout_filter(tmp_path):
    transport, _client, source, target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    location = Path(git(target.root, "rev-parse", "--git-path", "info/attributes"))
    if not location.is_absolute():
        location = target.root / location
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text("*.txt filter=attacker\n", encoding="utf-8")

    with pytest.raises(
        NativeExecutorBlocked, match="artifact_checkout_attributes_forbidden",
    ):
        transport.materialize(
            task_id=TASK, manifest=manifest, workspace=target, artifact=artifact,
        )
    assert target.head() == manifest.workspace_sha


def test_ignored_workspace_content_blocks_force_checkout(tmp_path):
    transport, _client, source, target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    location = Path(git(target.root, "rev-parse", "--git-path", "info/exclude"))
    if not location.is_absolute():
        location = target.root / location
    with location.open("a", encoding="utf-8") as stream:
        stream.write("\nignored.tmp\n")
    (target.root / "ignored.tmp").write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(
        NativeExecutorBlocked, match="artifact_workspace_ignored_files",
    ):
        transport.materialize(
            task_id=TASK, manifest=manifest, workspace=target, artifact=artifact,
        )
    assert (target.root / "ignored.tmp").read_text(encoding="utf-8") == "do not overwrite\n"

    invalid = DurableArtifact(
        ARTIFACT_KIND, "https://attacker.invalid/object", artifact.artifact_sha256,
    )
    with pytest.raises(NativeExecutorBlocked, match="durable_artifact_invalid"):
        transport.verify(
            task_id=TASK, manifest=manifest, workspace=source,
            produced_sha=result.produced_sha, artifact=invalid,
        )


def test_manifest_task_mismatch_is_rejected_before_store_access(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)

    with pytest.raises(NativeExecutorBlocked, match="artifact_task_mismatch"):
        transport.publish(
            task_id="cwp-" + "2" * 24, manifest=manifest, lease_generation=1,
            attempt_id="attempt-1", effect_id="effect-1",
            workspace=source, result=result,
        )

    assert client.put_calls == 0


def test_cross_task_descriptor_cannot_be_replayed(tmp_path):
    transport, _client, source, _target, manifest, result = fixture_values(tmp_path)
    artifact = publish(transport, source, manifest, result)
    other_task = "cwp-" + "2" * 24
    other = TaskManifest.parse({
        **{
            "schema_version": manifest.schema_version,
            "repository": manifest.repository,
            "base_sha": manifest.base_sha,
            "workspace_sha": manifest.workspace_sha,
            "adapter_id": manifest.adapter_id,
            "adapter_contract_sha256": manifest.adapter_contract_sha256,
            "before_sha256": dict(manifest.before_sha256),
        },
        "task_id": other_task,
    })

    with pytest.raises(NativeExecutorBlocked, match="artifact_manifest_invalid"):
        transport.verify(
            task_id=other_task, manifest=other, workspace=source,
            produced_sha=result.produced_sha, artifact=artifact,
        )


def test_result_must_descend_from_manifest_workspace_checkpoint(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(tmp_path)
    tree = git(source.root, "rev-parse", f"{manifest.base_sha}^{{tree}}")
    divergent = git(
        source.root, "commit-tree", tree, "-p", manifest.base_sha,
        "-m", "divergent workspace",
    )
    divergent_manifest = TaskManifest.parse({
        "schema_version": manifest.schema_version,
        "task_id": manifest.task_id,
        "repository": manifest.repository,
        "base_sha": manifest.base_sha,
        "workspace_sha": divergent,
        "adapter_id": manifest.adapter_id,
        "adapter_contract_sha256": manifest.adapter_contract_sha256,
        "before_sha256": dict(manifest.before_sha256),
    })

    with pytest.raises(NativeExecutorBlocked, match="artifact_base_not_ancestor"):
        publish(transport, source, divergent_manifest, result)

    assert client.put_calls == 0


def test_size_limit_fails_before_any_upload(tmp_path):
    transport, client, source, _target, manifest, result = fixture_values(
        tmp_path, maximum=1_024,
    )

    with pytest.raises(NativeExecutorBlocked, match="artifact_too_large"):
        publish(transport, source, manifest, result)

    assert client.put_calls == 0


def test_preflight_and_endpoint_validation_fail_closed(tmp_path):
    transport, client, _source, _target, _manifest, _result = fixture_values(tmp_path)

    transport.preflight()
    assert client.put_calls == 1
    assert len(client.objects) == 1
    transport.preflight()
    assert client.put_calls == 2
    assert len(client.objects) == 1

    client.deny_put = True
    with pytest.raises(NativeExecutorBlocked) as caught:
        transport.preflight()
    assert caught.value.args == ("artifact_store_unavailable",)
    assert "secret" not in str(caught.value)

    with pytest.raises(NativeExecutorBlocked, match="artifact_store_config_invalid"):
        S3ArtifactConfig(
            bucket="durable-artifacts", region="us-east-2",
            endpoint_url="http://user:secret@attacker.invalid/?query=1",
        )


def test_boto_client_ignores_unvalidated_generic_endpoint(monkeypatch):
    import boto3

    captured = {}
    client = FakeS3()

    def fake_client(service, **kwargs):
        captured.update(kwargs)
        assert service == "s3"
        return client

    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://attacker.invalid")
    monkeypatch.setattr(boto3, "client", fake_client)
    transport = S3GitBundleArtifactTransport.from_boto3(S3ArtifactConfig(
        bucket="durable-artifacts", region="us-east-2",
    ))

    assert transport.client is client
    assert captured["endpoint_url"] is None
    assert captured["verify"] is True
    assert captured["config"].ignore_configured_endpoint_urls is True


@pytest.mark.parametrize("failure", ["deny_get", "timeout_before_store",
                                      "timeout_after_store"])
def test_preflight_requires_conclusive_conditional_write_and_readback(
    tmp_path, failure,
):
    transport, client, _source, _target, _manifest, _result = fixture_values(tmp_path)
    setattr(client, failure, True)

    with pytest.raises(
        NativeExecutorBlocked, match="artifact_store_unavailable"
    ):
        transport.preflight()

    # Even a timeout raised after the probe was stored is not mistaken for
    # proof that this credential received an authorized PUT response.
    if failure == "timeout_after_store":
        assert len(client.objects) == 1
