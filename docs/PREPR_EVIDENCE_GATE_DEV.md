# Pre-PR independent evidence — DEV stage only

This is a reusable read-only admission guard for the existing Worker Pool.
It does not create a queue, model session, artifact storage, PR, merge or deploy.
The result always has ready_for_pr=false; it **never** authorizes work.

The calling control plane must provide real, trusted readers for:
GitHub issue/branch and SHA ancestry; Qwen/Ollama model process/digest and
baseline; immutable Git bundle CAS and full-byte replay; Git file contents at
base and produced commits; a distinct Validator that materialized the CAS in an
independent checkout; and verified incremental cost zero. These callbacks
must not be implemented from a task payload, issue comment or model response.

The unit tests use synthetic data to test fail-closed positive/negative/replay.
They are NOT evidence of a real Qwen repair or Git CAS publication. Existing
app/native_one_shot.py, app/github_cas_transport.py and app/issue_native_adapter.py
remain the intended production authorities. The last is fixed to issue #8;
a separately authorized and tested adapter is still required for Noteri #42.
No READY_FOR_PR may be emitted by this source-only gate.

Issue: https://github.com/ericson-j-santos/engineering-worker-pool/issues/60
Noteri dependency: https://github.com/ericson-j-santos/noteri-runtime/issues/42
Rules: v1.6.20, main@86abe65b22781c2f608a7d7286ce3a28cd48aaaf
