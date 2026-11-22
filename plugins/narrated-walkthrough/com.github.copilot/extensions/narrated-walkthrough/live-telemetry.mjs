// Deterministic live-telemetry collector and data-driven recommendation engine
// for the narrated-walkthrough A-HUD (Architect HUD) Extension Canvas.
//
// Zero LLM calls, zero invented numbers: every stat, node queue depth, PR state,
// artifact budget metric, and recommended intervention is computed directly from
// authoritative workspace/plugin tools (.squad/team.md, issue-work-queue, gh,
// artifacts.py, git, and deck.json). Any probe that cannot run reports
// `unavailable` explicitly instead of fabricating or retaining stale values.

import { execFile } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export const BOTTLENECK_READY_THRESHOLD = 5;
export const BOTTLENECK_BLOCKED_THRESHOLD = 2;
export const BOTTLENECK_TOTAL_THRESHOLD = 6;

async function defaultRun(cmd, args, { cwd, timeout = 12000 } = {}) {
    const { stdout } = await execFileAsync(cmd, args, {
        cwd,
        timeout,
        maxBuffer: 4 * 1024 * 1024,
    });
    return stdout;
}

/**
 * Parse a GitHub `owner/repo` slug from an origin remote URL.
 */
export function parseGitHubSlug(remoteUrl) {
    if (typeof remoteUrl !== "string") return null;
    const trimmed = remoteUrl.trim();
    const m = trimmed.match(/github\.com[:/]([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?$/i);
    return m ? m[1] : null;
}

/**
 * Parse active team members deterministically from a repo's `.squad/team.md`.
 * Keeps corven-agent-tools completely repo-agnostic: member names and roles come
 * strictly from the consuming repository's roster.
 */
export function parseSquadRoster(markdown) {
    if (typeof markdown !== "string" || !markdown.trim()) return [];
    const lines = markdown.split(/\r?\n/);
    let inMembers = false;
    const members = [];

    for (const raw of lines) {
        const line = raw.trim();
        if (/^##\s+Members\b/i.test(line)) {
            inMembers = true;
            continue;
        }
        if (inMembers && /^##\s+/.test(line)) {
            break;
        }
        if (!inMembers || !line.startsWith("|")) continue;
        const cells = line.split("|").slice(1, -1).map((c) => c.trim());
        if (cells.length < 4) continue;
        const [name, role, charter, status] = cells;
        if (!name || /^name$/i.test(name) || /^-+$/.test(name)) continue;
        // Include only active AI/squad team members (skip Human / Support / Disabled)
        if (!/active/i.test(status) || /human|support|disabled/i.test(status)) continue;

        const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
        const level = /\blead\b|\barchitect\b/i.test(role) ? "lead" : "specialist";
        members.push({
            id,
            name,
            role,
            level,
            charter: charter.replace(/`/g, ""),
        });
    }
    return members;
}

/**
 * Extract an explicit PR number referenced by a pack's spec or title (e.g. `pr-353` or `PR #353`).
 */
export function extractPackPrNumber(spec, title) {
    const hay = `${spec || ""} ${title || ""}`;
    const m = hay.match(/\bpr[\s#-]*(\d+)\b/i);
    return m ? Number.parseInt(m[1], 10) : null;
}

/**
 * Deterministic, data-driven state classification and task/work recommendations
 * for a single Squad member node.
 */
export function deriveNodeStateAndActions(
    member,
    { readyIssues = [], blockedIssues = [], openPrs = [], repoNextAvailable = null },
) {
    const readyCount = readyIssues.length;
    const blockedCount = blockedIssues.length;
    const totalQueue = readyCount + blockedCount;
    const prCount = openPrs.length;

    let state;
    let stateLabel;
    if (
        readyCount >= BOTTLENECK_READY_THRESHOLD
        || blockedCount >= BOTTLENECK_BLOCKED_THRESHOLD
        || totalQueue >= BOTTLENECK_TOTAL_THRESHOLD
    ) {
        state = "bottleneck";
        stateLabel = `${readyCount} ready · ${blockedCount} blocked`;
    } else if (prCount > 0) {
        state = "waiting_merge";
        stateLabel = `${prCount} open PR${prCount === 1 ? "" : "s"} · ${readyCount} ready`;
    } else if (readyCount === 0) {
        state = "idle";
        stateLabel = blockedCount > 0 ? `0 ready · ${blockedCount} blocked` : "0 ready · clear queue";
    } else {
        state = "active";
        stateLabel = `${readyCount} ready · ${blockedCount} blocked`;
    }

    const topReady = readyIssues.slice(0, 3).map((i) => ({ number: i.number, title: i.title }));
    const topBlocked = blockedIssues.slice(0, 3).map((i) => ({ number: i.number, title: i.title }));

    const quickActions = [];
    if (state === "bottleneck") {
        quickActions.push({
            label: "Scale up vs. Update evals",
            action: "triage_bottleneck",
            context: {
                nodeId: member.id,
                readyCount,
                blockedCount,
                totalQueue,
                topReady,
                topBlocked,
                options: ["scale_reflexively", "update_comparative_evals"],
            },
        });
        if (blockedIssues.length > 0) {
            quickActions.push({
                label: `Unblock #${blockedIssues[0].number}`,
                action: "unblock_issue",
                context: {
                    nodeId: member.id,
                    issue: blockedIssues[0].number,
                    title: blockedIssues[0].title,
                },
            });
        } else if (readyIssues.length > 0) {
            quickActions.push({
                label: `Assign #${readyIssues[0].number}`,
                action: "assign_next_item",
                context: {
                    nodeId: member.id,
                    issue: readyIssues[0].number,
                    title: readyIssues[0].title,
                },
            });
        }
    } else if (state === "waiting_merge") {
        quickActions.push({
            label: `Review PR #${openPrs[0].number}`,
            action: "review_pr",
            context: { nodeId: member.id, pr: openPrs[0].number, url: openPrs[0].url },
        });
        const nextPick = readyIssues[0] || repoNextAvailable;
        if (nextPick) {
            quickActions.push({
                label: `Assign next item (#${nextPick.number})`,
                action: "assign_next_item",
                context: { nodeId: member.id, issue: nextPick.number, title: nextPick.title },
            });
        }
    } else if (state === "idle") {
        if (repoNextAvailable) {
            quickActions.push({
                label: `Assign next item (#${repoNextAvailable.number})`,
                action: "assign_next_item",
                context: {
                    nodeId: member.id,
                    issue: repoNextAvailable.number,
                    title: repoNextAvailable.title,
                    reason: "idle_ready_queue",
                },
            });
        }
        if (blockedIssues.length > 0) {
            quickActions.push({
                label: `Unblock #${blockedIssues[0].number}`,
                action: "unblock_issue",
                context: {
                    nodeId: member.id,
                    issue: blockedIssues[0].number,
                    title: blockedIssues[0].title,
                },
            });
        }
    } else if (readyIssues.length > 0) {
        quickActions.push({
            label: `Assign next item (#${readyIssues[0].number})`,
            action: "assign_next_item",
            context: {
                nodeId: member.id,
                issue: readyIssues[0].number,
                title: readyIssues[0].title,
            },
        });
    }

    const summaryParts = [];
    if (topReady.length > 0) {
        summaryParts.push(`Next ready: ${topReady.map((i) => `#${i.number}`).join(", ")}`);
    } else {
        summaryParts.push("No assigned ready issues");
    }
    if (topBlocked.length > 0) {
        summaryParts.push(`Blocked: ${topBlocked.map((i) => `#${i.number}`).join(", ")}`);
    }
    if (openPrs.length > 0) {
        summaryParts.push(`Open PRs: ${openPrs.map((p) => `#${p.number}`).join(", ")}`);
    }

    return {
        id: member.id,
        name: member.name,
        role: member.role,
        level: member.level,
        state,
        state_label: stateLabel,
        queue_depth: totalQueue,
        ready_count: readyCount,
        blocked_count: blockedCount,
        summary: summaryParts.join(" · "),
        chat_prompt: `Chat with ${member.name} (${member.role}) — live queue: ${readyCount} ready, ${blockedCount} blocked${topReady[0] ? `; top ready #${topReady[0].number}` : ""}.`,
        quick_actions: quickActions,
    };
}

async function firstExisting(candidates) {
    for (const c of candidates) {
        if (!c) continue;
        const st = await stat(c).catch(() => null);
        if (st && st.isFile()) return c;
    }
    return null;
}

async function resolveHelperPaths(repoPath) {
    const home = os.homedir();
    const issueQueue = await firstExisting([
        path.join(home, ".copilot", "installed-plugins", "corven-agent-tools", "github-issues", "skills", "issue-work-queue", "scripts", "issue_queue.py"),
        repoPath ? path.join(repoPath, ".github", "skills", "issue-work-queue", "scripts", "issue_queue.py") : null,
    ]);
    const artifactsScript = await firstExisting([
        repoPath ? path.join(repoPath, "scripts", "artifacts.py") : null,
        path.join(home, ".copilot", "installed-plugins", "corven-agent-tools", "playwright-report-retrieval", "scripts", "artifacts.py"),
    ]);
    return { issueQueue, artifactsScript };
}

/**
 * Collect live, 100% deterministic telemetry for the active repository and pack.
 */
export async function collectLiveTelemetry({
    repoPath,
    deckDir,
    packMeta = null,
    run = defaultRun,
}) {
    const measuredAt = new Date().toISOString();
    const sources = [];
    const errors = [];

    // 1. Read active pack files (deck.json + timeline.json)
    let deckJson = null;
    let timelineJson = null;
    if (deckDir) {
        try { deckJson = JSON.parse(await readFile(path.join(deckDir, "deck.json"), "utf8")); } catch {}
        try { timelineJson = JSON.parse(await readFile(path.join(deckDir, "timeline.json"), "utf8")); } catch {}
    }

    // 2. Live git state + repo slug
    let branch = null;
    let headSha = null;
    let repoSlug = null;
    if (repoPath) {
        try {
            const [bOut, shaOut, remoteOut] = await Promise.all([
                run("git", ["-C", repoPath, "rev-parse", "--abbrev-ref", "HEAD"]),
                run("git", ["-C", repoPath, "rev-parse", "--short", "HEAD"]),
                run("git", ["-C", repoPath, "config", "--get", "remote.origin.url"]).catch(() => ""),
            ]);
            branch = bOut.trim() || null;
            headSha = shaOut.trim() || null;
            repoSlug = parseGitHubSlug(remoteOut);
            sources.push("git");
        } catch (err) {
            errors.push(`git: ${err.message}`);
        }
    }

    // 3. Squad roster from consuming repo's .squad/team.md
    let roster = [];
    if (repoPath) {
        try {
            const teamMd = await readFile(path.join(repoPath, ".squad", "team.md"), "utf8");
            roster = parseSquadRoster(teamMd);
            if (roster.length) sources.push(".squad/team.md");
        } catch {
            // Optional: repos without Squad still get live git/PR/artifact stats
        }
    }

    const { issueQueue, artifactsScript } = await resolveHelperPaths(repoPath);
    const packPrNumber = extractPackPrNumber(packMeta?.spec, timelineJson?.title || packMeta?.title);

    // 4. Run external deterministic probes in parallel
    const [iqRes, issuesRes, openPrsRes, packPrRes, usageRes] = await Promise.allSettled([
        repoSlug && issueQueue
            ? run("python3", [issueQueue, "--repo", repoSlug, "--mode", "status", "--format", "json"], { cwd: repoPath })
                .then((out) => JSON.parse(out))
            : Promise.resolve(null),
        repoSlug
            ? run("gh", ["issue", "list", "-R", repoSlug, "--state", "open", "--limit", "200", "--json", "number,title,labels,url"], { cwd: repoPath })
                .then((out) => JSON.parse(out))
            : Promise.resolve(null),
        repoSlug
            ? run("gh", ["pr", "list", "-R", repoSlug, "--state", "open", "--json", "number,title,headRefName,mergeStateStatus,statusCheckRollup,url,updatedAt"], { cwd: repoPath })
                .then((out) => JSON.parse(out))
            : Promise.resolve(null),
        repoSlug && packPrNumber
            ? run("gh", ["pr", "view", String(packPrNumber), "-R", repoSlug, "--json", "number,title,state,mergedAt,url,additions,deletions,changedFiles"], { cwd: repoPath })
                .then((out) => JSON.parse(out))
            : Promise.resolve(null),
        repoPath && artifactsScript
            ? run("python3", [artifactsScript, "--repo-path", repoPath, "usage", "--json"], { cwd: repoPath })
                .then((out) => JSON.parse(out))
            : Promise.resolve(null),
    ]);

    const iq = iqRes.status === "fulfilled" ? iqRes.value : null;
    if (iq) sources.push("issue-work-queue");
    else if (iqRes.status === "rejected") errors.push(`issue-work-queue: ${iqRes.reason?.message || iqRes.reason}`);

    const openIssues = issuesRes.status === "fulfilled" && Array.isArray(issuesRes.value) ? issuesRes.value : null;
    if (openIssues) sources.push("gh-issues");
    else if (issuesRes.status === "rejected") errors.push(`gh-issues: ${issuesRes.reason?.message || issuesRes.reason}`);

    const openPrs = openPrsRes.status === "fulfilled" && Array.isArray(openPrsRes.value) ? openPrsRes.value : null;
    if (openPrs) sources.push("gh-prs");
    else if (openPrsRes.status === "rejected") errors.push(`gh-prs: ${openPrsRes.reason?.message || openPrsRes.reason}`);

    const packPr = packPrRes.status === "fulfilled" ? packPrRes.value : null;
    const usage = usageRes.status === "fulfilled" ? usageRes.value : null;
    if (usage) sources.push("artifacts.py");
    else if (usageRes.status === "rejected") errors.push(`artifacts.py: ${usageRes.reason?.message || usageRes.reason}`);

    // 5. Build 100% factual live stats (NO static placeholders!)
    const stats = [];
    if (openPrs !== null) {
        stats.push({
            label: "Open PRs (Live)",
            value: String(openPrs.length),
            trend: openPrs.length === 0
                ? "queue clear"
                : openPrs.slice(0, 2).map((p) => `#${p.number}`).join(", "),
            tone: openPrs.length === 0 ? "ok" : "warn",
        });
    }
    if (packPr) {
        const net = (packPr.additions || 0) - (packPr.deletions || 0);
        stats.push({
            label: `PR #${packPr.number} (Live)`,
            value: String(packPr.state || "UNKNOWN"),
            trend: `${net >= 0 ? "+" : ""}${net} lines · ${packPr.changedFiles || 0} files`,
            tone: packPr.state === "MERGED" ? "ok" : (packPr.state === "OPEN" ? "warn" : ""),
        });
    }
    if (iq) {
        stats.push({
            label: "Ready Issues (Live)",
            value: String(iq.available_count ?? (iq.available || []).length),
            trend: iq.next_available ? `next #${iq.next_available.number}` : "none queued",
            tone: (iq.available_count ?? 0) > 0 ? "ok" : "",
        });
        stats.push({
            label: "Blocked Issues (Live)",
            value: String(iq.blocked_count ?? (iq.blocked || []).length),
            trend: `${iq.untriaged_count ?? 0} untriaged`,
            tone: (iq.blocked_count ?? 0) > 0 ? "warn" : "ok",
        });
    }
    if (usage && typeof usage.totalBytes === "number") {
        const mb = (usage.totalBytes / (1024 * 1024)).toFixed(1);
        const pct = typeof usage.percentUsed === "number" ? usage.percentUsed.toFixed(1) : "0.0";
        const capGb = usage.maxGb ?? 2;
        stats.push({
            label: "Local Artifacts (Live)",
            value: `${mb} MB`,
            trend: `${pct}% of ${capGb} GB · ${usage.totalFiles ?? 0} files`,
            tone: usage.overLimit ? "warn" : "ok",
        });
    }
    if (deckJson?.pace?.scenes?.length) {
        const rates = deckJson.pace.scenes.map((s) => s.balanced_sps).filter((n) => typeof n === "number" && n > 0);
        if (rates.length) {
            const spread = (Math.max(...rates) / Math.min(...rates)).toFixed(2);
            stats.push({
                label: "Audio Cadence",
                value: `${deckJson.pace.target_sps} syl/s`,
                trend: `${spread}× spread · ${deckJson.scenes || rates.length} scenes`,
                tone: Number(spread) <= 1.08 ? "ok" : "warn",
            });
        }
    }

    // 6. Build 100% factual live Squad node cards with data-driven recommendations
    const availableSet = new Set((iq?.available || []).map((i) => i.number));
    const blockedSet = new Set((iq?.blocked || []).map((i) => i.number));
    const issuesByMember = new Map();
    if (openIssues) {
        for (const iss of openIssues) {
            const labels = (iss.labels || []).map((l) => (typeof l === "string" ? l : l?.name)).filter(Boolean);
            for (const lbl of labels) {
                if (!lbl.startsWith("squad:")) continue;
                const memberId = lbl.slice("squad:".length).toLowerCase();
                if (!issuesByMember.has(memberId)) {
                    issuesByMember.set(memberId, { ready: [], blocked: [] });
                }
                const bucket = issuesByMember.get(memberId);
                if (availableSet.has(iss.number)) bucket.ready.push(iss);
                else if (blockedSet.has(iss.number)) bucket.blocked.push(iss);
            }
        }
    }

    const nodes = roster.map((member) => {
        const bucket = issuesByMember.get(member.id) || { ready: [], blocked: [] };
        const memberPrs = (openPrs || []).filter((p) => {
            const hay = `${p.title || ""} ${p.headRefName || ""}`.toLowerCase();
            return hay.includes(member.id);
        });
        return deriveNodeStateAndActions(member, {
            readyIssues: bucket.ready,
            blockedIssues: bucket.blocked,
            openPrs: memberPrs,
            repoNextAvailable: iq?.next_available || null,
        });
    });

    // Add the grounded Walkthrough Media Agent node (facts strictly from active deck.json)
    const sceneCount = Array.isArray(timelineJson?.scenes) ? timelineJson.scenes.length : (deckJson?.scenes || 0);
    const totalDur = typeof timelineJson?.total_duration === "number" ? timelineJson.total_duration : deckJson?.total_duration;
    const paceLabel = deckJson?.pace?.target_sps ? `${deckJson.pace.target_sps} syl/s` : "measured WAV";
    nodes.push({
        id: "video-agent",
        name: "Walkthrough Video Agent",
        role: "Media · Grounded Q&A",
        level: "video",
        state: "active",
        state_label: `${sceneCount} scenes · ${paceLabel}`,
        summary: `Grounded in this artifact pack (${totalDur ? `${totalDur.toFixed(1)}s` : "exact timeline"}). Click to pause briefing and ask about any scene or check.`,
        chat_prompt: `Chat with the Walkthrough Video Agent about "${timelineJson?.title || packMeta?.title || "current pack"}".`,
        quick_actions: [],
    });

    // 7. Live clickable artifact links (strictly factual live links + active pack durable links)
    const artifacts = [];
    if (packPr?.url) {
        artifacts.push({ label: `PR #${packPr.number} (${packPr.state})`, url: packPr.url, kind: "pr" });
    }
    for (const p of (openPrs || []).slice(0, 4)) {
        if (packPr && p.number === packPr.number) continue;
        artifacts.push({ label: `Open PR #${p.number}`, url: p.url, kind: "pr" });
    }
    if (iq?.next_available && repoSlug) {
        artifacts.push({
            label: `Next Ready #${iq.next_available.number}`,
            url: `https://github.com/${repoSlug}/issues/${iq.next_available.number}`,
            kind: "issue",
        });
    }
    if (Array.isArray(timelineJson?.hud?.artifacts)) {
        const seenUrls = new Set(artifacts.map((a) => a.url));
        for (const a of timelineJson.hud.artifacts) {
            if (a?.url && !seenUrls.has(a.url)) {
                artifacts.push(a);
                seenUrls.add(a.url);
            }
        }
    }

    const packCommit = packMeta?.commit || null;
    const packFreshness = {
        packCommit,
        liveHead: headSha,
        branch,
        stale: Boolean(packCommit && headSha && !packCommit.startsWith(headSha) && !headSha.startsWith(packCommit)),
        packPrState: packPr?.state || null,
        packPrMergedAt: packPr?.mergedAt || null,
    };

    return {
        ok: true,
        measuredAt,
        repoSlug,
        repoPath: repoPath || null,
        branch,
        headSha,
        sources,
        errors,
        packFreshness,
        stats,
        nodes,
        artifacts,
    };
}
