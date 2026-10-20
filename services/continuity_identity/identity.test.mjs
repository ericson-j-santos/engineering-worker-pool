import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPair, exportJWK, createLocalJWKSet, SignJWT } from "jose";
import { ISSUER, AUDIENCE, admittedClaims, validatedRuntimeConfiguration,
  verifiedGithubToken, createHandler } from "./index.mjs";
const sha = "b".repeat(40);
const now = Math.floor(Date.now() / 1000);
const claims = {
  iss: ISSUER, aud: AUDIENCE,
  sub: "repo:ericson-j-santos/engineering-worker-pool:ref:refs/heads/feat/hosted-cold-checkpoint-resume-20261020",
  repository: "ericson-j-santos/engineering-worker-pool", repository_id: "1384155635",
  repository_owner: "ericson-j-santos", repository_owner_id: "60470500",
  actor: "ericson-j-santos", actor_id: "60470500", event_name: "push",
  ref: "refs/heads/feat/hosted-cold-checkpoint-resume-20261020", ref_type: "branch",
  workflow_ref: "ericson-j-santos/engineering-worker-pool/.github/workflows/native-live-continuity.yml@refs/heads/feat/hosted-cold-checkpoint-resume-20261020",
  workflow_sha: sha, sha, runner_environment: "github-hosted",
  run_id: "12345", run_attempt: "1", iat: now - 10, nbf: now - 10, exp: now + 290,
};
const config = {
  authority_id: "51f8d7cb-133a-4e57-9cd9-d22b2e224aff", schema: "ewp_continuity_v1",
  database_url: "postgresql://ewp_continuity_runtime:test-only@ep-fixture.neon.tech/neondb?sslmode=verify-full&sslrootcert=system",
  aws_access_key_id: "test-key", aws_secret_access_key: "test-secret",
  s3_endpoint: "https://br-royal-butterfly-b4as4z27.storage.c-6.us-east-2.aws.neon.tech",
  s3_region: "us-east-2", s3_bucket: "engineering-continuity-artifacts",
  s3_prefix: "engineering-worker-pool/checkpoints/live-v1",
};
function request(body = { purpose: "native-continuity-runtime-v1", run_id: "12345" }, authorization = "Bearer " + "x".repeat(32)) {
  return new Request("https://identity.invalid/bootstrap", { method: "POST",
    headers: { authorization, "content-type": "application/json" }, body: JSON.stringify(body) });
}
test("exact admitted identity and idempotent exchange", async () => {
  assert.equal(admittedClaims(claims, [sha], now), true);
  let reads = 0;
  const handler = createHandler({ verify: async () => claims, shas: () => [sha],
    configuration: () => { reads++; return config; }, now: () => now });
  const first = await handler(request());
  const second = await handler(request());
  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), await second.json());
  assert.equal(first.headers.get("cache-control"), "no-store, max-age=0");
  assert.equal(reads, 2);
});
const drifts = {
  issuer: { iss: "https://attacker.invalid" }, audience: { aud: "other" },
  audienceArray: { aud: [AUDIENCE, "other"] }, repo: { repository: "attacker/repo" },
  repoId: { repository_id: "1" }, owner: { repository_owner: "other" },
  ownerId: { repository_owner_id: "1" }, actor: { actor: "other" },
  actorId: { actor_id: "1" }, event: { event_name: "pull_request_target" },
  branch: { ref: "refs/heads/main" }, refType: { ref_type: "tag" },
  workflow: { workflow_ref: "other" }, workflowSha: { workflow_sha: "a".repeat(40) },
  codeSha: { sha: "a".repeat(40), workflow_sha: "a".repeat(40) },
  runner: { runner_environment: "self-hosted" }, invalidRun: { run_id: "0" },
  invalidAttempt: { run_attempt: "../1" }, expired: { exp: now },
  future: { iat: now + 1 }, notYet: { nbf: now + 1 },
  old: { iat: now - 601 }, long: { exp: now + 1000 },
  missingSubject: { sub: "" }, reusable: { job_workflow_ref: "other" },
  reusableSha: { job_workflow_sha: sha }, nonInteger: { iat: 1.5 },
};
for (const [name, mutation] of Object.entries(drifts)) {
  test("reject identity drift before reading credentials: " + name, async () => {
    let reads = 0;
    const handler = createHandler({ verify: async () => ({ ...claims, ...mutation }),
      shas: () => [sha], configuration: () => { reads++; return config; }, now: () => now });
    const result = await handler(request());
    assert.equal(result.status, 403);
    assert.equal(reads, 0);
    assert.equal((await result.text()).includes("test-secret"), false);
  });
}
test("missing and invalid signatures never access credentials", async () => {
  let reads = 0;
  const handler = createHandler({ verify: async () => { throw new Error("signature"); },
    shas: () => [sha], configuration: () => { reads++; return config; } });
  assert.equal((await handler(request({}, ""))).status, 401);
  assert.equal((await handler(request())).status, 401);
  assert.equal(reads, 0);
});
test("request binding, size, path, and safe health", async () => {
  let reads = 0;
  const handler = createHandler({ verify: async () => claims, shas: () => [sha],
    configuration: () => { reads++; return config; }, now: () => now });
  for (const body of [{ purpose: "native-continuity-runtime-v1", run_id: "999" },
    { purpose: "owner", run_id: "12345" },
    { purpose: "native-continuity-runtime-v1", run_id: "12345", admin: true },
    { purpose: "x".repeat(600), run_id: "12345" }])
    assert.equal((await handler(request(body))).status, 400);
  assert.equal((await handler(new Request("https://identity.invalid/bootstrap?secret=1"))).status, 404);
  const health = await handler(new Request("https://identity.invalid/health"));
  assert.deepEqual(await health.json(), { service: "continuity-identity", version: 1 });
  assert.equal(reads, 0);
});
test("configuration pins runtime role, TLS, bucket, endpoint and exact schema", () => {
  assert.deepEqual(validatedRuntimeConfiguration(JSON.stringify(config)), config);
  for (const mutation of [
    { database_url: config.database_url.replace("verify-full", "require") },
    { database_url: config.database_url.replace("ewp_continuity_runtime", "neondb_owner") },
    { database_url: config.database_url.replace(".neon.tech", ".attacker.invalid") },
    { s3_endpoint: "http://localhost" }, { s3_bucket: "other" },
    { s3_prefix: "engineering-worker-pool" }, { s3_region: "other" },
    { schema: "../public" }, { authority_id: "invalid" }, { extra: "leak" },
  ]) assert.throws(() => validatedRuntimeConfiguration(JSON.stringify({ ...config, ...mutation })));
});
test("real RSA verification and forged/expired tokens", async () => {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const publicJwk = { ...(await exportJWK(publicKey)), kid: "test-key", alg: "RS256", use: "sig" };
  const keys = createLocalJWKSet({ keys: [publicJwk] });
  const sign = payload => new SignJWT(payload).setProtectedHeader({ alg: "RS256", typ: "JWT", kid: "test-key" }).sign(privateKey);
  const token = await sign(claims);
  assert.deepEqual(await verifiedGithubToken(token, keys), claims);
  const parts = token.split(".");
  parts[1] = Buffer.from(JSON.stringify({ ...claims, actor_id: "1" })).toString("base64url");
  await assert.rejects(verifiedGithubToken(parts.join("."), keys));
  await assert.rejects(verifiedGithubToken(await sign({ ...claims, exp: now - 1 }), keys));
  await assert.rejects(verifiedGithubToken(await sign({ ...claims, aud: "other" }), keys));
  const none = Buffer.from('{"alg":"none","typ":"JWT"}').toString("base64url") + "." + parts[1] + ".";
  await assert.rejects(verifiedGithubToken(none, keys));
});
