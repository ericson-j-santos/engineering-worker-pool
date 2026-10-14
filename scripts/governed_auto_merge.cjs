"use strict";

/*
 * Governed, event-driven merge for owner-created same-repository PRs.
 * Never executes PR code with write credentials. Policy changes are
 * themselves ineligible for this workflow and must be merged separately.
 */

const REPOSITORY = "ericson-j-santos/engineering-worker-pool";
const OWNER = "ericson-j-santos";
const ACTIONS_APP_ID = 15368;
const MARKER = "<!-- engineering-worker-pool:governed-automerge-v1 -->";
const CI_WORKFLOW = "CI";
const CANARY_WORKFLOW = "Worker Pool Candidate Image Canary";
const ACTIVE = new Set(["queued", "pending", "in_progress", "requested", "waiting"]);
const ALLOWED_PREFIX = /^(?:governed\/)[a-z0-9][a-z0-9._-]{2,150}$/;
const SHA = /^[0-9a-f]{40}$/;

function block(reason) {
  return { allowed: false, reason };
}

function canaryRequired(files) {
  return files.some(({filename}) =>
    filename === "Dockerfile" ||
    filename === ".dockerignore" ||
    filename === "requirements.txt" ||
    filename.startsWith("app/") ||
    filename === "scripts/candidate_canary_probe.py" ||
    filename === "tests/test_candidate_canary_probe.py" ||
    filename === ".github/workflows/candidate-canary.yml");
}

function currentGreenCheck(checks, name, sha) {
  const found = checks.filter(c => c.name === name && c.head_sha === sha);
  return found.length === 1 &&
    found[0].status === "completed" &&
    found[0].conclusion === "success" &&
    found[0].app && found[0].app.id === ACTIONS_APP_ID;
}

function currentGreenRun(runs, name, sha) {
  return runs.some(r => r.name === name &&
    r.event === "pull_request" && r.head_sha === sha &&
    r.status === "completed" && r.conclusion === "success");
}

/*
 * Only repo state and GitHub-provided data are inputs. The PR title, body,
 * files and branch are untrusted: they grant no additional permissions.
 */
function decide({
  repository, trigger, pr, files, checks, workflowRuns,
  compare, branch, reviews
}) {
  if (repository !== REPOSITORY) return block("repository_mismatch");
  if (!trigger || trigger.event !== "pull_request" ||
      trigger.status !== "completed" || trigger.conclusion !== "success" ||
      ![CI_WORKFLOW, CANARY_WORKFLOW].includes(trigger.name) ||
      trigger.head_repository?.full_name !== REPOSITORY ||
      !SHA.test(trigger.head_sha || "")) return block("trigger_untrusted");
  if (!pr || pr.state !== "open" || pr.draft !== false ||
      pr.mergeable !== true || pr.mergeable_state !== "clean" ||
      !Number.isSafeInteger(pr.number) || pr.number < 1 ||
      pr.user?.login !== OWNER ||
      pr.head?.repo?.full_name !== REPOSITORY ||
      pr.base?.repo?.full_name !== REPOSITORY ||
      pr.base?.ref !== "main" ||
      !ALLOWED_PREFIX.test(pr.head?.ref || "") ||
      pr.head?.ref !== trigger.head_branch ||
      pr.head?.sha !== trigger.head_sha ||
      !SHA.test(pr.base?.sha || "") ||
      typeof pr.body !== "string" || !pr.body.includes(MARKER))
    return block("pr_not_admitted");

  if (!Array.isArray(files) || files.length === 0 || files.length > 25)
    return block("diff_scope_invalid");
  const seen = new Set();
  for (const file of files) {
    const path = file?.filename;
    if (typeof path !== "string" || seen.has(path) ||
        file.status === "removed" ||
        path.startsWith(".") ||
        path.includes("..") || path.includes("\\") ||
        !/^(?:app|docs|scripts|tests)\/[a-zA-Z0-9_./-]+$/.test(path) ||
        path === "app/main.py" ||
        path === "app/store.py" ||
        path === "app/native_one_shot.py" ||
        path === "app/native_gateway_commands.py" ||
        path === "scripts/governed_auto_merge.cjs" ||
        path === "tests/test_governed_auto_merge.cjs")
      return block("manual_review_required");
    seen.add(path);
  }

  if (!compare || compare.behind_by !== 0 ||
      !Number.isSafeInteger(compare.ahead_by) ||
      compare.ahead_by < 1 || compare.status !== "ahead")
    return block("branch_out_of_sync");
  if (!branch || branch.protected !== true ||
      branch.commit?.sha !== pr.base.sha ||
      !Array.isArray(branch.protection?.required_status_checks?.checks) ||
      !branch.protection.required_status_checks.checks.some(c =>
        c.context === "test" && c.app_id === ACTIONS_APP_ID))
    return block("branch_protection_not_confirmed");

  if (!Array.isArray(reviews) || reviews.some(r => r.state === "CHANGES_REQUESTED") ||
      (pr.requested_reviewers || []).length > 0 ||
      (pr.requested_teams || []).length > 0)
    return block("review_not_satisfied");

  if (!Array.isArray(checks) || !Array.isArray(workflowRuns) ||
      checks.some(c => c.head_sha === trigger.head_sha &&
        (ACTIVE.has(c.status) || c.conclusion !== "success")) ||
      !currentGreenCheck(checks, "test", trigger.head_sha) ||
      !currentGreenRun(workflowRuns, CI_WORKFLOW, trigger.head_sha))
    return block("ci_not_fully_verified");
  const requiredCanary = canaryRequired(files);
  if (requiredCanary &&
      (!currentGreenCheck(checks, "candidate-canary", trigger.head_sha) ||
       !currentGreenRun(workflowRuns, CANARY_WORKFLOW, trigger.head_sha)))
    return block("canary_not_fully_verified");

  return {
    allowed: true, reason: "ready",
    prNumber: pr.number, expectedHead: trigger.head_sha,
    expectedBase: pr.base.sha, canaryRequired: requiredCanary
  };
}

