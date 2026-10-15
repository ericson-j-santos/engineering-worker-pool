"use strict";
const {validatePostMergeCIContext}=require("./governed_auto_merge.cjs");
if (!validatePostMergeCIContext(process.env)) {
  process.stderr.write("GOVERNED_POSTMERGE_CI_BLOCKED: source_or_sha_invalid\n");
  process.exitCode=2;
} else {
  process.stdout.write(
    "GOVERNED_POSTMERGE_CI_SHA_VERIFIED: sha=" +
    process.env.GITHUB_SHA + " pr=" + process.env.EXPECTED_MERGE_PR + "\n"
  );
}
