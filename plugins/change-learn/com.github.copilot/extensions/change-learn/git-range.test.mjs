import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
    attachPullRequests,
    commitLabel,
    parseCommits,
    scaffoldDeckPreview,
} from "./git-range.mjs";

const directory = path.dirname(fileURLToPath(import.meta.url));
const deckScript = path.resolve(directory, "../../../skills/change-learn/scripts/deck.py");

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

test("scaffolds a disposable deck preview from direct analysis", async () => {
    const deck = await scaffoldDeckPreview({
        scriptPath: deckScript,
        title: "Direct canvas preview",
        analysis: {
            spec: { repo: "/example/repository", from: "abc1234", to: "def5678" },
            totals: { files: 1, adds: 3, dels: 1, commits: 1 },
            commits: [{ sha: "def5678", author: "Ada", s: "Add direct preview" }],
            areas: [{ area: "src", files: 1, adds: 3, dels: 1, sample: ["src/app.py"] }],
            files: [{ p: "src/app.py", st: "M", a: 3, d: 1 }],
            untracked: [],
            flags: [],
        },
    });

    assert.match(deck, /^---\nmarp: true/m);
    assert.match(deck, /Direct canvas preview/);
    assert.match(deck, /```mermaid/);
});
