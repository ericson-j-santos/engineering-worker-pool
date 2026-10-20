import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPair, exportJWK, createLocalJWKSet, SignJWT } from "jose";
import { ISSUER, AUDIENCE, PRINCIPALS, admittedClaims, validatedRuntimeConfiguration,
  verifiedGithubToken, verifiedCurrentMainSha, createHandler } from "./index.mjs";
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
  subject: { sub: "repo:ericson-j-santos/engineering-worker-pool:ref:refs/heads/main" },
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
  partialExactJobRef: { job_workflow_ref: claims.workflow_ref },
  foreignJobRef: { job_workflow_ref: "other", job_workflow_sha: sha },
  divergentJobSha: { job_workflow_ref: claims.workflow_ref, job_workflow_sha: "a".repeat(40) },
  nullJobRef: { job_workflow_ref: null, job_workflow_sha: sha },
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

test("signed direct workflow provenance must match the admitted workflow and SHA", async () => {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = await exportJWK(publicKey);
  const keys = createLocalJWKSet({ keys: [{ ...jwk, kid: "direct-job", alg: "RS256" }] });
  const direct = { ...claims, job_workflow_ref: claims.workflow_ref, job_workflow_sha: sha };
  const signed = await new SignJWT(direct)
    .setProtectedHeader({ alg: "RS256", typ: "JWT", kid: "direct-job" }).sign(privateKey);
  let reads = 0;
  const handler = createHandler({
    verify: value => verifiedGithubToken(value, keys),
    shas: () => [sha], configuration: () => { reads++; return config; }, now: () => now,
  });
  const result = await handler(request(undefined, "Bearer " + signed));
  assert.equal(result.status, 200);
  assert.equal(reads, 1);
  assert.equal((await result.json()).identity.sha, sha);
});


const reqsysSha = "c".repeat(40);
const reqsysClaims = {
  iss: ISSUER, aud: AUDIENCE,
  sub: "repo:ericson-j-santos/reqsys-v2-enterprise-real:ref:refs/heads/main",
  repository: "ericson-j-santos/reqsys-v2-enterprise-real", repository_id: "1228361750",
  repository_owner: "ericson-j-santos", repository_owner_id: "60470500",
  actor: "github-actions[bot]", actor_id: "41898282", event_name: "workflow_dispatch",
  ref: "refs/heads/main", ref_type: "branch",
  workflow_ref: "ericson-j-santos/reqsys-v2-enterprise-real/.github/workflows/codex-worker-pool-handoff.yml@refs/heads/main",
  workflow_sha: reqsysSha, sha: reqsysSha, runner_environment: "github-hosted",
  run_id: "98765", run_attempt: "1", iat: now - 10, nbf: now - 10, exp: now + 290,
};

test("exact ReqSys handoff identity is admitted without broadening the EWP profile", async () => {
  assert.equal(admittedClaims(reqsysClaims, [], now, reqsysSha), true);
  assert.equal(admittedClaims(reqsysClaims, [reqsysSha], now), false);
  let reads = 0;
  const handler = createHandler({
    verify: async () => reqsysClaims, shas: () => [],
    verifySource: async () => reqsysSha,
    configuration: () => { reads++; return config; }, now: () => now,
  });
  const result = await handler(request(
    { purpose: "native-continuity-runtime-v1", run_id: reqsysClaims.run_id }
  ));
  assert.equal(result.status, 200);
  assert.equal((await result.json()).identity.repository_id, "1228361750");
  assert.equal(reads, 1);
});

test("cross-profile claim splicing is rejected before reading credentials", async () => {
  const mutations = [
    { workflow_ref: claims.workflow_ref },
    { repository: claims.repository, repository_id: claims.repository_id },
    { actor: claims.actor, actor_id: claims.actor_id },
    { event_name: claims.event_name },
    { ref: claims.ref },
    { sub: claims.sub },
  ];
  for (const mutation of mutations) {
    let reads = 0;
    const handler = createHandler({
      verify: async () => ({ ...reqsysClaims, ...mutation }),
      shas: () => [], verifySource: async () => reqsysSha,
      configuration: () => { reads++; return config; }, now: () => now,
    });
    const result = await handler(request(
      { purpose: "native-continuity-runtime-v1", run_id: reqsysClaims.run_id }
    ));
    assert.equal(result.status, 403);
    assert.equal(reads, 0);
  }
});

test("ReqSys reusable workflow provenance, when present, is exact", async () => {
  const direct = {
    ...reqsysClaims,
    job_workflow_ref: reqsysClaims.workflow_ref,
    job_workflow_sha: reqsysSha,
  };
  assert.equal(admittedClaims(direct, [], now, reqsysSha), true);
  assert.equal(admittedClaims({ ...direct, job_workflow_ref: claims.workflow_ref }, [], now, reqsysSha), false);
  assert.equal(admittedClaims({ ...direct, job_workflow_sha: sha }, [], now, reqsysSha), false);
});


function githubResponse(value, status = 200) {
  return { status, text: async () => JSON.stringify(value) };
}

test("ReqSys source verification binds OIDC SHA to current signed main commit", async () => {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push([String(url), options]);
    if (calls.length === 1) return githubResponse({
      ref: "refs/heads/main", object: { type: "commit", sha: reqsysSha },
    });
    return githubResponse({
      sha: reqsysSha, verification: { verified: true, reason: "valid" },
    });
  };
  const principal = PRINCIPALS["reqsys-handoff-v1"];
  assert.equal(await verifiedCurrentMainSha(principal, reqsysClaims, fetcher), reqsysSha);
  assert.equal(calls.length, 2);
  assert.ok(calls.every(([, options]) => options.redirect === "error" && options.cache === "no-store"));
  assert.ok(calls.every(([url]) => url.startsWith("https://api.github.com/repos/ericson-j-santos/reqsys-v2-enterprise-real/")));
});

test("ReqSys source mismatch and unsigned current main fail closed", async () => {
  const principal = PRINCIPALS["reqsys-handoff-v1"];
  let calls = 0;
  assert.equal(await verifiedCurrentMainSha(principal, reqsysClaims, async () => {
    calls++;
    return githubResponse({
      ref: "refs/heads/main", object: { type: "commit", sha: "d".repeat(40) },
    });
  }), null);
  assert.equal(calls, 1);
  assert.equal(await verifiedCurrentMainSha(principal, reqsysClaims, async url => {
    if (String(url).includes("/git/ref/")) return githubResponse({
      ref: "refs/heads/main", object: { type: "commit", sha: reqsysSha },
    });
    return githubResponse({
      sha: reqsysSha, verification: { verified: false, reason: "unsigned" },
    });
  }), null);
});

test("source verification outage and mismatch never read private configuration", async () => {
  for (const mode of ["unavailable", "mismatch"]) {
    let reads = 0;
    const handler = createHandler({
      verify: async () => reqsysClaims, shas: () => [],
      verifySource: async () => {
        if (mode === "unavailable") throw new Error("synthetic outage");
        return null;
      },
      configuration: () => { reads++; return config; }, now: () => now,
    });
    const result = await handler(request(
      { purpose: "native-continuity-runtime-v1", run_id: reqsysClaims.run_id }
    ));
    assert.equal(result.status, mode === "unavailable" ? 503 : 403);
    assert.equal(reads, 0);
  }
});

test("historical profile still requires environment SHA allowlist", () => {
  assert.equal(admittedClaims(claims, [sha], now), true);
  assert.equal(admittedClaims(claims, [], now, sha), false);
  assert.equal(admittedClaims(claims, ["d".repeat(40)], now, sha), false);
});
