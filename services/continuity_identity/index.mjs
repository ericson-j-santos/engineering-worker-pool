import { createRemoteJWKSet, jwtVerify } from "jose";

export const ISSUER = "https://token.actions.githubusercontent.com";
export const AUDIENCE = "urn:engineering-worker-pool:continuity:v1";
const jwks = createRemoteJWKSet(new URL(ISSUER + "/.well-known/jwks"), {
  timeoutDuration: 5000, cooldownDuration: 30000, cacheMaxAge: 300000,
});
const OWNER = "ericson-j-santos";
const OWNER_ID = "60470500";
export const PRINCIPALS = Object.freeze({
  "engineering-worker-pool-branch-v1": Object.freeze({
    repository: "ericson-j-santos/engineering-worker-pool",
    repository_id: "1384155635",
    actor: "ericson-j-santos",
    actor_id: "60470500",
    event_name: "push",
    ref: "refs/heads/feat/hosted-cold-checkpoint-resume-20261020",
    workflow_ref: "ericson-j-santos/engineering-worker-pool/.github/workflows/native-live-continuity.yml@refs/heads/feat/hosted-cold-checkpoint-resume-20261020",
    sha_policy: "environment_allowlist",
  }),
  "reqsys-handoff-v1": Object.freeze({
    repository: "ericson-j-santos/reqsys-v2-enterprise-real",
    repository_id: "1228361750",
    actor: "github-actions[bot]",
    actor_id: "41898282",
    event_name: "workflow_dispatch",
    ref: "refs/heads/main",
    workflow_ref: "ericson-j-santos/reqsys-v2-enterprise-real/.github/workflows/codex-worker-pool-handoff.yml@refs/heads/main",
    sha_policy: "verified_current_main",
  }),
});
const HEAD = /^[0-9a-f]{40}$/;
const UINT = /^[1-9][0-9]{0,19}$/;
const PRIVATE_ENDPOINT = "https://br-royal-butterfly-b4as4z27.storage.c-6.us-east-2.aws.neon.tech";
const PRIVATE_BUCKET = "engineering-continuity-artifacts";
const PREFIX = "engineering-worker-pool/checkpoints/live-v1";
const HEADERS = {
  "cache-control": "no-store, max-age=0", "pragma": "no-cache",
  "x-content-type-options": "nosniff", "content-type": "application/json",
};
function response(status, body) {
  return new Response(JSON.stringify(body), { status, headers: HEADERS });
}
function principalForClaims(payload) {
  const matches = Object.entries(PRINCIPALS).filter(([, principal]) => {
    const required = {
      iss: ISSUER, aud: AUDIENCE,
      repository: principal.repository, repository_id: principal.repository_id,
      repository_owner: OWNER, repository_owner_id: OWNER_ID,
      actor: principal.actor, actor_id: principal.actor_id,
      event_name: principal.event_name, ref: principal.ref, ref_type: "branch",
      workflow_ref: principal.workflow_ref, runner_environment: "github-hosted",
    };
    if (Object.entries(required).some(([key, value]) => payload[key] !== value)) return false;
    return payload.sub === "repo:" + principal.repository + ":ref:" + principal.ref;
  });
  return matches.length === 1 ? matches[0] : null;
}

export function admittedClaims(
  payload, allowedShas, nowSeconds = Date.now() / 1000, verifiedSourceSha = null
) {
  const matched = principalForClaims(payload);
  if (!matched) return false;
  const [, principal] = matched;
  if (!UINT.test(String(payload.run_id)) || !UINT.test(String(payload.run_attempt))) return false;
  if (!HEAD.test(payload.sha || "") || payload.workflow_sha !== payload.sha) return false;
  if (principal.sha_policy === "environment_allowlist") {
    if (!Array.isArray(allowedShas) || allowedShas.length < 1 || allowedShas.length > 8 ||
        !allowedShas.every(s => typeof s === "string" && HEAD.test(s)) ||
        new Set(allowedShas).size !== allowedShas.length ||
        !allowedShas.includes(payload.sha)) return false;
  } else if (principal.sha_policy === "verified_current_main") {
    if (verifiedSourceSha !== payload.sha) return false;
  } else return false;
  const jobProvenancePresent = payload.job_workflow_ref !== undefined ||
    payload.job_workflow_sha !== undefined;
  if (jobProvenancePresent && (payload.job_workflow_ref !== principal.workflow_ref ||
      payload.job_workflow_sha !== payload.sha)) return false;
  if (![payload.iat, payload.nbf, payload.exp].every(Number.isSafeInteger)) return false;
  if (payload.iat > nowSeconds || payload.nbf > nowSeconds ||
      payload.exp <= nowSeconds || nowSeconds - payload.iat > 600 ||
      payload.exp - payload.iat > 600 || payload.exp <= payload.iat) return false;
  return true;
}
export function validatedRuntimeConfiguration(raw) {
  const value = JSON.parse(raw);
  const keys = ["authority_id", "schema", "database_url", "aws_access_key_id",
    "aws_secret_access_key", "s3_endpoint", "s3_region", "s3_bucket", "s3_prefix"];
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join() !== keys.sort().join()) throw new Error("configuration_invalid");
  if (!keys.every(k => typeof value[k] === "string" && value[k].length > 0 &&
      value[k].length < 4096 && !/[\r\n\0]/.test(value[k]))) throw new Error("configuration_invalid");
  const uri = new URL(value.database_url);
  if (uri.protocol !== "postgresql:" || !uri.hostname.endsWith(".neon.tech") ||
      uri.username !== "ewp_continuity_runtime" || uri.pathname !== "/neondb" ||
      !uri.password || uri.port && uri.port !== "5432" || uri.hash ||
      [...uri.searchParams.keys()].some(k => !["sslmode", "sslrootcert", "channel_binding", "connect_timeout"].includes(k)) ||
      [...uri.searchParams.keys()].length !== new Set(uri.searchParams.keys()).size ||
      uri.searchParams.get("sslmode") !== "verify-full" ||
      uri.searchParams.get("sslrootcert") !== "system" ||
      !/^[a-z][a-z0-9_]{0,62}$/.test(value.schema) ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value.authority_id) ||
      value.s3_endpoint !== PRIVATE_ENDPOINT || value.s3_bucket !== PRIVATE_BUCKET ||
      value.s3_prefix !== PREFIX || value.s3_region !== "us-east-2") throw new Error("configuration_invalid");
  return value;
}
export async function verifiedGithubToken(token, keySet = jwks) {
  const { payload } = await jwtVerify(token, keySet, {
    issuer: ISSUER, audience: AUDIENCE, algorithms: ["RS256"], typ: "JWT",
    clockTolerance: 0, maxTokenAge: "10m",
    requiredClaims: ["sub", "iat", "nbf", "exp", "repository_id", "repository_owner_id",
      "actor_id", "sha", "workflow_sha", "workflow_ref", "ref", "run_id", "run_attempt"],
  });
  return payload;
}

