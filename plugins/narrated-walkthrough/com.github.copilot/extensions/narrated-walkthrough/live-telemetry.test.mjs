import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
    collectLiveTelemetry,
    deriveNodeStateAndActions,
    extractPackPrNumber,
    parseGitHubSlug,
    parseSquadRoster,
} from "./live-telemetry.mjs";

test("parseGitHubSlug parses HTTPS and SSH remotes cleanly", () => {
    assert.equal(parseGitHubSlug("https://github.com/matthewcorven/venom-dog-sports.git"), "matthewcorven/venom-dog-sports");
    assert.equal(parseGitHubSlug("git@github.com:matthewcorven/corven-agent-tools.git"), "matthewcorven/corven-agent-tools");
    assert.equal(parseGitHubSlug("not-a-url"), null);
});

test("parseSquadRoster extracts only active AI squad members from .squad/team.md", () => {
    const md = `
# Team
## Members
| Name | Role | Charter | Status |
|------|------|---------|--------|
| Eric | Lead | \`.squad/agents/eric/charter.md\` | ✅ Active |
| Shelly | Frontend Dev | \`.squad/agents/shelly/charter.md\` | ✅ Active |
| Matthew Corven | Engineering Manager | — | 👤 Human |
| Scribe | Session Logger | \`.squad/agents/scribe/charter.md\` | 🔧 Support |
## Coding Agent
`;
    const roster = parseSquadRoster(md);
    assert.equal(roster.length, 2);
    assert.equal(roster[0].id, "eric");
    assert.equal(roster[0].level, "lead");
    assert.equal(roster[1].id, "shelly");
    assert.equal(roster[1].level, "specialist");
});

test("deriveNodeStateAndActions computes data-driven recommendations from live counts", () => {
    // 1. Bottleneck node -> Scale up vs. Update evals + Unblock #<n>
    const eddie = deriveNodeStateAndActions(
        { id: "eddie", name: "Eddie", role: "DevOps", level: "specialist" },
        {
            readyIssues: [
                { number: 346, title: "A" }, { number: 344, title: "B" },
                { number: 337, title: "C" }, { number: 336, title: "D" },
                { number: 335, title: "E" },
            ],
            blockedIssues: [{ number: 299, title: "Blocked infra" }],
            openPrs: [],
            repoNextAvailable: { number: 166, title: "Trial Run Order" },
        },
    );
    assert.equal(eddie.state, "bottleneck");
    assert.equal(eddie.queue_depth, 6);
    assert.equal(eddie.quick_actions[0].label, "Scale up vs. Update evals");
    assert.equal(eddie.quick_actions[0].action, "triage_bottleneck");
    assert.equal(eddie.quick_actions[1].label, "Unblock #299");

    // 2. Idle node with 0 ready -> Assign next item (#166) + Unblock
    const shelly = deriveNodeStateAndActions(
        { id: "shelly", name: "Shelly", role: "Frontend", level: "specialist" },
        {
            readyIssues: [],
            blockedIssues: [{ number: 310, title: "UI gate" }],
            openPrs: [],
            repoNextAvailable: { number: 166, title: "Trial Run Order" },
        },
    );
    assert.equal(shelly.state, "idle");
    assert.equal(shelly.quick_actions[0].label, "Assign next item (#166)");
    assert.equal(shelly.quick_actions[1].label, "Unblock #310");

    // 3. Active healthy node -> Assign next item (#343)
    const albrecht = deriveNodeStateAndActions(
        { id: "albrecht", name: "Albrecht", role: "Backend", level: "specialist" },
        {
            readyIssues: [{ number: 343, title: "API tweak" }],
            blockedIssues: [],
            openPrs: [],
            repoNextAvailable: { number: 166, title: "Trial Run Order" },
        },
    );
    assert.equal(albrecht.state, "active");
    assert.equal(albrecht.quick_actions[0].label, "Assign next item (#343)");
});

test("extractPackPrNumber identifies PR references deterministically", () => {
    assert.equal(extractPackPrNumber("pr-353-review-walkthrough", ""), 353);
    assert.equal(extractPackPrNumber("", "Architect HUD — PR #353"), 353);
    assert.equal(extractPackPrNumber("narrated-morning-brief", "Daily Standup"), null);
});

