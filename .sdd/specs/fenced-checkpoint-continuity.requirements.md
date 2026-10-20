# Fenced checkpoint continuity

## Objective

Allow one canonical task to resume on another eligible executor without letting
an expired executor mutate the Worker Pool or repeat an uncertain effect.

## Requirements

1. Fencing is opt-in per task; upgraded producers preserve the token-only flow
   for tasks created with `fencing_required=false`.
2. Legacy claim routes in the upgraded service never acquire an opt-in fenced
   task.
3. Fenced admission is disabled until a persisted exclusive-cutover attestation
   states that pre-fencing claimers were drained or isolated from the queue.
4. Every fenced claim increments a durable `lease_generation` atomically and
   creates an immutable `attempt_id`.
5. A database trigger rejects the pre-fencing claim SQL for fenced builder and
   validator work unless generation and attempt advance in the same update.
6. Worker-side renew, start, handoff, completion, failure, block and checkpoint
   operations reject a missing or stale generation/attempt for a fenced task.
7. A checkpoint is idempotent by task and checkpoint ID. Identical replay does
   not count as material progress; divergent replay fails closed.
8. A new checkpoint advances material progress and contains only bounded
   metadata, integrity digests and recoverable references.
9. `unknown` is terminal for worker-side checkpoints of that effect until the
   separately authenticated reconciliation records the outcome. Worker-side
   `confirmed` and `not_applied` checkpoints require an effect result reference.
10. The latest `pending` or any unreconciled `unknown` state for an effect blocks automatic
   rerouting, normal lease-releasing transitions and explicit requeue.
11. Post-lease reconciliation requires a technical credential distinct from
   worker credentials, the expected generation and an independent-readback
   evidence reference. It is idempotent per task, effect and generation.
12. A fenced claim returns the latest checkpoint and a ledger with the latest
   state and reconciliation result of every effect. The claimant must skip
   `confirmed` effects and may retry `not_applied` effects under its new fence.
13. A destination-level adapter must compare the generation at the effect
   boundary. Worker Pool validation alone is not proof of external fencing.

## Acceptance

- Executor A acquires generation 1 and records a checkpoint.
- After its lease expires, executor B acquires the same task at generation 2.
- Late mutations from A are rejected and B can complete the canonical task.
- A destination control rejects A after observing generation 2.
- An unknown effect blocks reassignment.
- Worker credentials cannot reconcile an effect; a separately authenticated
  reconciliation can release the task after independent readback.
- The same logical effect can be reconciled independently in later generations.
- A replacement executor sees distinct outcomes for multiple effects and does
  not infer one effect's result from another.
- The literal pre-fencing builder and validator claim SQL is rejected for a
  fenced task, including after store restart. Before the persisted cutover gate
  is enabled, fenced admission fails closed and legacy tasks remain claimable.
- Pre-fencing claimers are drained or isolated before enabling the gate: their
  old `SELECT` can otherwise cause head-of-line blocking on a mixed queue.
- Four separate processes prove A checkpoint, one deterministic lease-recovery
  cycle, B builder takeover, C readback through its own sink connection and
  rejection of A/g1 while B/g2 and C/g3 hold the task. The separate SQLite sink
  exercises serial CAS transitions and one logical effect, not CAS contention.
- The multiprocess test calls `WorkerPoolStore` directly. It does not claim
  HTTP/authentication, background-controller, physical-host, GitHub or other
  production-destination qualification.
- Legacy tasks still complete with the v1 token-only flow.
- Positive, negative, replay, concurrency and full regression tests pass on the
  same SHA.
