# Issue-bound RAG → Qwen Builder → independent Validator (increment 1)

## Purpose

This is an **opt-in, fail-closed adapter** that lets the *existing* scoped
`bridge.property.v1` Qwen Builder consume a verified source citation as
**untrusted supporting data**, without widening paths, commands, task
admission, model endpoint, or the acceptance contract.

The previous Agent Core/Worker Pool RAG read-only pilot and the Dell physical
E2E #40221994125 are real evidence for **that read-only PR-context query
only**. Neither pilot proves the source for a new issue-specific task. This
change adds a real contract and plumbing for that missing boundary; it does
not falsely reuse the PR #9 citation as issue #8 evidence.

## Trust and admission

- Operator first obtains `IssueBinding` through the current authenticated
  GitHub issue/comment/main-HEAD admission. Only explicit literal owner
  authorization for the **current** main SHA is accepted by that separate
  module. No issue text is accepted as a command.
- The optional `rag_receipt` is the canonical, exact
  `worker.rag.evidence-v1` structure from `agent_rag_consumer`, including
  question digest, selected citation, source hash/byte count, Git blob SHA,
  Qwen digest, and the receipt SHA-256.
- The RAG receipt must refer to the **same repository**, current
  `IssueBinding.base_sha`, and task correlation ID. The selected source
  must be `README.md`, `AGENTS.md`, or
  `docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md`, and the question must be the
  operator-defined `issue_rag_handoff.QUESTION`. Other PRs, other SHAs,
  old external RAG pilots and arbitrary files block before the model runs.
- A **trusted independent_read(path, sha) callback** must retrieve the
  original UTF-8 bytes from an authenticated GitHub GET at the exact SHA or
  a separately attested checkout. The receipt is only accepted if this
  independent read matches its byte count, SHA-256, Git blob SHA, excerpt,
  line and immutable URL. A self-reported hash is never enough.
- The Builder is allowed to append only the verified, length-bounded
  citation text to its Qwen messages. The messages explicitly identify
  the excerpt as **untrusted source content**, not an instruction,
  authorization, tool call, or replacement of the acceptance cases. The
  model has no new tool access. The Builder still performs 42/42 cases.
- The Validator must independently read and verify the same source at
  the original base SHA, before running its current 42/42 acceptance and
  independently executed 52+ regression gate. If a source drifts, block.
- The prompt is only enriched when `rag_receipt` and
  `rag_source_reader` are both explicitly provided and the native
  Qwen model is required. Default legacy/non-RAG paths are unchanged.

## Execution boundary

A trusted caller would do the following **only inside an authorized
Session Launcher / Command Gateway session**, with an actual issue admission
and already bounded source-reader implementation:

```text
admit owner-approved issue and HEAD through trusted GitHub API
research only allowlisted source at current issue base SHA with native Qwen
persist/authenticate RAG receipt and source bytes in durable task evidence
supply rag_receipt + independent GitHub reader to IssueQwenNativeAdapter
NativeOneShotExecutor Builder → Git patch → durable CAS → Validator
Validator independently fetches source and runs acceptance/regressions
publish one draft PR only when the global durable-PR gate is satisfied
```

**The integration above is not enabled automatically by this increment.**
No new GitHub issue authorizations, work claims, secrets, CAS writes, draft PRs,
merge, deploy or model downloads are performed by importing these modules.

## Honest limits and next gates

1. The trusted issue-specific receipt producer and protected durable handoff
   across **different physical hosts** still need to be connected. The
   `independent_read` protocol does not magically authenticate bytes:
   the operational caller must prove its independent identity and origin.
2. A real issue whose **baseline still fails at its current authorized
   main SHA** must be admitted. Old owner approvals on previous SHAs must
   never be carried forward silently.
3. A full physical run must verify current, not simulated: issue/TODO →
   Qwen model with RAG-derived context → real patch/commit →
   independent SHA-bound CAS readback → Validator on distinct checkout
   (42/42 + 52/52 for this profile) → exactly one draft PR →
   independent readback and replay with zero additional model calls.
4. A synthetic regression test passing does not certify a real Qwen model
   selecting a source for the issue. In particular the earlier read-only
   Dell experiment citing `agent-core` PR #9 is **not a relevant
   issue-bound source**.

Merge and production remain out of scope. Cost incremental limit remains
R$0.00. No Docker on physical hosts, no GUI/RDC/terminal fallback, and no
unrestricted LLM commands.
