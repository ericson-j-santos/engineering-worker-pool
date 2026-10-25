# SQLite reader connection lifecycle

## Scope

The native Windows issue-8 Qwen pipeline failed during cleanup with a leftover
SQLite database. A separate governed read-only inspection identified one SQLite
file, but could not prove which process held the historic Windows handle.

Python's sqlite3.Connection context manager commits or rolls back transactions
but does not close the connection. The shared WorkerPoolStore previously used
that bare context for nine read accessors, leaving handles to garbage collection.

## Change and prevention

- _read_db() retains the SQLite context's transaction behavior and always closes
  the native connection in finally, including exception and cancellation paths.
- Read accessors use _read_db(). Writer _tx() and initialization preserve their
  existing explicit transaction and close behavior.
- _db() closes newly created connections if PRAGMA setup throws an exception.
- tests/test_store_connection_cleanup.py includes the pre-change negative
  control, actual SQLite close and unlink, rollback, replay, read API and
  independent concurrent-reader cases.
- The existing Native Qwen7B Windows Contract workflow runs these same tests
  on native GitHub-hosted Windows. No Docker or Ollama is used by that job.

## Boundaries

This repair does not migrate a shared Worker Pool authority, authenticate a
physical Dell builder, attest the historic exact holder of the SQLite lock,
persist a Qwen-generated artifact or produce an agent-authored PR draft.

No legacy CI gate is disabled. No merge, production deployment, new service,
GitHub write credential on the model process, Docker on the Dell or cloud
inference is authorized by this change.