async function boundedGithubJson(url, maximum, fetcher = fetch) {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || parsed.hostname !== "api.github.com" ||
      parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash)
    throw new Error("github_source_invalid");
  const result = await fetcher(parsed, {
    method: "GET", redirect: "error", cache: "no-store",
    headers: {
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "engineering-worker-pool-continuity-identity/1",
    },
  });
  if (!result || result.status !== 200) throw new Error("github_source_unavailable");
  const raw = await result.text();
  if (raw.length < 2 || raw.length > maximum) throw new Error("github_source_invalid");
  const value = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("github_source_invalid");
  return value;
}

export async function verifiedCurrentMainSha(principal, payload, fetcher = fetch) {
  if (!principal || principal.sha_policy !== "verified_current_main" ||
      principal.repository !== "ericson-j-santos/reqsys-v2-enterprise-real" ||
      principal.ref !== "refs/heads/main") throw new Error("github_source_profile_invalid");
  const root = "https://api.github.com/repos/ericson-j-santos/reqsys-v2-enterprise-real";
  const ref = await boundedGithubJson(root + "/git/ref/heads/main", 65536, fetcher);
  if (ref.ref !== principal.ref || ref.object?.type !== "commit" ||
      ref.object?.sha !== payload.sha) return null;
  const commit = await boundedGithubJson(root + "/git/commits/" + payload.sha, 131072, fetcher);
  if (commit.sha !== payload.sha || commit.verification?.verified !== true ||
      commit.verification?.reason !== "valid") return null;
  return payload.sha;
}
async function boundedJson(request) {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > 512)) throw new Error("request_invalid");
  if (!request.body) throw new Error("request_invalid");
  const reader = request.body.getReader();
  let size = 0;
  const chunks = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 512) { await reader.cancel(); throw new Error("request_invalid"); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}
export function createHandler({ verify = verifiedGithubToken,
  shas = () => (process.env.EWP_ALLOWED_SHAS || "").split(",").filter(Boolean),
  verifySource = verifiedCurrentMainSha,
  configuration = () => validatedRuntimeConfiguration(process.env.EWP_RUNTIME_CONFIG_JSON || ""),
  now = () => Date.now() / 1000 } = {}) {
  return async request => {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET" && !url.search) {
      return response(200, { service: "continuity-identity", version: 1 });
    }
    if (url.pathname !== "/bootstrap" || url.search || request.method !== "POST")
      return response(404, { error: "not_found" });
    const auth = request.headers.get("authorization") || "";
    if (!auth.startsWith("Bearer ") || auth.length > 16384 || auth.length < 20 ||
        /[\r\n]/.test(auth)) return response(401, { error: "unauthorized" });
    let payload;
    try { payload = await verify(auth.slice(7)); }
    catch { return response(401, { error: "unauthorized" }); }
    const matched = principalForClaims(payload);
    if (!matched) return response(403, { error: "identity_not_admitted" });
    const [, principal] = matched;
    let verifiedSourceSha = null;
    if (principal.sha_policy === "verified_current_main") {
      try { verifiedSourceSha = await verifySource(principal, payload); }
      catch { return response(503, { error: "source_verification_unavailable" }); }
    }
    if (!admittedClaims(payload, shas(), now(), verifiedSourceSha))
      return response(403, { error: "identity_not_admitted" });
    try {
      const body = await boundedJson(request);
      if (!body || Object.keys(body).sort().join() !== "purpose,run_id" ||
          body.purpose !== "native-continuity-runtime-v1" || body.run_id !== payload.run_id)
        return response(400, { error: "request_invalid" });
    } catch { return response(400, { error: "request_invalid" }); }
    try {
      const runtime = configuration();
      return response(200, { version: 1, identity: {
        repository_id: payload.repository_id, sha: payload.sha,
        run_id: payload.run_id, run_attempt: payload.run_attempt,
      }, runtime });
    } catch { return response(503, { error: "configuration_unavailable" }); }
  };
}
export default { fetch: createHandler() };
