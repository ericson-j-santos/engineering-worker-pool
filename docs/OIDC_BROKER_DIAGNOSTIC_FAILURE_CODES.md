# OIDC broker rejection categories — diagnostic-only

The native continuity producer failed at GitHub Actions run 39490722834
(20 October 2026), receiving identity_broker_http_403. The contemporaneous
client claim-shape diagnostic reported no named static claim mismatches, but
the broker's current SHA allowlist is not exposed to callers. The exact
rejected condition therefore remains unverified; do not assume a missing SHA.

The broker now emits a single **low-cardinality category** for a rejected
cryptographically verified OIDC claim, such as:
- principal_unmatched
- allowlist_configuration_invalid
- source_sha_not_allowlisted
- verified_source_sha_mismatch
- run_identity_invalid
- source_identity_invalid
- job_provenance_invalid
- token_time_invalid

These are internal telemetry categories, never returned in the HTTP response.
The response remains HTTP 403 with the same generic body. Logs must never
contain the JWT, a SHA, token metadata, connection string, private roles,
allowlist values, credentials, or the protected runtime configuration.
Audit logging failures never permit admission.

This change **does not modify** admittedClaims(), its allowlist limits,
signed JWT verification, principal policies, credential read boundary, or
runtime environment variables. The client's diagnostic field now checks
the exact expected JWT audience string, rather than incorrectly expecting
an array. Client claim-shape diagnostics are still not authorization evidence.

## Review and deployment boundary

All changes are code and tests only. Neon continuityidentity deployment
remains untouched; no runtime credentials or EWP_ALLOWED_SHAS are read,
changed or published. Before any new deployment, require a separately
authorized DEV target and SHA, current environment readback without
revealing secrets, cost=R$0.00 qualification, and a governed post-deployment
readback. An existing deployment does not implicitly adopt these changes.

Until real OIDC readback proves the authority, do not dispatch issue #8,
invoke the model again or claim CAS/Validator/agent draft PR completion.
