# DEV authority without external providers

## Explicit opt-in (not a deployed service)

The first issue-to-Qwen draft PR must use existing native Worker Pool lifecycle
and GitHub Git CAS, with no external database, object store, identity broker,
Docker on a personal host, or inference API.

- The **authority of record** is one native PostgreSQL instance bound to
  127.0.0.1 on an authorized POSIX host (Linux or WSL with native systemd).
  Its HTTPS Worker Pool API is the *only* route for separately authorized
  Builder/Validator hosts. It must have an independently verified TLS origin,
  token identity, fenced lease, checkpoint and recovery service.
- GitHubObjectCAS / IssueBoundGitBundleTransport persist and read back bounded
  issue-scoped public **source-code** Git bundles with fixed SHA-256, using
  trusted GitHub API and a write-capable process separate from Qwen.
- There is **no automatic database failover**. Another machine may continue
  only after the old authority is proven fenced/offline, a durable backup is
  independently restored, and the new identity/lease generation is verified.
  Two disconnected local SQLite/PostgreSQL databases are NOT shared authority.
- The first real physical test still requires an open issue, current owner
  comment bound to current Worker Pool main SHA, a failing baseline, and all
  normal Session Launcher/Command Gateway/cost/E2E gates.

The runtime exposes an optional and explicit
CODEX_WORKER_POOL_AUTHORITY_PROFILE=native-free-dev-v1 configuration check.
When set, it requires:

- CODEX_WORKER_POOL_DEPLOYMENT_ENV=dev
- CODEX_WORKER_POOL_BACKEND=postgres
- CODEX_WORKER_POOL_ARTIFACT_TRANSPORT=github-git-cas-v1
- CODEX_WORKER_POOL_DB_SCHEMA=ewp_dev_<unique_suffix>
- CODEX_WORKER_POOL_AUTHORITY_ID=<prequalified UUID>
- CODEX_WORKER_POOL_DATABASE_URL_FILE=<private owner-only file>
- CODEX_WORKER_POOL_API_TOKEN_FILE=<private owner-only file>
- CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE=<separate owner-only file>
- CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED=0

The credential file uses an exact local PostgreSQL URI (127.0.0.1), explicit
port, sslmode=verify-full, sslrootcert=system or an installed CA file,
channel_binding=require, and 1–30 second connection timeout. The guard
rejects links, nonprivate or linked token/DB files, nonlocal database hosts,
weaker TLS settings, external broker/S3 configuration and unapproved
automatic fenced-cutover. It reads local configuration but neither
connects to PostgreSQL nor contacts GitHub or any provider.

**IMPORTANT:** the declared artifact transport is configuration intent, not
proof that a live Builder has published Git bundle bytes. Even a successful
startup check does not prove database TLS handshake, shared authority, remote
HTTPS reachability, CAS readback, independent Validator, 42/42, 52/52,
READY_FOR_PR or a draft PR created by the model.

## Next physical qualification

1. Inventory native PostgreSQL, systemd, owner-only credentials, dedicated
   DEV database/schema, TLS CA and authenticated Worker Pool API through a
   validated Session Launcher and Command Gateway on an available host.
2. Use read-only schema/authority checks and real separate-host readback.
   Do not initialize/migrate or enable fenced cutover until a reversible
   plan and exact DEV permissions are confirmed.
3. Prove a real task lease, Git CAS persistence, independent Validator,
   single draft PR and replay without duplicate inference or publication.

Cost increment is capped at R$0.00. No external provider migration is
implied, and the existing Worker Pool implementation remains available.
