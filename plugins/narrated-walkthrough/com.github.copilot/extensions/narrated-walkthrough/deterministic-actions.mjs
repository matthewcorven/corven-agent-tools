// Deterministic, zero-LLM execution handlers for A-HUD data-driven node actions:
//   - triage_bottleneck  ("Scale up vs. Update evals")
//   - unblock_issue      ("Unblock #<N>")
//   - assign_next_item   ("Assign next item (#<N>)")
//
// Each handler runs authoritative CLI/plugin probes live (issue_queue.py, gh),
// applies an explicit, inspectable deterministic rubric, writes an auditable
// receipt, and returns structured result data + an A2UI v1.0 follow-up surface
// WITHOUT spending LLM tokens unless the Architect explicitly chooses to hand
// the verified dossier to an AI agent via `dispatch_verified_dossier`.

import { execFile } from "node:child_process";
import { stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import {
    BOTTLENECK_BLOCKED_THRESHOLD,
    BOTTLENECK_READY_THRESHOLD,
    BOTTLENECK_TOTAL_THRESHOLD,
    parseGitHubSlug,
} from "./live-telemetry.mjs";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PROD_GATE_RE = /\b(production|prod\b|cutover|key vault|service bus|event grid|prod readiness|prod deploy)\b/i;

export const DETERMINISTIC_COMPUTE_ACTIONS = new Set([
    "triage_bottleneck",
    "unblock_issue",
    "assign_next_item",
]);

async function defaultRun(cmd, args, { cwd, timeout = 15000 } = {}) {
    const { stdout } = await execFileAsync(cmd, args, {
        cwd,
        timeout,
        maxBuffer: 8 * 1024 * 1024,
    });
    return stdout;
}

async function firstExisting(candidates) {
    for (const c of candidates) {
        if (!c) continue;
        const st = await stat(c).catch(() => null);
        if (st && st.isFile()) return c;
    }
    return null;
}

async function resolveIssueQueueScript(repoPath, helpersOverride) {
    if (helpersOverride && "issueQueue" in helpersOverride) {
        return helpersOverride.issueQueue;
    }
    const home = os.homedir();
    const pluginsRoot = path.resolve(__dirname, "..", "..", "..", "..");
    return firstExisting([
        path.join(home, ".copilot", "installed-plugins", "corven-agent-tools", "github-issues", "skills", "issue-work-queue", "scripts", "issue_queue.py"),
        path.join(pluginsRoot, "github-issues", "skills", "issue-work-queue", "scripts", "issue_queue.py"),
        repoPath ? path.join(repoPath, ".github", "skills", "issue-work-queue", "scripts", "issue_queue.py") : null,
    ]);
}

async function resolveRepoSlug(repoPath, run) {
    if (!repoPath) return null;
    try {
        const remote = await run("git", ["-C", repoPath, "config", "--get", "remote.origin.url"]);
        return parseGitHubSlug(remote);
    } catch {
        return null;
    }
}

function summarizeBlockerEntry(entry) {
    const resolved = entry?.resolved || {};
    const num = Number(resolved.number || entry?.reference || 0) || null;
    const labels = Array.isArray(resolved.labels)
        ? resolved.labels.map((l) => (typeof l === "string" ? l : l?.name)).filter(Boolean)
        : [];
    const owner = labels.find((l) => l.startsWith("squad:"))?.slice(6) || null;
    return {
        number: num,
        reference: String(entry?.reference || num || ""),
        state: resolved.state || "UNKNOWN",
        title: resolved.title || "(unresolved reference)",
        owner,
        url: resolved.url || null,
    };
}

/**
 * Pure deterministic rubric for `triage_bottleneck` ("Scale up vs. Update evals").
 */
export function evaluateBottleneckRubric({ nodeId, available = [], blocked = [], allBlocked = [] }) {
    const prodGated = [];
    const actionableNow = [];
    for (const item of available) {
        if (PROD_GATE_RE.test(item.title || "")) prodGated.push({ number: item.number, title: item.title });
        else actionableNow.push({ number: item.number, title: item.title });
    }

    // Identify ready items on this node that directly unblock other open issues across the repo
    const readyNumSet = new Set(available.map((i) => i.number));
    const unblocksMap = new Map();
    for (const b of (allBlocked.length ? allBlocked : blocked)) {
        const refs = [...(b.unresolved_blockers || []), ...(b.unresolved_dependencies || [])]
            .map((r) => Number(r?.resolved?.number || r?.reference))
            .filter((n) => Number.isFinite(n) && readyNumSet.has(n));
        for (const leaf of refs) {
            if (!unblocksMap.has(leaf)) unblocksMap.set(leaf, []);
            unblocksMap.get(leaf).push(b.number);
        }
    }
    const rootUnblockerLeaves = [...unblocksMap.entries()]
        .map(([num, unlocks]) => {
            const match = available.find((i) => i.number === num);
            return { number: num, title: match?.title || "", unlocks: [...new Set(unlocks)].sort((a, b) => a - b) };
        })
        .sort((a, b) => b.unlocks.length - a.unlocks.length || a.number - b.number);

    const readyCount = available.length;
    const blockedCount = blocked.length;
    const prodRatio = readyCount > 0 ? Math.round((prodGated.length / readyCount) * 100) : 0;

    let recommendation;
    let formulaMatched;
    let rationale;
    if (prodGated.length > 0 && prodRatio >= 40) {
        recommendation = "UPDATE_EVALS_SPLIT_PROD_GATE";
        formulaMatched = `prodGatedRatio (${prodRatio}%) >= 40% (${prodGated.length}/${readyCount} ready are prod-gated)`;
        rationale = `Do NOT reflexively scale workers: ${prodGated.length} of ${readyCount} ready items are production-gated. Update queue/eval segmentation to separate prod-gated work from active throughput, then clear the ${actionableNow.length} actionable item(s).`;
    } else if (rootUnblockerLeaves.length > 0 && blockedCount >= 1) {
        recommendation = "CLEAR_CRITICAL_PATH_LEAVES_THEN_SCALE";
        formulaMatched = `actionableNow (${actionableNow.length}) >= 3 && rootUnblockerLeaves (${rootUnblockerLeaves.length}) > 0`;
        const top = rootUnblockerLeaves[0];
        rationale = `Real actionable load is ${actionableNow.length} (${prodGated.length} prod-gated, ${blockedCount} blocked). Prioritize root unblocker #${top.number} (directly unlocks ${top.unlocks.map((n) => `#${n}`).join(", ")}) before scaling parallel workers or expanding eval matrices.`;
    } else if (actionableNow.length >= BOTTLENECK_READY_THRESHOLD) {
        recommendation = "SCALE_UP_REFLEXIVELY";
        formulaMatched = `actionableNow (${actionableNow.length}) >= ${BOTTLENECK_READY_THRESHOLD} && prodGatedRatio (${prodRatio}%) < 40%`;
        rationale = `Genuine non-gated workload pressure (${actionableNow.length} actionable items). Reflexively adding a parallel worker or splitting batches is justified.`;
    } else {
        recommendation = "MAINTAIN_AND_REBENCHMARK";
        formulaMatched = `actionableNow (${actionableNow.length}) < ${BOTTLENECK_READY_THRESHOLD} && blocked (${blockedCount}) < ${BOTTLENECK_BLOCKED_THRESHOLD}`;
        rationale = `Actionable queue (${actionableNow.length}) is within normal single-agent capacity once gated/blocked items are separated.`;
    }

    return {
        nodeId,
        thresholds: {
            readyBottleneck: BOTTLENECK_READY_THRESHOLD,
            blockedBottleneck: BOTTLENECK_BLOCKED_THRESHOLD,
            totalBottleneck: BOTTLENECK_TOTAL_THRESHOLD,
            prodGateSplitPercent: 40,
        },
        counts: {
            ready: readyCount,
            actionableNow: actionableNow.length,
            prodGated: prodGated.length,
            prodGatedPercent: prodRatio,
            blocked: blockedCount,
        },
        actionableNow,
        prodGated,
        blocked: blocked.map((b) => ({
            number: b.number,
            title: b.title,
            blockers: [...(b.unresolved_blockers || []), ...(b.unresolved_dependencies || [])]
                .map((r) => Number(r?.resolved?.number || r?.reference))
                .filter(Number.isFinite),
        })),
        rootUnblockerLeaves,
        recommendation,
        formulaMatched,
        rationale,
    };
}

/**
 * Pure deterministic blocker-graph resolver for `unblock_issue`.
 */
export function evaluateUnblockGraph({ issueNumber, repoQueue }) {
    const num = Number(issueNumber);
    const available = repoQueue?.available || [];
    const blocked = repoQueue?.blocked || [];
    const availSet = new Set(available.map((i) => i.number));
    const blockedByNum = new Map(blocked.map((i) => [i.number, i]));

    const targetBlocked = blockedByNum.get(num);
    if (!targetBlocked) {
        const targetAvail = available.find((i) => i.number === num);
        return {
            issueNumber: num,
            currentlyBlocked: false,
            status: targetAvail ? "ALREADY_READY" : "NOT_IN_OPEN_QUEUE",
            title: targetAvail?.title || `Issue #${num}`,
            directBlockers: [],
            actionableRootLeaves: targetAvail ? [{ number: num, title: targetAvail.title, reason: "Issue itself is ready now" }] : [],
            formulaMatched: targetAvail ? "issue in issue_queue.available (0 unresolved blockers)" : "issue absent from open queue",
        };
    }

    const rawRefs = [
        ...(targetBlocked.unresolved_blockers || []),
        ...(targetBlocked.unresolved_dependencies || []),
    ];
    const seen = new Set();
    const directBlockers = [];
    for (const ref of rawRefs) {
        const s = summarizeBlockerEntry(ref);
        if (s.number && seen.has(s.number)) continue;
        if (s.number) seen.add(s.number);
        const blockerNode = s.number ? blockedByNum.get(s.number) : null;
        const isReadyLeaf = Boolean(s.number && availSet.has(s.number));
        const nestedBlockers = blockerNode
            ? [...(blockerNode.unresolved_blockers || []), ...(blockerNode.unresolved_dependencies || [])]
                .map((r) => Number(r?.resolved?.number || r?.reference))
                .filter(Number.isFinite)
            : [];
        directBlockers.push({
            ...s,
            readyNow: isReadyLeaf,
            blockedBy: nestedBlockers,
        });
    }

    // BFS to find every ready leaf that transitively unblocks `num`
    const leafMap = new Map();
    const visited = new Set([num]);
    const stack = directBlockers.map((b) => b.number).filter(Number.isFinite);
    while (stack.length) {
        const cur = stack.pop();
        if (visited.has(cur)) continue;
        visited.add(cur);
        if (availSet.has(cur)) {
            const match = available.find((i) => i.number === cur);
            leafMap.set(cur, { number: cur, title: match?.title || `Issue #${cur}`, state: "READY_NOW" });
            continue;
        }
        const bNode = blockedByNum.get(cur);
        if (bNode) {
            for (const r of [...(bNode.unresolved_blockers || []), ...(bNode.unresolved_dependencies || [])]) {
                const n = Number(r?.resolved?.number || r?.reference);
                if (Number.isFinite(n) && !visited.has(n)) stack.push(n);
            }
        }
    }

    const actionableRootLeaves = [...leafMap.values()].sort((a, b) => a.number - b.number);
    return {
        issueNumber: num,
        currentlyBlocked: true,
        status: "BLOCKED",
        title: targetBlocked.title,
        directBlockers,
        actionableRootLeaves,
        formulaMatched: `issue #${num} has ${directBlockers.length} unresolved blocker(s); transitive DAG yields ${actionableRootLeaves.length} ready root leaf(s)`,
    };
}

/**
 * Execute a deterministic, zero-LLM action handler against live workspace/GitHub data.
 */
export async function executeDeterministicAction(normalized, { repoPath, run = defaultRun, helpers } = {}) {
    const actionName = normalized.action.name;
    const ctx = normalized.action.context || {};
    const measuredAt = new Date().toISOString();
    const repoSlug = await resolveRepoSlug(repoPath, run);
    const issueQueue = await resolveIssueQueueScript(repoPath, helpers);

    if (!repoSlug || !issueQueue) {
        return {
            ok: false,
            error: `Deterministic handler for '${actionName}' requires a git GitHub remote and issue_queue.py.`,
        };
    }

    if (actionName === "triage_bottleneck") {
        const nodeId = String(ctx.nodeId || normalized.node?.id || "").toLowerCase();
        const [memberOut, repoOut] = await Promise.all([
            run("python3", [issueQueue, "--repo", repoSlug, "--label", `squad:${nodeId}`, "--mode", "status", "--format", "json"], { cwd: repoPath }),
            run("python3", [issueQueue, "--repo", repoSlug, "--mode", "status", "--format", "json"], { cwd: repoPath }),
        ]);
        const memberIq = JSON.parse(memberOut);
        const repoIq = JSON.parse(repoOut);
        const evalResult = evaluateBottleneckRubric({
            nodeId,
            available: memberIq.available || [],
            blocked: memberIq.blocked || [],
            allBlocked: repoIq.blocked || [],
        });

        const topLeaf = evalResult.rootUnblockerLeaves[0];
        const summary = `${nodeId}: ${evalResult.recommendation} — ${evalResult.rationale}`;
        return {
            ok: true,
            kind: "triage_bottleneck",
            measuredAt,
            repoSlug,
            sources: ["issue-work-queue (member)", "issue-work-queue (repo DAG)"],
            llmTokensUsed: 0,
            title: `Deterministic Bottleneck Analysis — ${normalized.node?.name || nodeId}`,
            summary,
            data: evalResult,
            handoffPrompt: `Act on this deterministic bottleneck analysis for ${normalized.node?.name || nodeId}: recommendation=${evalResult.recommendation} (${evalResult.formulaMatched})${topLeaf ? `; top root unblocker=#${topLeaf.number}` : ""}.`,
        };
    }

    if (actionName === "unblock_issue") {
        const issueNumber = Number(ctx.issue || ctx.issueNumber);
        if (!Number.isFinite(issueNumber) || issueNumber <= 0) {
            return { ok: false, error: "unblock_issue requires a positive numeric context.issue." };
        }
        const repoOut = await run("python3", [issueQueue, "--repo", repoSlug, "--mode", "status", "--format", "json"], { cwd: repoPath });
        const repoIq = JSON.parse(repoOut);
        const graph = evaluateUnblockGraph({ issueNumber, repoQueue: repoIq });

        const leafList = graph.actionableRootLeaves.map((l) => `#${l.number}`).join(", ") || "none";
        const summary = graph.currentlyBlocked
            ? `#${issueNumber} is blocked by ${graph.directBlockers.map((b) => `#${b.number} (${b.readyNow ? "READY" : "BLOCKED"})`).join(", ")}. Actionable root unblocker leaf(s): ${leafList}.`
            : `#${issueNumber} is currently ${graph.status} (0 unresolved blockers).`;

        return {
            ok: true,
            kind: "unblock_issue",
            measuredAt,
            repoSlug,
            sources: ["issue-work-queue (live dependency DAG)"],
            llmTokensUsed: 0,
            title: `Deterministic Blocker DAG — Issue #${issueNumber}`,
            summary,
            data: graph,
            handoffPrompt: `Unblock #${issueNumber} (${graph.title}) using its verified live blocker DAG: start with actionable root leaf(s) ${leafList}.`,
        };
    }

    if (actionName === "assign_next_item") {
        const issueNumber = Number(ctx.issue || ctx.issueNumber);
        const nodeId = String(ctx.nodeId || normalized.node?.id || "").toLowerCase();
        const [repoOut, prsOut] = await Promise.all([
            run("python3", [issueQueue, "--repo", repoSlug, "--mode", "status", "--format", "json"], { cwd: repoPath }),
            run("gh", ["pr", "list", "-R", repoSlug, "--state", "open", "--json", "number,title,headRefName,url"], { cwd: repoPath }).catch(() => "[]"),
        ]);
        const repoIq = JSON.parse(repoOut);
        const openPrs = JSON.parse(prsOut);
        const target = Number.isFinite(issueNumber) && issueNumber > 0
            ? (repoIq.available || []).find((i) => i.number === issueNumber)
            : repoIq.next_available;
        const conflictingPrs = target
            ? openPrs.filter((p) => new RegExp(`\\b${target.number}\\b`).test(`${p.title || ""} ${p.headRefName || ""}`))
            : [];

        const verifiedReady = Boolean(target && conflictingPrs.length === 0);
        const summary = target
            ? (verifiedReady
                ? `#${target.number} (${target.title}) is VERIFIED READY: OPEN, 0 unresolved blockers, 0 conflicting open PRs — safe to assign to ${normalized.node?.name || nodeId}.`
                : `#${target.number} has ${conflictingPrs.length} conflicting open PR(s): ${conflictingPrs.map((p) => `#${p.number}`).join(", ")}.`)
            : `Issue #${issueNumber} is NOT currently in the ready queue (may be blocked or closed).`;

        return {
            ok: true,
            kind: "assign_next_item",
            measuredAt,
            repoSlug,
            sources: ["issue-work-queue", "gh-prs"],
            llmTokensUsed: 0,
            title: `Deterministic Readiness Gate — ${target ? `#${target.number}` : "No Ready Item"}`,
            summary,
            data: {
                nodeId,
                issueNumber: target?.number || issueNumber || null,
                title: target?.title || null,
                verifiedReady,
                conflictingPrs,
                formulaMatched: verifiedReady
                    ? "issue in issue_queue.available && conflictingOpenPrs === 0"
                    : "issue not ready or conflicting open PR detected",
            },
            handoffPrompt: target
                ? `Assign verified-ready issue #${target.number} (${target.title}) to ${normalized.node?.name || nodeId} and begin execution.`
                : `Re-evaluate queue assignment for ${normalized.node?.name || nodeId}; requested item is not ready.`,
        };
    }

    return { ok: false, error: `No deterministic handler registered for '${actionName}'.` };
}