async function run(github, context, core) {
  const payload = context.payload;
  const trigger = payload.workflow_run;
  const owner = context.repo.owner;
  const repo = context.repo.repo;
  if (!trigger || context.repo.owner + "/" + repo !== REPOSITORY ||
      trigger.event !== "pull_request" || trigger.conclusion !== "success" ||
      ![CI_WORKFLOW, CANARY_WORKFLOW].includes(trigger.name)) {
    core.info("GOVERNED_MERGE_BLOCKED: event_out_of_scope");
    return;
  }
  if (!SHA.test(trigger.head_sha || "") ||
      !ALLOWED_PREFIX.test(trigger.head_branch || "") ||
      trigger.head_repository?.full_name !== REPOSITORY) {
    core.info("GOVERNED_MERGE_BLOCKED: source_identity_invalid");
    return;
  }

  const listed = await github.paginate(github.rest.pulls.list, {
    owner, repo, state: "open", head: OWNER + ":" + trigger.head_branch,
    per_page: 100
  });
  const matching = listed.filter(p => p.head?.sha === trigger.head_sha);
  if (matching.length !== 1) {
    core.info("GOVERNED_MERGE_BLOCKED: pull_request_not_unique");
    return;
  }
  const number = matching[0].number;
  const [prResponse, files, reviews, branchResponse, compareResponse,
         checksResponse, runsResponse] = await Promise.all([
    github.rest.pulls.get({owner, repo, pull_number: number}),
    github.paginate(github.rest.pulls.listFiles, {
      owner, repo, pull_number: number, per_page: 100
    }),
    github.paginate(github.rest.pulls.listReviews, {
      owner, repo, pull_number: number, per_page: 100
    }),
    github.rest.repos.getBranch({owner, repo, branch: "main"}),
    github.rest.repos.compareCommitsWithBasehead({
      owner, repo, basehead: "main..." + trigger.head_sha
    }),
    github.rest.checks.listForRef({
      owner, repo, ref: trigger.head_sha, per_page: 100
    }),
    github.rest.actions.listWorkflowRunsForRepo({
      owner, repo, head_sha: trigger.head_sha, per_page: 100
    })
  ]);
  if (checksResponse.data.total_count > 100 ||
      runsResponse.data.total_count > 100 ||
      files.length > 25) {
    core.info("GOVERNED_MERGE_BLOCKED: evidence_paginated_or_oversized");
    return;
  }
  const pr = prResponse.data;
  const result = decide({
    repository: REPOSITORY, trigger, pr, files,
    reviews, branch: branchResponse.data,
    compare: compareResponse.data,
    checks: checksResponse.data.check_runs,
    workflowRuns: runsResponse.data.workflow_runs
  });
  if (!result.allowed) {
    core.info("GOVERNED_MERGE_BLOCKED: " + result.reason);
    return;
  }

  // A concurrent PR or base update invalidates this decision. The merge
  // request itself also passes the expected exact head SHA to GitHub.
  const [latestPr, latestBranch] = await Promise.all([
    github.rest.pulls.get({owner, repo, pull_number: number}),
    github.rest.repos.getBranch({owner, repo, branch: "main"})
  ]);
  if (latestPr.data.head.sha !== result.expectedHead ||
      latestPr.data.base.sha !== result.expectedBase ||
      latestPr.data.state !== "open" ||
      latestPr.data.draft !== false ||
      latestPr.data.mergeable !== true ||
      latestPr.data.mergeable_state !== "clean" ||
      latestBranch.data.commit.sha !== result.expectedBase ||
      latestBranch.data.protected !== true) {
    core.info("GOVERNED_MERGE_BLOCKED: concurrent_state_change");
    return;
  }

  // Server-side branch protection is never bypassed. No push or force is used.
  let response;
  try {
    response = await github.rest.pulls.merge({
      owner, repo, pull_number: number, sha: result.expectedHead,
      merge_method: "merge"
    });
  } catch (e) {
    core.warning("GOVERNED_MERGE_BLOCKED: github_merge_denied_http_" +
      String(e.status || "unknown"));
    return;
  }
  if (response.data.merged !== true || !SHA.test(response.data.sha || "")) {
    core.setFailed("GOVERNED_MERGE_BLOCKED: merge_result_invalid");
    return;
  }
  const post = await github.rest.repos.getBranch({owner, repo, branch: "main"});
  if (post.data.commit.sha !== response.data.sha) {
    core.setFailed("GOVERNED_MERGE_BLOCKED: independent_main_readback_failed");
    return;
  }
  core.info("GOVERNED_MERGE_VERIFIED: pr=" + number +
    " sha=" + response.data.sha + " expected_head=" + result.expectedHead);
}

module.exports = { decide, canaryRequired, run };
