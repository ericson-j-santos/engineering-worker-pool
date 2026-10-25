"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const yaml = fs.readFileSync(
  path.join(__dirname, "..", ".github", "workflows",
    "issue-qwen7b-cas-token-broker-dev.yml"), "utf8");
const owner = "ericson-j-santos", repo = "engineering-worker-pool";
const base = "958990e201c18219a8284f37437ed308c95d1a71";
const sha = "d".repeat(40);
const approval = "/local-agent authorize bridge.property.v1 sha:" + base;
const marker = "ISSUE_CAS_BROKER_OWNER_20261025_ONCE";
const fixed = Buffer.from("CWP-S3-PREFLIGHT-V1\n", "utf8");
const digest = crypto.createHash("sha256").update(fixed).digest("hex");
const objectPath = "evidence/cas/qwen7b-cas-v1/preflight-v1/sha256/" +
  digest.slice(0, 2) + "/" + digest + ".probe";
const gitBlob = (bytes) => crypto.createHash("sha1").update(
  Buffer.concat([Buffer.from("blob " + bytes.length + "\0"), bytes])
).digest("hex");

function scripts() {
  const lines = yaml.split(/\r?\n/), result = [];
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] !== "          script: |") continue;
    let block = [];
    for (let j = i + 1; j < lines.length; j++) {
      if (lines[j].startsWith("            ")) {
        block.push(lines[j].slice(12));
      } else if (lines[j].trim() === "") {
        block.push("");
      } else {
        break;
      }
    }
    result.push(block.join("\n"));
  }
  return result;
}
const code = scripts();
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function mock() {
  const state = {
    main: base, prDraft: true, prSha: sha, issueOpen: true,
    ownerApproved: true, sourceSha: "d9a25c574163c49f695e774609949160d94f4490",
    branch: null, content: null, writes: [], failures: [], info: [], checksOk: true,
  };
  const data = (value) => Promise.resolve({data: value});
  const notFound = () => {const e = new Error("not found");e.status = 404;throw e;};
  const pull = () => ({
    number: 47, state: "open", draft: state.prDraft, body: "approval\n" + marker,
    head: {ref: "feat/issue-qwen7b-native-github-cas-writer-20261025",
           sha: state.prSha,repo:{full_name:owner + "/" + repo}},
    base: {sha:base},
  });
  const rest = {
    pulls: {get:async()=>data(pull())},
    issues: {
      get:async()=>data({state:state.issueOpen?"open":"closed"}),
      getComment:async()=>data({
        user:{login:owner},author_association:"OWNER",
        issue_url:"https://api.github.com/repos/" + owner + "/" + repo + "/issues/8",
        body:state.ownerApproved?approval:"/local-agent revoke",
      }),
    },
    checks: {listForRef:async()=>data({check_runs:state.checksOk ?
      ["test", "native-contract"].map(name=>({
        name,head_sha:sha,status:"completed",conclusion:"success",
        started_at:"2026-10-25T08:00:00Z",completed_at:"2026-10-25T08:01:00Z",
      })) : []})},
    git: {createRef:async(input)=>{
      if(state.branch)throw Error("duplicate");
      assert.equal(input.ref,"refs/heads/agent/cas-qwen7b-issue-8-" + base.slice(0,12));
      assert.equal(input.sha,base);
      state.branch=base;state.writes.push("createRef");return data({ref:input.ref});
    }},
    repos: {
      getBranch:async(input)=>{
        if(input.branch==="main")return data({commit:{sha:state.main}});
        if(input.branch==="agent/cas-qwen7b-issue-8-" + base.slice(0,12) && state.branch)
          return data({commit:{sha:state.branch}});
        return notFound();
      },
      getContent:async(input)=>{
        if(input.path==="app/portfolio_bridge.py")
          return data({type:"file",sha:state.sourceSha});
        if(input.path!==objectPath || !state.content)return notFound();
        return data({
          type:"file",path:objectPath,sha:gitBlob(state.content),
          size:state.content.length,encoding:"base64",
          content:state.content.toString("base64"),
        });
      },
      createOrUpdateFileContents:async(input)=>{
        assert.equal(input.path,objectPath);
        assert.equal(input.branch,"agent/cas-qwen7b-issue-8-"+base.slice(0,12));
        state.writes.push("writeContent");
        state.content=Buffer.from(input.content,"base64");
        state.branch="e".repeat(40);
        return data({commit:{sha:state.branch}});
      },
      compareCommitsWithBasehead:async(input)=>data({
        status:state.content?"ahead":"identical",
        ahead_by:state.content?1:0,behind_by:0,
        total_commits:state.content?1:0,
        files:state.content?[{filename:objectPath,status:"added"}]:[],
      }),
    },
  };
  return {
    state, github:{rest}, core:{
      setFailed:(reason)=>state.failures.push(reason),
      info:(msg)=>state.info.push(msg),
    },
    context:{
      actor:owner,repo:{owner,repo},
      payload:{pull_request:{number:47,head:{sha}}},
    },
  };
}
async function invoke(index, fixture) {
  const fn=new AsyncFunction("github","context","core","require",code[index]);
  return fn(fixture.github,fixture.context,fixture.core,require);
}
test("broker scripts are syntactically executable and privileged job has no PR checkout", () => {
  assert.equal(code.length,3);
  for(const s of code)assert.doesNotThrow(
    ()=>new AsyncFunction("github","context","core","require",s));
  const block=yaml.split("\n  publish:\n")[1].split("\n  independent-readback:\n")[0];
  assert.match(block,/contents: write/);
  assert.doesNotMatch(block,/actions\/checkout/);
  assert.doesNotMatch(block,/secrets\./);
  assert.match(yaml,/persist-credentials: false/);
  assert.match(yaml,/github\.event\.changes\.body\.from/);
  assert.match(yaml,/!contains\(github\.event\.changes\.body\.from/);
  assert.match(yaml,/github\.event\.pull_request\.draft == true/);
  assert.match(yaml,/cancel-in-progress: false/);
});
test("positive owner admission, one GitHub CAS write, independent readback and replay", async()=>{
  const f=mock();
  await invoke(0,f);
  assert.deepEqual(f.state.failures,[]);
  await invoke(1,f);
  assert.deepEqual(f.state.failures,[]);
  assert.deepEqual(f.state.writes,["createRef","writeContent"]);
  assert.ok(f.state.content.equals(fixed));
  await invoke(2,f);
  assert.deepEqual(f.state.failures,[]);
  await invoke(1,f);
  assert.deepEqual(f.state.writes,["createRef","writeContent"]);
  assert.deepEqual(f.state.failures,[]);
});
test("stale owner/main/CI/source blocks admission with no writes",async()=>{
  for(const kind of ["main","owner","issue","source","CI","branch"]) {
    const f=mock();
    if(kind==="main")f.state.main="f".repeat(40);
    if(kind==="owner")f.state.ownerApproved=false;
    if(kind==="issue")f.state.issueOpen=false;
    if(kind==="source")f.state.sourceSha="a".repeat(40);
    if(kind==="CI")f.state.checksOk=false;
    if(kind==="branch")f.state.prSha="f".repeat(40);
    await invoke(0,f);
    assert.equal(f.state.failures.length,1,kind);
    assert.deepEqual(f.state.writes,[],kind);
  }
});
test("writer rejects revoked owner and changed main without external side effects",async()=>{
  for(const kind of ["owner","main","issue","draft"]) {
    const f=mock();
    if(kind==="owner")f.state.ownerApproved=false;
    if(kind==="main")f.state.main="a".repeat(40);
    if(kind==="issue")f.state.issueOpen=false;
    if(kind==="draft")f.state.prDraft=false;
    await invoke(1,f);
    assert.deepEqual(f.state.failures,["CAS_BROKER_WRITE_PRECONDITION_CHANGED"],kind);
    assert.deepEqual(f.state.writes,[],kind);
  }
});
test("object corruption fails independent readback; never repairs or silently accepts",async()=>{
  const f=mock();await invoke(1,f);
  f.state.content=Buffer.from("tampered");
  await invoke(2,f);
  assert.deepEqual(f.state.failures,["CAS_INDEPENDENT_READBACK_FAILED"]);
  assert.deepEqual(f.state.writes,["createRef","writeContent"]);
});
test("pre-existing divergent branch blocks any write or PR creation",async()=>{
  const f=mock();f.state.branch="d".repeat(40);
  await invoke(1,f);
  assert.deepEqual(f.state.failures,["CAS_BRANCH_NOT_EMPTY"]);
  assert.deepEqual(f.state.writes,[]);
});