test("collectLiveTelemetry produces 100% factual metrics and nodes from deterministic command outputs", async () => {
    const tmp = await mkdtemp(path.join(os.tmpdir(), "nw-telemetry-test-"));
    try {
        await mkdir(path.join(tmp, ".squad"), { recursive: true });
        await mkdir(path.join(tmp, "deck"), { recursive: true });
        await writeFile(
            path.join(tmp, ".squad", "team.md"),
            "## Members\n| Name | Role | Charter | Status |\n|---|---|---|---|\n| Eddie | DevOps | c | ✅ Active |\n| Shelly | Frontend | c | ✅ Active |\n",
        );
        await writeFile(
            path.join(tmp, "deck", "timeline.json"),
            JSON.stringify({ title: "PR #353 Review", scenes: [{ id: "s1" }], total_duration: 85.4 }),
        );
        await writeFile(
            path.join(tmp, "deck", "deck.json"),
            JSON.stringify({ scenes: 1, total_duration: 85.4, pace: { target_sps: 6.3, scenes: [{ balanced_sps: 6.3 }] } }),
        );

        const mockRun = async (cmd, args) => {
            if (cmd === "git" && args.includes("--abbrev-ref")) return "develop\n";
            if (cmd === "git" && args.includes("--short")) return "abc1234\n";
            if (cmd === "git" && args.includes("remote.origin.url")) return "https://github.com/matthewcorven/venom-dog-sports.git\n";
            if (cmd === "python3" && args.some((a) => a.endsWith("issue_queue.py"))) {
                return JSON.stringify({
                    available_count: 2,
                    blocked_count: 1,
                    untriaged_count: 0,
                    next_available: { number: 166, title: "Trial Run Order" },
                    available: [{ number: 346, title: "Infra A" }, { number: 166, title: "Trial Run Order" }],
                    blocked: [{ number: 299, title: "Prod KV" }],
                });
            }
            if (cmd === "gh" && args[0] === "issue") {
                return JSON.stringify([
                    { number: 346, title: "Infra A", labels: [{ name: "squad:eddie" }], url: "https://github.com/matthewcorven/venom-dog-sports/issues/346" },
                    { number: 299, title: "Prod KV", labels: [{ name: "squad:shelly" }], url: "https://github.com/matthewcorven/venom-dog-sports/issues/299" },
                ]);
            }
            if (cmd === "gh" && args[0] === "pr" && args[1] === "list") return "[]";
            if (cmd === "gh" && args[0] === "pr" && args[1] === "view") {
                return JSON.stringify({ number: 353, state: "MERGED", mergedAt: "2026-11-22T04:23:01Z", additions: 430, deletions: 1004, changedFiles: 13, url: "https://github.com/matthewcorven/venom-dog-sports/pull/353" });
            }
            if (cmd === "python3" && args.some((a) => a.endsWith("artifacts.py"))) {
                return JSON.stringify({ totalBytes: 76923301, totalFiles: 225, percentUsed: 3.58, maxGb: 2, overLimit: false });
            }
            throw new Error(`unexpected cmd: ${cmd} ${args.join(" ")}`);
        };

        const tel = await collectLiveTelemetry({
            repoPath: tmp,
            deckDir: path.join(tmp, "deck"),
            packMeta: { spec: "pr-353-review-walkthrough", commit: "oldsha1" },
            run: mockRun,
            helpers: { issueQueue: "/mock/issue_queue.py", artifactsScript: "/mock/artifacts.py" },
        });

        assert.equal(tel.ok, true);
        assert.equal(tel.repoSlug, "matthewcorven/venom-dog-sports");
        assert.equal(tel.packFreshness.stale, true);
        assert.equal(tel.packFreshness.packPrState, "MERGED");
        assert.ok(tel.stats.some((s) => s.label === "PR #353 (Live)" && s.value === "MERGED"));
        assert.ok(tel.stats.some((s) => s.label === "Open PRs (Live)" && s.value === "0"));
        const shelly = tel.nodes.find((n) => n.id === "shelly");
        assert.equal(shelly.state, "idle");
        assert.equal(shelly.quick_actions[0].label, "Assign next item (#166)");
    } finally {
        await rm(tmp, { recursive: true, force: true });
    }
});
