import assert from "node:assert/strict";
import test from "node:test";

import { attachPullRequests, commitLabel, parseCommits } from "./git-range.mjs";

test("parses commit decorations and includes matching pull request metadata", () => {
    const commits = parseCommits(
        "abcdef0123456789\x1fabcdef0\x1f2026-10-17\x1fAda\x1fAdd range selector\x1fHEAD -> feature/range, origin/feature/range\x1e",
    );
    const enriched = attachPullRequests(commits, [{
        number: 42,
        title: "Add range selector",
        headRefName: "feature/range",
        baseRefName: "main",
        headRefOid: "abcdef0123456789",
        mergeCommit: null,
    }]);

    assert.deepEqual(enriched[0].branches, ["HEAD -> feature/range", "origin/feature/range"]);
    assert.deepEqual(enriched[0].pullRequests, [{
        number: 42,
        title: "Add range selector",
        headRefName: "feature/range",
        baseRefName: "main",
    }]);
    assert.match(commitLabel(enriched[0]), /abcdef0 - Add range selector/);
    assert.match(commitLabel(enriched[0]), /PR #42 \(feature\/range -> main\): Add range selector/);
});
