"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { decide, canaryRequired, run } = require("../scripts/governed_auto_merge.cjs");

const SHA = "a".repeat(40);
const BASE = "b".repeat(40);
const REPO = "ericson-j-santos/engineering-worker-pool";
const MARKER = "<!-- engineering-worker-pool:governed-automerge-v1 -->";

function fixture(overrides={}) {
  const source = {
    repository: REPO,
    trigger: {
      name: "CI", event: "pull_request", status: "completed",
      conclusion: "success", head_sha: SHA, head_branch: "governed/real-fix-20261014",
      head_repository: { full_name: REPO }
    },
    pr: {
      number: 42, state: "open", draft: false,
      mergeable: true, mergeable_state: "clean",
      body: "real change\n" + MARKER,
      user: { login: "ericson-j-santos" },
      requested_reviewers: [], requested_teams: [],
      head: { sha: SHA, ref: "governed/real-fix-20261014", repo: { full_name: REPO } },
      base: { sha: BASE, ref: "main", repo: { full_name: REPO } }
    },
    files: [{ filename: "app/isolated_fix.py", status: "modified" }],
    checks: [
      {name:"test",head_sha:SHA,status:"completed",conclusion:"success",app:{id:15368}},
      {name:"candidate-canary",head_sha:SHA,status:"completed",conclusion:"success",app:{id:15368}}
    ],
    workflowRuns: [
      {name:"CI", event:"pull_request",status:"completed",conclusion:"success",head_sha:SHA},
      {name:"Worker Pool Candidate Image Canary",event:"pull_request",status:"completed",conclusion:"success",head_sha:SHA}
    ],
    compare: {status:"ahead",ahead_by:1,behind_by:0},
    branch: {
      protected:true,commit:{sha:BASE},
      protection:{required_status_checks:{checks:[{context:"test",app_id:15368}]}}
    },
    reviews: []
  };
  return {...source,...overrides};
}

function blocked(reason, change) {
  const result = decide({...fixture(), ...change});
  assert.equal(result.allowed, false, reason);
  assert.equal(result.reason, reason);
}

test("genuine owner-scoped PR with two real gates may merge", () => {
  const data = fixture();
  const x = decide(data);
  assert.equal(x.allowed, true);
  assert.equal(x.expectedHead, SHA);
  assert.equal(x.expectedBase, BASE);
  assert.equal(x.prNumber, 42);
  assert.equal(x.canaryRequired, true);
});

test("docs-only passes without nonexistent path-scoped canary", () => {
  const data = fixture({
    files:[{filename:"docs/CHANGELOG_NATIVE.md",status:"added"}],
    checks:fixture().checks.filter(c=>c.name==="test"),
    workflowRuns:fixture().workflowRuns.filter(r=>r.name==="CI")
  });
  assert.equal(canaryRequired(data.files), false);
  assert.equal(decide(data).allowed, true);
});

test("repo, event, workflow, fork, author, branch and marker are strict", () => {
  blocked("repository_mismatch", {repository:"different/repo"});
  blocked("trigger_untrusted", {trigger:{...fixture().trigger,event:"push"}});
  blocked("trigger_untrusted", {trigger:{...fixture().trigger,name:"Unknown Workflow"}});
  blocked("trigger_untrusted", {trigger:{...fixture().trigger,conclusion:"failure"}});
  blocked("pr_not_admitted", {trigger:{...fixture().trigger,head_sha:BASE}});
  blocked("trigger_untrusted", {trigger:{...fixture().trigger,head_repository:{full_name:"some/fork"}}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,user:{login:"other"}}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,head:{...fixture().pr.head,repo:{full_name:"other/fork"}}}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,head:{...fixture().pr.head,ref:"feat/unguarded"}}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,body:"no marker"}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,draft:true}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,mergeable_state:"blocked"}});
  blocked("pr_not_admitted", {pr:{...fixture().pr,base:{...fixture().pr.base,ref:"dev"}}});
});

test("no empty, arbitrary or security-control self-edits", () => {
  blocked("diff_scope_invalid", {files:[]});
  for (const path of [
    ".github/workflows/governed-auto-merge.yml",
    "scripts/governed_auto_merge.cjs",
    "tests/test_governed_auto_merge.cjs",
    "app/main.py",
    "app/store.py",
    "app/native_gateway_commands.py",
    "app/native_one_shot.py",
    "docs/../.github/workflows/bypass.yml",
    "etc/password",
    "README.md"
  ]) {
    blocked("manual_review_required", {files:[{filename:path,status:"modified"}]});
  }
  blocked("manual_review_required", {
    files:[{filename:"docs/one.md",status:"modified"},{filename:"docs/one.md",status:"modified"}]
  });
  blocked("manual_review_required", {files:[{filename:"docs/one.md",status:"removed"}]});
});

test("missing, stale, failed and spoofed required CI is never accepted", () => {
  blocked("ci_not_fully_verified", {checks:[]});
  blocked("ci_not_fully_verified", {workflowRuns:[]});
  blocked("ci_not_fully_verified", {checks:fixture().checks.map(c=>c.name==="test"?{...c,conclusion:"skipped"}:c)});
  blocked("ci_not_fully_verified", {checks:fixture().checks.map(c=>c.name==="test"?{...c,app:{id:123}}:c)});
  blocked("ci_not_fully_verified", {checks:fixture().checks.map(c=>c.name==="test"?{...c,status:"in_progress"}:c)});
  blocked("ci_not_fully_verified", {workflowRuns:fixture().workflowRuns.map(r=>r.name==="CI"?{...r,event:"push"}:r)});
  blocked("ci_not_fully_verified", {workflowRuns:fixture().workflowRuns.map(r=>r.name==="CI"?{...r,head_sha:BASE}:r)});
  blocked("canary_not_fully_verified", {checks:fixture().checks.filter(c=>c.name==="test")});
  blocked("canary_not_fully_verified", {workflowRuns:fixture().workflowRuns.filter(r=>r.name==="CI")});
});

