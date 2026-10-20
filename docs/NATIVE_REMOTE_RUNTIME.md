# Native HTTPS composition and executor-process continuity

## Implemented scope

`app/native_remote_runtime.py` adds an **opt-in** composition without changing
`LocalWorkerApi`, the Windows DPAPI bootstrap, the server bind, the queue schema,
or existing task manifests. It does not provision or activate infrastructure.

- `HttpsWorkerRoute`: one exact HTTPS origin and explicit certificate authority.
- `HttpsWorkerApi`: the existing exact task lifecycle over a separately owned TLS
  client; no redirects, proxy inheritance, automatic retries or administrative
  API routes. Response bytes and JSON structure are bounded and validated.
- `PosixWorkerTokenFile`: delegates to the existing POSIX ownership, mode,
  hardlink and `O_NOFOLLOW` checks. Windows is NOT silently accepted.
- `RemoteNativeExecutor`: verifies the real pinned Gateway session before
  constructing the HTTPS client, and injects the existing S3 transport into
  `NativeOneShotExecutor`. It does not accept self-declared boundary booleans.

All authority, CA, token-provider and S3-client inputs belong to trusted deployment
code, never to task manifests, issue text or remote requests. Unknown manifest
fields are rejected. The S3 client is injected explicitly and retains its own
identity and lifetime: callers do not copy a Windows DPAPI file between hosts,
export credentials globally or mix the API token with the artifact credential.
A platform-specific protected token provider may be installed separately. A
callable alone is not evidence that its ACL or deployment identity is approved.

## Safety and compatibility

The local loopback client remains unchanged. The new class inherits its task
shape, exact claims, fencing fields, checkpoint ambiguity reconciliation and
independent readbacks; it replaces only the transport and token-provider boundary.

The TLS context always validates certificate and hostname, uses TLS 1.2 or newer,
ignores ambient SSL certificate overrides, and never enables SSL key logging.
An explicitly configured private CA is supported; `verify=False`, caller-supplied
HTTP clients, URL credentials, paths, queries and fragments are not supported.
Origins use canonical lowercase ASCII hostnames or canonical IP literals.
Specify standard HTTPS without an explicit `:443`; nonstandard ports are allowed.
No untrusted input may choose this route. DNS/CA and server authorization remain
deployment responsibilities; TLS does not attest storage durability by itself.

A POST transport error remains `worker_transport_unverified`. It is never
automatically repeated. The existing checkpoint client performs a GET after an
ambiguous checkpoint POST; uncertainty is not converted into success.

Construction does not acquire work or write S3 objects. A one-shot run first
validates the manifest and independently attests Gateway/session/SHA, then uses
the original executor. For fenced tasks the original preflight still verifies
S3 conditional writing and full readback before claim. Installed command
registries must remain Gateway-backed; this composition does not invent product
adapters, authorize arbitrary commands or select queued work.

## Validation and exact limits

`tests/test_native_remote_runtime.py` covers origin and route rejection,
certificate configuration, proxy/key-log isolation, credential errors, redirects,
uncertain POST without retry, duplicate/nonfinite JSON, size bounds, no cookie
reuse, composition and rejection before HTTP/S3 when Gateway is unverified.
These are unit/fixture tests, not evidence of a private S3 provider.

`tests/test_native_remote_tls.py` runs the actual Worker Pool under Uvicorn/TLS in
a separate disposable authority process with SQLite in its own directory.
Worker A reaches it by HTTPS, acquires a fenced lease, saves and reads back a
checkpoint, then is killed by the test parent. The authority stays running and
the existing reconciler recovers the expired lease. B resumes the same task with
generation 2, rejects A's old generation, and the validator completes generation
3. Independent HTTPS and read-only SQLite reads assert one task and final state.
The worker never exports the database or a snapshot. Bad trust roots, hostname
and bearer authentication must leave the empty control database unchanged.

This is a real TLS/API/process-loss test in one disposable CI machine. It is NOT
loss of a VM, automatic selection of physical hosts, deployment-level Gateway
attestation of the protocol fixture, a real product modification, nor a private
S3 handoff. Synthetic worker registration flags in this protocol test do not
certify any host. The separate composition unit test checks that real Gateway
attestation is mandatory and cannot be replaced by those flags.

No private data is used; generated certificate keys and tokens are disposable
test fixtures, not operational credentials. OpenSSL is a required test
precondition and its absence fails rather than silently skipping the proof.
The test owns and terminates only its own child processes. No Docker is added.

## Remaining operational gate

Resolve existing protected references for one persistent authority and an
authorized private S3 route/identity independently accessible by each eligible
executor. Verify deployment/version, capabilities, Gateway-backed adapters,
server-side authorization and storage readback. Nothing in this code establishes
those references or creates a bucket, credential, role, database or service.

After that gate, exercise the same checkpoint protocol while losing an entire
executor, with the authority and private artifact storage outside that executor.
Continue the same canonical task, reject the stale writer at the actual
destination, independently verify the product effect and bound idempotent replay.
Do not replace the live queue with a snapshot or use public logs as artifact
transport. Keep TODO-30/issue #13 and the existing PR; do not create another queue
or duplicate the already consolidated Store/HTTP rehearsal.

Sources for TLS behavior: Python 3.12 `ssl` documentation and HTTPX SSL
documentation. These describe the transport guarantees, not deployment evidence.
