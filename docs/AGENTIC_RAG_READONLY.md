# Agent Core RAG → Worker Pool → Qwen7B (DEV, read-only)

## Scope

This opt-in adapter consumes the reviewed Agent Core capability
`github.pr.context.v1` from
`agent-core@1fc032d4c412bdb4ed202cf1193caf0cb49490ce`, pinned in
`requirements-rag.txt`. The original Agent Core PR #9 is merged; the
module is read-only and retrieves small, SHA-bound Markdown excerpts from the
official GitHub API. It is **not** a general code-editing agent.

The Worker Pool orchestrator, leases, Builder/Validator, task queue, CAS and
publish mechanisms remain unchanged. Neither the model nor retrieved content
can provide a queue manifest, commands, URLs, repository, model, credentials,
branch, publication permission or worker identity.

## End-to-end read-only research

1. Trusted operator supplies repository, PR, immutable expected head SHA,
   correlation ID, question and approved Markdown paths. These are
   configuration, **not** user/model/issue controlled tool arguments.
2. GitHub PR head/base and repository identity are independently read
   **before** any costly inference; drift fails closed.
3. Native Qwen2.5-Coder 7B plans a query and selects exactly one path from
   the operator allowlist. Bounded JSON schema and deterministic validation
   reject off-allowlist paths, arbitrary fields, unauthorized actions and
   unrelated queries.
4. The original Agent Core `Registry` + `GitHubPRContext` reads that file
   at the pinned PR head; validates Git blob SHA and SHA-256, bounded excerpts,
   line links, check-run observations and PR readback.
5. The Qwen model reads excerpts **as untrusted data** and chooses a valid
   citation index or reports insufficient evidence. It cannot execute text
   from a source; invalid JSON, forged citations and missing evidence block.
6. At most two distinct allowlisted source queries are attempted. A second
   retrieval occurs only after evidence is insufficient and with a different
   path. No repeated polling or blind retries.
7. The consumer re-reads the PR head/base after model inference and produces
   a canonical SHA-256 receipt, source digests, model digest, inference count
   and line reference. There are no writes or worker-dispatch effects.

The real transport reuses `NativeStructuredOllama.no_cloud` and
`NativeStructuredOllama.select_model`; HTTP is restricted to
`127.0.0.1:11434`, `trust_env=False`, no redirects. Requires
`NATIVE_OLLAMA_DEV=1` and a verified
`NATIVE_MODEL_DIGEST_PREFIX` with at least 12 hex chars.
GitHub access uses the reviewed Agent Core HTTPS GET adapter. No GPU, model,
bucket, daemon or credential is installed automatically.

## Validation and trust boundary

The PR CI tests all existing contracts plus source allowlists, drift before
and after inference, Git blob corruption, model action injection, no answer
without evidence, bounded re-retrieval, idempotent reads, and a local-only
HTTP transport contract. A dedicated PR-only step reuses the **existing**
temporary hosted Qwen7B container after structured repair, reads
`agent-core` PR #9 directly from real GitHub, tests stale SHA rejection
without model calls, performs actual Qwen inference and then reads the PR
and original Markdown bytes again via an independent GitHub client.

This hosted test exercises real retrieval and a real local-only model on a
disposable GitHub-hosted runner. It does **not** prove deployment on Dell,
Noteri, persistent agents, real TODO/Issue admission, a code repair, CAS
handoff, two-host failover, a branch/PR created by the local model, or
resistance to all prompt-injection attacks. Local Ollama client flags alone
also do not prove global egress restrictions on the Ollama daemon.

No Docker dependency is introduced on the Dell: the CI job reuses an
existing temporary hosted test environment; physical runtime uses native
Ollama in governed sessions. Do not run this module from a normal shell on
physical machines: Session Launcher, Command Gateway, reserved worktree,
attested model and observed host readiness remain mandatory.

## Acceptance / next operational gate

- Software: full tests and live GitHub + model E2E on the exact PR HEAD,
  required CI actually executed, independent source readback, negative cases.
- Physical: Dell (or another authorized host) picks up a SHA-bound governed
  job; `SESSION_LAUNCH_OK` + Command Gateway; locally installed Qwen digest
  attested; real GitHub retrieval and model inference; independent readback
  of same SHA; no unexpected model/cloud/command side effects.
- Autonomy: a separately authorized Issue/TODO through Worker Pool
  with CAS, Validator, real patch, draft PR, and replay. This read-only module
  does not satisfy that separate gate by itself.

If any gate is missing or `skipped`, status remains **partial**.
No merge/deploy/production or billing change is authorized by this module.
