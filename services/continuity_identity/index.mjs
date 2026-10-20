import { createRemoteJWKSet, jwtVerify } from "jose";

export const ISSUER = "https://token.actions.githubusercontent.com";
export const AUDIENCE = "urn:engineering-worker-pool:continuity:v1";
const jwks = createRemoteJWKSet(new URL(ISSUER + "/.well-known/jwks"), {
  timeoutDuration: 5000, cooldownDuration: 30000, cacheMaxAge: 300000,
});
const REPOSITORY = "ericson-j-santos/engineering-worker-pool";
const REPOSITORY_ID = "1384155635";
const OWNER_ID = "60470500";
const REF = "refs/heads/feat/hosted-cold-checkpoint-resume-20261020";
const WORKFLOW = REPOSITORY + "/.github/workflows/native-live-continuity.yml@" + REF;
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
export function admittedClaims(payload, allowedShas, nowSeconds = Date.now() / 1000) {
  if (!Array.isArray(allowedShas) || allowedShas.length < 1 || allowedShas.length > 4 ||
      !allowedShas.every(s => typeof s === "string" && HEAD.test(s))) return false;
  const required = {
    iss: ISSUER, aud: AUDIENCE, repository: REPOSITORY, repository_id: REPOSITORY_ID,
    repository_owner: "ericson-j-santos", repository_owner_id: OWNER_ID,
    actor: "ericson-j-santos", actor_id: OWNER_ID, event_name: "push",
    ref: REF, ref_type: "branch", workflow_ref: WORKFLOW,
    runner_environment: "github-hosted",
  };
  for (const [key, value] of Object.entries(required)) if (payload[key] !== value) return false;
  if (typeof payload.sub !== "string" || payload.sub.length < 1 || payload.sub.length > 512) return false;
  if (!UINT.test(String(payload.run_id)) || !UINT.test(String(payload.run_attempt))) return false;
  if (!HEAD.test(payload.sha || "") || payload.workflow_sha !== payload.sha ||
      !allowedShas.includes(payload.sha)) return false;
  if (payload.job_workflow_ref !== undefined || payload.job_workflow_sha !== undefined) return false;
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
    try {
      payload = await verify(auth.slice(7));
      if (!admittedClaims(payload, shas(), now())) return response(403, { error: "identity_not_admitted" });
    } catch { return response(401, { error: "unauthorized" }); }
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
