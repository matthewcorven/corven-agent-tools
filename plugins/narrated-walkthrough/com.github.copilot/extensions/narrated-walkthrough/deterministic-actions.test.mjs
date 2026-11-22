import assert from "node:assert/strict";
import test from "node:test";

import {
    evaluateBottleneckRubric,
    evaluateUnblockGraph,
    executeDeterministicAction,
} from "./deterministic-actions.mjs";

test("evaluateBottleneckRubric recommends CLEAR_CRITICAL_PATH_LEAVES_THEN_SCALE for Eric's exact queue shape", () => {
    const res = evaluateBottleneckRubric({
        nodeId: "eric",
        available: [
            { number: 166, title: "Trial Run Order Sheets" },
            { number: 226, title: "Event Grid email delivery reporting is disabled in the production environment" },
            { number: 229, title: "[WPP-2] Mandate true captured evidence" },
            { number: 231, title: "[WPP-4] Explore deterministic instruction blocks" },
            { number: 331, title: "Every existing Bicep reference is unverified" },
            { number: 342, title: "gitleaks: add concurrency with cancel-in-progress" },
        ],
        blocked: [
            {
                number: 228,
                title: "[WPP-1] Document deterministic SDLC",
                unresolved_blockers: [{ reference: "229" }, { reference: "234" }],
            },
        ],
        allBlocked: [
            {
                number: 228,
                title: "[WPP-1] Document deterministic SDLC",
                unresolved_blockers: [{ reference: "229" }, { reference: "234" }],
            },
            {
                number: 234,
                title: "[WPP-7] Establish GitHub issue hygiene",
                unresolved_blockers: [{ reference: "229" }],
            },
        ],
    });

    assert.equal(res.counts.ready, 6);
    assert.equal(res.counts.prodGated, 1);
    assert.equal(res.counts.actionableNow, 5);
    assert.equal(res.recommendation, "CLEAR_CRITICAL_PATH_LEAVES_THEN_SCALE");
    assert.equal(res.rootUnblockerLeaves[0].number, 229);
    assert.deepEqual(res.rootUnblockerLeaves[0].unlocks, [228, 234]);
});

test("evaluateBottleneckRubric recommends UPDATE_EVALS_SPLIT_PROD_GATE when >=40% of queue is prod-gated", () => {
    const res = evaluateBottleneckRubric({
        nodeId: "eddie",
        available: [
            { number: 226, title: "Event Grid production environment" },
            { number: 249, title: "Production Service Bus enablement" },
            { number: 260, title: "Production deploy verification" },
            { number: 346, title: "Pin go-sqlcmd sha256" },
        ],
        blocked: [],
    });
    assert.equal(res.counts.prodGated, 3);
    assert.equal(res.counts.prodGatedPercent, 75);
    assert.equal(res.recommendation, "UPDATE_EVALS_SPLIT_PROD_GATE");
});

test("evaluateUnblockGraph resolves transitive DAG to actionable root leaves", () => {
    const repoQueue = {
        available: [
            { number: 229, title: "[WPP-2] Mandate true captured evidence" },
        ],
        blocked: [
            {
                number: 228,
                title: "[WPP-1] Document deterministic SDLC",
                unresolved_blockers: [
                    { reference: "229", resolved: { number: 229, state: "OPEN", title: "[WPP-2] Mandate true captured evidence" } },
                    { reference: "234", resolved: { number: 234, state: "OPEN", title: "[WPP-7] Issue hygiene" } },
                ],
                unresolved_dependencies: [],
            },
            {
                number: 234,
                title: "[WPP-7] Issue hygiene",
                unresolved_blockers: [
                    { reference: "229", resolved: { number: 229, state: "OPEN", title: "[WPP-2] Mandate true captured evidence" } },
                ],
                unresolved_dependencies: [],
            },
        ],
    };

    const g = evaluateUnblockGraph({ issueNumber: 228, repoQueue });
    assert.equal(g.currentlyBlocked, true);
    assert.equal(g.directBlockers.length, 2);
    assert.equal(g.directBlockers[0].readyNow, true);
    assert.equal(g.directBlockers[1].readyNow, false);
    assert.deepEqual(g.directBlockers[1].blockedBy, [229]);
    assert.deepEqual(g.actionableRootLeaves.map((l) => l.number), [229]);
});

test("executeDeterministicAction runs unblock_issue with 0 LLM tokens and returns live DAG receipt", async () => {
    const mockRun = async (cmd, args) => {
        if (cmd === "git") return "https://github.com/example/repo.git\n";
        if (cmd === "python3") {
            return JSON.stringify({
                available: [{ number: 229, title: "WPP-2 Evidence" }],
                blocked: [{
                    number: 228,
                    title: "WPP-1 SDLC",
                    unresolved_blockers: [{ reference: "229", resolved: { number: 229, state: "OPEN", title: "WPP-2 Evidence" } }],
                    unresolved_dependencies: [],
                }],
            });
        }
        throw new Error(`unexpected ${cmd}`);
    };

    const res = await executeDeterministicAction(
        { action: { name: "unblock_issue", context: { issue: 228, nodeId: "eric" } } },
        { repoPath: "/tmp/mock", run: mockRun, helpers: { issueQueue: "/mock/issue_queue.py" } },
    );
    assert.equal(res.ok, true);
    assert.equal(res.llmTokensUsed, 0);
    assert.equal(res.data.actionableRootLeaves[0].number, 229);
    assert.match(res.summary, /#228/);
});
