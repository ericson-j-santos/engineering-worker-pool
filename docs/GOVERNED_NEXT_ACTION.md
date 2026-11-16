# Governed `next_action` selection

`app.continuation_selector.select_continuation` is an inert, fail-closed policy
boundary. It accepts authenticated canonical task/checkpoint readbacks plus a
trusted control-plane binding and returns only one of these stage identifiers:

- `submit_validation` for a fenced `builder_result` with confirmed durable
  artifact readback;
- `complete` for an independently leased `validator_result` with a material
  `produced_sha`.

The binding keeps the result tied to the task, GitHub issue, TODO Global item,
idempotency key, repository/base SHA, current rules SHA, and correlation ID.
Lease generation and attempt must match the checkpoint, so a stale or concurrent
attempt cannot select a stage. The active lease must belong to the registered
Builder or independent Validator for that transition. Pending or unknown effects,
terminal/replayed task states, identity drift, missing evidence, and every
free-text action fail closed.

The selector does not authenticate HTTP, dispatch GitHub Actions, claim work,
invoke a model, mutate a task, write GitHub/Notion, or touch a physical host.
Those integrations remain separate gates. Unit tests prove the bounded positive
transitions and negative identity, effect, replay, concurrency, and command-text
cases; they are not physical or product E2E evidence.