test("base drift, unprotected main, mismatched GitHub App, reviews", () => {
  blocked("branch_out_of_sync", {compare:{status:"diverged",ahead_by:1,behind_by:1}});
  blocked("branch_out_of_sync", {compare:{status:"identical",ahead_by:0,behind_by:0}});
  blocked("branch_protection_not_confirmed", {branch:{...fixture().branch,protected:false}});
  blocked("branch_protection_not_confirmed", {branch:{...fixture().branch,commit:{sha:SHA}}});
  blocked("branch_protection_not_confirmed", {branch:{...fixture().branch,protection:{required_status_checks:{checks:[{context:"test",app_id:999}]}}}});
  blocked("review_not_satisfied", {reviews:[{state:"CHANGES_REQUESTED"}]});
  blocked("review_not_satisfied", {pr:{...fixture().pr,requested_reviewers:[{login:"reviewer"}]}});
});

function harness(options={}) {
  const data = fixture();
  let calls=0;
  let mergeArgs;
  let getCalls=0;
  let serverMerged=false;
  let logs=[];
  const latestSha=options.latestSha || SHA;
  const mergeSha="c".repeat(40);
  const pr = {...data.pr};
  const symbols = {list:Symbol("list"),listFiles:Symbol("files"),listReviews:Symbol("reviews")};
  const github = {
    paginate: async (method) => {
      if (method === symbols.list) return [pr];
      if (method === symbols.listFiles) return data.files;
      if (method === symbols.listReviews) return [];
      throw Error("unexpected pagination");
    },
    rest: {
      pulls: {
        list:symbols.list,listFiles:symbols.listFiles,listReviews:symbols.listReviews,
        get:async()=>{getCalls++;return {data:getCalls>1?{
          ...pr,head:{...pr.head,sha:latestSha}
        }:pr}},
        merge:async args=>{
          calls++;mergeArgs=args;
          if (options.deny) throw {status:405};
          serverMerged = true;
          return {data:{merged:true,sha:mergeSha}};
        }
      },
      repos:{
        getBranch:async()=>({data:{
          ...data.branch,commit:{
            sha:options.branchHead ||
              (serverMerged && !options.stalePostReadback ? mergeSha : BASE)
          }
        }}),
        compareCommitsWithBasehead:async()=>({data:data.compare})
      },
      checks:{listForRef:async()=>({data:{total_count:data.checks.length,check_runs:data.checks}})},
      actions:{listWorkflowRunsForRepo:async()=>({data:{total_count:data.workflowRuns.length,workflow_runs:data.workflowRuns}})}
    }
  };
  const core = {
    info:s=>logs.push(s),warning:s=>logs.push(s),
    setFailed:s=>logs.push(s)
  };
  const context={repo:{owner:"ericson-j-santos",repo:"engineering-worker-pool"},
    payload:{workflow_run:data.trigger}};
  return {github,core,context,logs,getCalls:()=>getCalls,
    mergeCalls:()=>calls,mergeArgs:()=>mergeArgs};
}

test("integration: only official merge endpoint with expected head, independent main readback",async()=>{
  const x=harness();
  await run(x.github,x.context,x.core);
  assert.equal(x.mergeCalls(),1);
  assert.equal(x.mergeArgs().sha,SHA);
  assert.equal(x.mergeArgs().pull_number,42);
  assert.equal(x.mergeArgs().merge_method,"merge");
  assert.equal(x.logs.some(s=>s.startsWith("GOVERNED_MERGE_VERIFIED:")),true);
});

test("integration: concurrency prevents stale-head merge",async()=>{
  const x=harness({latestSha:BASE});
  await run(x.github,x.context,x.core);
  assert.equal(x.mergeCalls(),0);
  assert.equal(x.logs.some(s=>s.includes("concurrent_state_change")),true);
});

test("integration: GitHub refuses merge -> no fake success",async()=>{
  const x=harness({deny:true});
  await run(x.github,x.context,x.core);
  assert.equal(x.mergeCalls(),1);
  assert.equal(x.logs.some(s=>s.includes("github_merge_denied_http_405")),true);
  assert.equal(x.logs.some(s=>s.startsWith("GOVERNED_MERGE_VERIFIED:")),false);
});

test("integration: repository/main changed -> no merge",async()=>{
  const x=harness({branchHead:SHA});
  await run(x.github,x.context,x.core);
  assert.equal(x.mergeCalls(),0);
  assert.equal(x.logs.some(s=>s.includes("branch_protection_not_confirmed")),true);
});

test("integration: server merge without independent main readback blocks success",async()=>{
  const x=harness({stalePostReadback:true});
  await run(x.github,x.context,x.core);
  assert.equal(x.mergeCalls(),1);
  assert.equal(x.logs.some(s=>s.includes("independent_main_readback_failed")),true);
  assert.equal(x.logs.some(s=>s.startsWith("GOVERNED_MERGE_VERIFIED:")),false);
});
