// Deterministic A2UI v1.0 callback router + loopback deck server for the
// narrated-walkthrough Copilot App Extension Canvas.
//
// Every UI click in the canvas iframe posts here BEFORE any AI agent turn:
//   1. CSRF nonce verification (fail closed, 403)
//   2. A2UI v1.0 payload validation & action allowlist (fail closed, 400)
//   3. Deterministic local handling (oversight, bookmarks, seek, audit log, SSE)
//   4. Structured session.send({ prompt, displayPrompt, mode }) when an AI turn
//      is required — waking the yielded Copilot agent deterministically.

import { execFile, spawn } from "node:child_process";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { appendFile, readdir, readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { DETERMINISTIC_COMPUTE_ACTIONS, executeDeterministicAction } from "./deterministic-actions.mjs";
import { collectLiveTelemetry } from "./live-telemetry.mjs";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLAYER_TEMPLATE_PATH = path.resolve(__dirname, "..", "..", "..", "skills", "narrated-walkthrough", "player", "player.html");
const DOC_BUILDER_PATH = path.resolve(__dirname, "..", "..", "..", "skills", "narrated-walkthrough", "scripts", "doc_to_walkthrough.py");
const BUILD_NARRATION_PATH = path.resolve(__dirname, "..", "..", "..", "skills", "narrated-walkthrough", "scripts", "build_narration.py");

export const MAX_BODY_BYTES = 16 * 1024;
export const DETERMINISTIC_ONLY_ACTIONS = new Set([
    "set_oversight",
    "seek_scene",
    "bookmark_scene",
    "inspect_a2ui",
]);
export const SENSITIVE_CONFIRM_ACTIONS = new Set([
    "merge_pr",
    "deploy_prod",
    "delete_resource",
]);
const ACTION_NAME_RE = /^[a-z][a-z0-9_.-]{0,63}$/;

const MIME_BY_EXT = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
};

export function tokensMatch(expected, actual) {
    if (typeof expected !== "string" || typeof actual !== "string") return false;
    const a = Buffer.from(expected, "utf8");
    const b = Buffer.from(actual, "utf8");
    if (a.length !== b.length || a.length === 0) return false;
    return timingSafeEqual(a, b);
}

/**
 * Validate an incoming A2UI v1.0 client callback payload.
 * Returns `{ ok: true, normalized }` or `{ ok: false, error }`.
 */
export function validateA2uiCallback(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return { ok: false, error: "Payload must be a JSON object." };
    }
    const version = payload.version ?? "v1.0";
    if (version !== "v1.0") {
        return { ok: false, error: `Unsupported A2UI version '${String(version)}'; expected 'v1.0'.` };
    }
    const action = payload.action;
    if (!action || typeof action !== "object" || Array.isArray(action)) {
        return { ok: false, error: "Missing required 'action' object." };
    }
    if (typeof action.name !== "string" || !ACTION_NAME_RE.test(action.name)) {
        return {
            ok: false,
            error: "action.name must match /^[a-z][a-z0-9_.-]{0,63}$/.",
        };
    }
    const surfaceId = typeof action.surfaceId === "string" && action.surfaceId.trim()
        ? action.surfaceId.trim()
        : (typeof payload.surfaceId === "string" && payload.surfaceId.trim() ? payload.surfaceId.trim() : "");
    if (!surfaceId) {
        return { ok: false, error: "Missing non-empty 'surfaceId'." };
    }
    const context = action.context && typeof action.context === "object" && !Array.isArray(action.context)
        ? action.context
        : {};
    const label = typeof payload.label === "string" ? payload.label.trim().slice(0, 240) : action.name;
    const sourceComponentId = typeof action.sourceComponentId === "string"
        ? action.sourceComponentId.trim().slice(0, 128)
        : (typeof payload.sourceComponentId === "string" ? payload.sourceComponentId.trim().slice(0, 128) : undefined);
    const sceneId = typeof payload.sceneId === "string" ? payload.sceneId.trim().slice(0, 128) : undefined;
    const timestampSec = typeof payload.timestampSec === "number" && Number.isFinite(payload.timestampSec) && payload.timestampSec >= 0
        ? Math.round(payload.timestampSec * 1000) / 1000
        : undefined;
    const oversight = typeof payload.oversight === "string" ? payload.oversight.trim().slice(0, 32) : undefined;
    const userNote = typeof payload.userNote === "string" ? payload.userNote.trim().slice(0, 2000) : undefined;
    const node = payload.node && typeof payload.node === "object" && !Array.isArray(payload.node)
        ? {
            id: String(payload.node.id ?? "").slice(0, 64),
            name: String(payload.node.name ?? "").slice(0, 120),
            role: String(payload.node.role ?? "").slice(0, 120),
            level: String(payload.node.level ?? "").slice(0, 32),
            chat_prompt: typeof payload.node.chat_prompt === "string" ? payload.node.chat_prompt.slice(0, 500) : undefined,
        }
        : undefined;
    const deliveryMode = payload.mode === "enqueue" ? "enqueue" : "immediate";
    const dryRun = payload.dryRun === true;

    return {
        ok: true,
        normalized: {
            version: "v1.0",
            surfaceId,
            sourceComponentId,
            label,
            action: {
                name: action.name,
                surfaceId,
                ...(sourceComponentId ? { sourceComponentId } : {}),
                context,
            },
            ...(sceneId ? { sceneId } : {}),
            ...(timestampSec !== undefined ? { timestampSec } : {}),
            ...(oversight ? { oversight } : {}),
            ...(userNote ? { userNote } : {}),
            ...(node && node.id ? { node } : {}),
            deliveryMode,
            dryRun,
        },
    };
}

/**
 * Build the structured prompt + human-readable displayPrompt sent to session.send().
 */
export function buildAgentTurnPayload(normalized, { instanceId, deckDir, deckTitle }) {
    const requiresExplicitConfirmation = SENSITIVE_CONFIRM_ACTIONS.has(normalized.action.name);
    const sceneSuffix = normalized.sceneId
        ? ` (${normalized.sceneId}${normalized.timestampSec !== undefined ? ` @ ${normalized.timestampSec.toFixed(1)}s` : ""})`
        : "";

    let displayPrompt;
    if (normalized.action.name === "chat_node" && normalized.node?.name) {
        displayPrompt = `Architect HUD: Chat with ${normalized.node.name}${sceneSuffix}`;
    } else if (normalized.action.name === "reply_custom" && normalized.userNote) {
        displayPrompt = `Architect HUD${sceneSuffix}: ${normalized.userNote}`;
    } else {
        displayPrompt = `Architect A2UI [${normalized.action.name}]: ${normalized.label}${sceneSuffix}`;
    }

    const envelope = {
        kind: "a2ui_canvas_callback",
        version: "v1.0",
        instanceId,
        deckTitle: deckTitle || "",
        deckDir: deckDir || "",
        surfaceId: normalized.surfaceId,
        ...(normalized.sourceComponentId ? { sourceComponentId: normalized.sourceComponentId } : {}),
        action: normalized.action,
        ...(normalized.node ? { targetNode: normalized.node } : {}),
        ...(normalized.sceneId ? { sceneId: normalized.sceneId } : {}),
        ...(normalized.timestampSec !== undefined ? { timestampSec: normalized.timestampSec } : {}),
        ...(normalized.oversight ? { oversight: normalized.oversight } : {}),
        ...(normalized.userNote ? { userNote: normalized.userNote } : {}),
        guardrails: {
            deterministicValidated: true,
            requiresExplicitConfirmation,
        },
    };

    const lines = [
        "[A2UI Canvas Callback]",
        `The Architect triggered action \`${normalized.action.name}\` from the Narrated Walkthrough canvas (\`${instanceId}\`).`,
        ...(normalized.node?.name
            ? [`Target agent node: **${normalized.node.name}** (${normalized.node.role || normalized.node.level || "agent"}).`]
            : []),
        ...(normalized.userNote ? [`Architect message: ${normalized.userNote}`] : []),
        ...(requiresExplicitConfirmation
            ? ["Guardrail: this action is marked sensitive (`requiresExplicitConfirmation: true`). Verify prerequisites and confirm explicitly before executing any irreversible repository or release mutation unless the Architect already explicitly authorized it."]
            : []),
        "",
        "```json",
        JSON.stringify(envelope, null, 2),
        "```",
    ];

    return {
        prompt: lines.join("\n"),
        displayPrompt,
        mode: normalized.deliveryMode,
        envelope,
    };
}

/**
 * Deterministic JS callback hook executed before waking any AI agent.
 */
export async function dispatchA2uiCallback(rawPayload, runtime) {
    const validated = validateA2uiCallback(rawPayload);
    if (!validated.ok) {
        return { status: 400, body: { ok: false, error: validated.error } };
    }
    const n = validated.normalized;
    const timestamp = new Date().toISOString();
    const callbackId = `cb_${randomBytes(6).toString("hex")}`;

    // 1. Always apply deterministic local state updates first (0 LLM tokens)
    if (n.oversight) {
        runtime.state.oversight = n.oversight;
    }
    if (n.sceneId) {
        runtime.state.lastSceneId = n.sceneId;
    }
    if (n.timestampSec !== undefined) {
        runtime.state.lastTimestampSec = n.timestampSec;
    }

    const isDeterministicOnly = DETERMINISTIC_ONLY_ACTIONS.has(n.action.name);
    const auditRecord = {
        callbackId,
        timestamp,
        instanceId: runtime.instanceId,
        surfaceId: n.surfaceId,
        action: n.action.name,
        label: n.label,
        sceneId: n.sceneId,
        timestampSec: n.timestampSec,
        oversight: n.oversight,
        nodeId: n.node?.id,
        deterministicOnly: isDeterministicOnly,
        dryRun: n.dryRun,
    };

    runtime.state.recentCallbacks.push(auditRecord);
    if (runtime.state.recentCallbacks.length > 50) {
        runtime.state.recentCallbacks.shift();
    }

    // Best-effort append to durable per-deck JSONL audit log
    if (runtime.deckDir) {
        try {
            await appendFile(
                path.join(runtime.deckDir, ".canvas-actions.jsonl"),
                `${JSON.stringify(auditRecord)}\n`,
                "utf8",
            );
        } catch {
            // Non-fatal: read-only deck directory still works in memory
        }
    }

    if (isDeterministicOnly) {
        runtime.broadcast("callback_ack", {
            callbackId,
            action: n.action.name,
            label: n.label,
            dispatchedToAgent: false,
            status: "handled_deterministically",
            oversight: runtime.state.oversight,
            timestamp,
        });
        return {
            status: 200,
            body: {
                ok: true,
                callbackId,
                dispatchedToAgent: false,
                status: "handled_deterministically",
                state: {
                    oversight: runtime.state.oversight,
                    lastSceneId: runtime.state.lastSceneId,
                },
            },
        };
    }

    // 1b. Deterministic compute actions (triage_bottleneck, unblock_issue, assign_next_item)
    // run authoritative live CLI/DAG analysis in Node with 0 LLM tokens FIRST and render
    // the verified result directly into A-HUD; an AI turn is only triggered if the Architect
    // explicitly sends the verified dossier via `dispatch_verified_dossier`.
    if (DETERMINISTIC_COMPUTE_ACTIONS.has(n.action.name) && !n.action.context?.forceAgentTurn) {
        const computed = await executeDeterministicAction(n, {
            repoPath: runtime.state.repoPath,
            ...(runtime.telemetryRunner ? { run: runtime.telemetryRunner } : {}),
        });
        const receipt = {
            callbackId,
            timestamp,
            action: n.action.name,
            label: n.label,
            node: n.node || null,
            ...computed,
        };
        runtime.state.lastDeterministicResult = receipt;
        if (runtime.deckDir) {
            try {
                await appendFile(
                    path.join(runtime.deckDir, ".canvas-actions.jsonl"),
                    `${JSON.stringify({ callbackId, timestamp, kind: "deterministic_compute_result", action: n.action.name, ok: computed.ok, llmTokensUsed: 0, summary: computed.summary || computed.error })}\n`,
                    "utf8",
                );
            } catch {}
        }
        runtime.broadcast("deterministic_result", receipt);
        runtime.broadcast("callback_ack", {
            callbackId,
            action: n.action.name,
            label: n.label,
            dispatchedToAgent: false,
            status: computed.ok ? "computed_deterministically" : "deterministic_error",
            displayPrompt: computed.summary || computed.error,
            timestamp,
        });
        return {
            status: computed.ok ? 200 : 500,
            body: {
                ok: computed.ok,
                callbackId,
                dispatchedToAgent: false,
                llmTokensUsed: 0,
                status: computed.ok ? "computed_deterministically" : "deterministic_error",
                result: receipt,
                ...(computed.ok ? {} : { error: computed.error }),
            },
        };
    }

    // 2. Build structured agent-turn payload and wake yielded session via session.send()
    const turnPayload = buildAgentTurnPayload(n, {
        instanceId: runtime.instanceId,
        deckDir: runtime.deckDir,
        deckTitle: runtime.state.title,
    });

    if (n.dryRun) {
        runtime.broadcast("callback_ack", {
            callbackId,
            action: n.action.name,
            label: n.label,
            dispatchedToAgent: false,
            dryRun: true,
            status: "dry_run_validated",
            displayPrompt: turnPayload.displayPrompt,
            timestamp,
        });
        return {
            status: 200,
            body: {
                ok: true,
                callbackId,
                dispatchedToAgent: false,
                dryRun: true,
                status: "dry_run_validated",
                displayPrompt: turnPayload.displayPrompt,
                envelope: turnPayload.envelope,
            },
        };
    }

    if (!runtime.session || typeof runtime.session.send !== "function") {
        return {
            status: 503,
            body: {
                ok: false,
                callbackId,
                error: "Copilot session bridge is not connected.",
            },
        };
    }

    runtime.state.agentStatus = "dispatching";
    runtime.broadcast("callback_ack", {
        callbackId,
        action: n.action.name,
        label: n.label,
        dispatchedToAgent: true,
        status: "dispatching",
        displayPrompt: turnPayload.displayPrompt,
        timestamp,
    });

    try {
        const messageId = await runtime.session.send({
            prompt: turnPayload.prompt,
            displayPrompt: turnPayload.displayPrompt,
            mode: turnPayload.mode,
        });
        runtime.state.agentStatus = "thinking";
        runtime.state.lastMessageId = messageId;
        runtime.broadcast("agent_status", {
            callbackId,
            messageId,
            status: "thinking",
            displayPrompt: turnPayload.displayPrompt,
            timestamp: new Date().toISOString(),
        });
        return {
            status: 200,
            body: {
                ok: true,
                callbackId,
                messageId,
                dispatchedToAgent: true,
                status: "dispatched",
                displayPrompt: turnPayload.displayPrompt,
            },
        };
    } catch (error) {
        runtime.state.agentStatus = "error";
        const msg = error instanceof Error ? error.message : String(error);
        runtime.broadcast("agent_status", {
            callbackId,
            status: "error",
            error: msg,
            timestamp: new Date().toISOString(),
        });
        return {
            status: 502,
            body: {
                ok: false,
                callbackId,
                dispatchedToAgent: false,
                error: `Failed to wake session via session.send(): ${msg}`,
            },
        };
    }
}

export function injectBridgeBootstrap(html, bridgeConfig) {
    const safeJson = JSON.stringify(bridgeConfig).replace(/<\//g, "<\\/");
    const tag = `\n<script>window.__WALKTHROUGH_BRIDGE__ = ${safeJson};</script>\n`;
    // Inject immediately after the opening <head> tag (never via indexOf("</head>"),
    // because an inlined Mermaid/DOMPurify bundle inside <head> can contain literal
    // "</head>" string literals that would be corrupted by a naive match).
    const headMatch = /<head(?:\s[^>]*)?>/i.exec(html);
    if (headMatch) {
        const at = headMatch.index + headMatch[0].length;
        return html.slice(0, at) + tag + html.slice(at);
    }
    const bodyMatch = /<body(?:\s[^>]*)?>/i.exec(html);
    if (bodyMatch) {
        const at = bodyMatch.index + bodyMatch[0].length;
        return html.slice(0, at) + tag + html.slice(at);
    }
    return tag + html;
}

export function resolveSafeAsset(deckDir, urlPath) {
    const cleanRel = decodeURIComponent(urlPath).replace(/^\/+/, "");
    if (!cleanRel || cleanRel.includes("\0")) return null;
    const root = path.resolve(deckDir);
    const resolved = path.resolve(root, cleanRel);
    if (resolved !== root && !resolved.startsWith(root + path.sep)) {
        return null;
    }
    // Never expose hidden dotfiles (.canvas-actions.jsonl, .raw masters, etc.) over HTTP
    const relParts = path.relative(root, resolved).split(path.sep);
    if (relParts.some((part) => part.startsWith("."))) {
        return null;
    }
    return resolved;
}

function classifyPackKind(spec, title, hudPackKind) {
    if (typeof hudPackKind === "string" && hudPackKind.trim()) return hudPackKind.trim();
    const hay = `${spec || ""} ${title || ""}`.toLowerCase();
    if (/\bpr[\s#-]*\d+|\bpull[\s-]?request|\breview\b/.test(hay)) return "pr_review";
    if (/\bstandup\b|\bmorning[\s-]?brief\b|\bdaily\b/.test(hay)) return "standup";
    if (/\barch(?:itecture)?\b|\bdesign\b|\bdoc(?:ument)?\b|\bflow\b|\bdiagram\b|\badr\b/.test(hay)) return "architecture_doc";
    return "walkthrough";
}

function normalizeAgentLabel(producer, kind) {
    const raw = String(producer || "").trim();
    if (!raw) {
        if (kind === "pr_review") return "PR Review Agent";
        if (kind === "standup") return "Morning Standup";
        if (kind === "architecture_doc") return "Architecture Doc";
        return "Walkthrough Agent";
    }
    // Trim parenthetical timestamps/notes so agent filter stays concise and grouped
    return raw.replace(/\s*\([^)]*\)\s*$/g, "").trim() || raw;
}

async function inspectPackDir(dir, meta = {}) {
    const resolved = path.resolve(dir);
    const slidesSt = await stat(path.join(resolved, "slides.html")).catch(() => null);
    if (!slidesSt || !slidesSt.isFile()) return null;

    let tl = null;
    let deck = null;
    try { tl = JSON.parse(await readFile(path.join(resolved, "timeline.json"), "utf8")); } catch {}
    try { deck = JSON.parse(await readFile(path.join(resolved, "deck.json"), "utf8")); } catch {}

    const title = (typeof tl?.title === "string" && tl.title.trim())
        || meta.spec
        || path.basename(resolved);
    const kind = classifyPackKind(meta.spec, title, tl?.hud?.pack_kind);
    const producer = (typeof tl?.hud?.producer === "string" && tl.hud.producer.trim())
        || (kind === "pr_review"
            ? "PR Review Agent"
            : kind === "standup"
                ? "Morning Standup Briefer"
                : kind === "architecture_doc"
                    ? "Architecture Doc Brief"
                    : "Walkthrough Agent");
    const agent = normalizeAgentLabel(producer, kind);
    const totalDuration = typeof tl?.total_duration === "number"
        ? tl.total_duration
        : (typeof deck?.total_duration === "number" ? deck.total_duration : null);
    const sceneCount = Array.isArray(tl?.scenes)
        ? tl.scenes.length
        : (typeof deck?.scenes === "number" ? deck.scenes : 0);

    let hasVideo = false;
    try {
        const files = await readdir(resolved);
        hasVideo = files.some((f) => f.endsWith(".mp4") || f.endsWith(".webm"));
    } catch {}

    return {
        id: meta.run || path.basename(path.dirname(resolved)) || path.basename(resolved),
        kind,
        title,
        producer,
        agent,
        status: meta.status || "ready",
        spec: meta.spec || "",
        issue: meta.issue ? String(meta.issue) : null,
        branch: meta.branch || null,
        commit: meta.commit || null,
        updatedAt: meta.updatedUtc || meta.utc || slidesSt.mtime.toISOString(),
        worktreePath: meta.worktreePath || null,
        totalDuration,
        sceneCount,
        hasVideo,
        deckDir: resolved,
    };
}

/**
 * Infer a repository's sibling `<repo>-artifacts` root from any path inside the repo or worktree.
 */
export async function inferArtifactRootFromRepo(repoPath) {
    if (!repoPath) return null;
    const absRepo = path.resolve(repoPath);
    let canonicalRepo = absRepo;
    try {
        const { stdout } = await execFileAsync("git", ["-C", absRepo, "rev-parse", "--git-common-dir"], { timeout: 5000 });
        const commonDir = path.resolve(absRepo, stdout.trim());
        if (path.basename(commonDir) === ".git") {
            canonicalRepo = path.dirname(commonDir);
        }
    } catch {
        // Not a git repo — fall back to absRepo basename
    }
    const candidate = `${canonicalRepo}-artifacts`;
    const st = await stat(candidate).catch(() => null);
    return st && st.isDirectory() ? candidate : null;
}

/**
 * Render a live A-HUD landing view when a repo has no built walkthrough packs yet.
 */
export async function renderEmptyLandingHtml({ title, repoPath }) {
    const tpl = await readFile(PLAYER_TEMPLATE_PATH, "utf8");
    const repoName = repoPath ? path.basename(path.resolve(repoPath)) : "this repository";
    const starterData = {
        title: title || `A-HUD — ${repoName}`,
        scenes: [],
        cues: [],
        total: 0,
        hud: {
            summary: `Live deterministic Architect HUD for ${repoName}. No narrated walkthrough artifact packs have been dropped yet — live repo, queue, PR, and artifact telemetry are active below, or trigger a pack build with one click.`,
            oversight_default: "lead",
        },
        a2ui: [
            {
                version: "v1.0",
                createSurface: {
                    surfaceId: "ahud-starter-actions",
                    catalogId: "https://a2ui.org/specification/v1_0/basic_catalog.json",
                },
            },
            {
                version: "v1.0",
                updateComponents: {
                    surfaceId: "ahud-starter-actions",
                    components: [
                        { id: "root", component: "Column", children: ["prompt", "note", "actions"] },
                        { id: "prompt", component: "Text", variant: "h3", text: `No walkthrough packs dropped yet in ${repoName} — build one now?` },
                        { id: "note", component: "Text", variant: "body", text: "Live deterministic telemetry above is already active. Pick a starter pack below to wake the agent and drop a narrated pack into your artifact root." },
                        { id: "actions", component: "Row", children: ["act-standup", "act-pr-review"] },
                        { id: "lbl-standup", component: "Text", text: "Generate Daily Standup Walkthrough Pack" },
                        { id: "act-standup", component: "Button", variant: "primary", child: "lbl-standup", action: { name: "generate_pack", context: { kind: "standup", repoPath: repoPath || "" } } },
                        { id: "lbl-pr", component: "Text", text: "Generate PR Review Walkthrough Pack" },
                        { id: "act-pr-review", component: "Button", variant: "secondary", child: "lbl-pr", action: { name: "generate_pack", context: { kind: "pr_review", repoPath: repoPath || "" } } },
                    ],
                },
            },
        ],
    };
    const safeData = JSON.stringify(starterData).replace(/<\//g, "<\\/");
    return tpl
        .replace("__DATA__", safeData)
        .replace("__TITLE__", starterData.title)
        .replace("__VOICE__", "live telemetry")
        .replace("__MODEL__", "A-HUD")
        .replace("__MERMAID__", "<script>window.mermaid = { initialize() {}, async render() { return { svg: '' }; } };</script>");
}

/**
 * Find the nearest runs.jsonl artifacts root by walking up from deckDir (max 5 levels).
 */
export async function inferPacksRoot(deckDir) {
    let cur = path.resolve(deckDir);
    for (let depth = 0; depth < 5; depth++) {
        const idx = path.join(cur, "runs.jsonl");
        const st = await stat(idx).catch(() => null);
        if (st && st.isFile()) return cur;
        const parent = path.dirname(cur);
        if (parent === cur) break;
        cur = parent;
    }
    return path.dirname(path.resolve(deckDir));
}

/**
 * Discover all agent-dropped narrated walkthrough artifact packs under packsRoot.
 * Supports e2e-artifacts `runs.jsonl` indices as well as flat directories of packs.
 */
export async function discoverArtifactPacks({ packsRoot, activeDeckDir }) {
    const foundByDir = new Map();
    const activeResolved = activeDeckDir ? path.resolve(activeDeckDir) : null;

    if (packsRoot) {
        const root = path.resolve(packsRoot);
        const runsJsonl = path.join(root, "runs.jsonl");
        const idxSt = await stat(runsJsonl).catch(() => null);

        if (idxSt && idxSt.isFile()) {
            const text = await readFile(runsJsonl, "utf8").catch(() => "");
            const latestByRun = new Map();
            for (const line of text.split(/\r?\n/)) {
                const trimmed = line.trim();
                if (!trimmed) continue;
                try {
                    const row = JSON.parse(trimmed);
                    if (row && row.run) {
                        const prev = latestByRun.get(row.run) || {};
                        latestByRun.set(row.run, { ...prev, ...row });
                    }
                } catch {}
            }
            for (const row of latestByRun.values()) {
                const candidateDir = row.phasePath || (row.path ? path.join(row.path, "planning") : null);
                if (!candidateDir) continue;
                const pack = await inspectPackDir(candidateDir, row);
                if (pack) foundByDir.set(pack.deckDir, pack);
            }
        }

        // Also scan immediate child directories (and <child>/planning) for flat pack folders
        try {
            const entries = await readdir(root, { withFileTypes: true });
            for (const ent of entries) {
                if (!ent.isDirectory() || ent.name.startsWith(".")) continue;
                for (const candidate of [path.join(root, ent.name), path.join(root, ent.name, "planning")]) {
                    const abs = path.resolve(candidate);
                    if (foundByDir.has(abs)) continue;
                    const pack = await inspectPackDir(abs, { run: ent.name });
                    if (pack) foundByDir.set(pack.deckDir, pack);
                }
            }
        } catch {}
    }

    // Ensure the currently loaded deckDir always appears in the landscape feed
    if (activeResolved && !foundByDir.has(activeResolved)) {
        const activePack = await inspectPackDir(activeResolved, {});
        if (activePack) foundByDir.set(activePack.deckDir, activePack);
    }

    return [...foundByDir.values()]
        .map((p) => ({ ...p, active: activeResolved === p.deckDir }))
        .sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
}

function estimateSyllablesJs(text) {
    const words = String(text || "").match(/[A-Za-z0-9]+(?:'[A-Za-z]+)?/g) || [];
    let total = 0;
    for (const w of words) {
        const letters = w.toLowerCase().replace(/[^a-z]/g, "");
        if (!letters) { total += Math.max(1, w.length); continue; }
        let c = (letters.match(/[aeiouy]+/g) || []).length;
        if (letters.endsWith("e") && c > 1) c -= 1;
        total += Math.max(1, c);
    }
    return Math.max(1, total);
}

/**
 * Load a built pack's runtime JSON payload for zero-reload in-place SPA switching.
 */
export async function buildPackRuntimeData(deckDir, fallbackTitle = "Narrated Walkthrough") {
    if (!deckDir) return null;
    const resolved = path.resolve(deckDir);
    const tl = JSON.parse(await readFile(path.join(resolved, "timeline.json"), "utf8"));
    const scenes = Array.isArray(tl.scenes) ? tl.scenes : [];
    const cues = [];
    let running = 0;
    const normScenes = scenes.map((s) => {
        const dur = Number(s.duration) || 1;
        const start = typeof s.start === "number" ? s.start : Math.round(running * 1000) / 1000;
        running = start + dur;
        const caps = Array.isArray(s.captions) && s.captions.length ? s.captions : [s.narration || s.title || ""];
        const weights = caps.map((c) => estimateSyllablesJs(c));
        const totalW = weights.reduce((a, b) => a + b, 0) || 1;
        let cur = start;
        const endScene = start + dur;
        caps.forEach((text, i) => {
            const endT = i === caps.length - 1 ? endScene : cur + dur * (weights[i] / totalW);
            cues.push({
                t: Math.round(cur * 1000) / 1000,
                end: Math.round(endT * 1000) / 1000,
                text,
                scene: s.id,
            });
            cur = endT;
        });
        return {
            id: s.id,
            title: s.title,
            mermaid: s.mermaid,
            start,
            duration: dur,
            audio: s.audio,
            narration: s.narration,
            ...(s.kind ? { kind: s.kind } : {}),
            ...(s.choices ? { choices: s.choices } : {}),
            ...(s.meta ? { meta: s.meta } : {}),
        };
    });

    let masterUrl = null;
    const masterRel = tl.master_audio || "audio/master.wav";
    const masterFile = path.join(resolved, masterRel);
    try {
        const bytes = await readFile(masterFile);
        const sha8 = createHash("sha256").update(bytes).digest("hex").slice(0, 8);
        masterUrl = `${masterRel}?v=${sha8}`;
    } catch {
        masterUrl = null;
    }

    let repo = typeof tl.repo === "string" && tl.repo.includes("/") ? tl.repo : null;
    if (!repo && Array.isArray(tl.hud?.artifacts)) {
        for (const a of tl.hud.artifacts) {
            const m = String(a?.url || "").match(/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)(?:\/|$)/i);
            if (m) { repo = m[1]; break; }
        }
    }

    return {
        title: tl.title || fallbackTitle,
        ...(repo ? { repo } : {}),
        master_audio: masterUrl,
        scenes: normScenes,
        cues,
        total: typeof tl.total_duration === "number" ? tl.total_duration : Math.round(running * 1000) / 1000,
        disposition: tl.disposition || null,
        hud: tl.hud || null,
        a2ui: tl.a2ui || null,
        tts: tl.tts ? { model: tl.tts.model || "none", voice: tl.tts.voice || "none" } : null,
    };
}

const OPEN_TTS_VOICE_PRESETS = ["US_James", "US_Emily", "UK_Charles", "UK_Sophia"];

/**
 * Classify an oMLX / OpenAI-compatible model metadata entry into a narration-capable
 * TTS or Speech-to-Speech (STS / Omni / Realtime voice) category, or null if unsuitable.
 */
export function classifyAudioModel(entry) {
    if (!entry || typeof entry !== "object") return null;
    const id = String(entry.id || "").trim();
    if (!id) return null;
    const mtype = String(entry.model_type || entry.type || "").toLowerCase();
    const etype = String(entry.engine_type || "").toLowerCase();
    const ctype = String(entry.config_model_type || "").toLowerCase();
    const hay = `${id} ${mtype} ${etype} ${ctype}`.toLowerCase();

    // Exclude non-speech / transcription-only models
    if (
        mtype === "audio_stt" || etype === "audio_stt" || mtype === "reranker" ||
        mtype === "markitdown" || mtype === "embedding" ||
        /\b(whisper|parakeet|transcrib|asr|stt|rerank|markitdown|ocr|embed)\b/.test(hay)
    ) {
        return null;
    }

    // Speech-to-speech / Omni / Realtime voice models
    if (
        entry.realtime_voice === true ||
        mtype === "audio_sts" || etype === "audio_sts" || mtype === "sts" || mtype === "audio_omni" ||
        /(omni|sts|lfm[\d.]*[-_]?audio|mossformer|sesame|csm)/.test(`${id.toLowerCase()} ${ctype}`)
    ) {
        return { kind: "sts", kindLabel: "Speech-to-Speech" };
    }

    // Text-to-speech models
    if (
        mtype === "audio_tts" || etype === "audio_tts" || mtype === "tts" || mtype === "speech" ||
        /(tts|kokoro|orpheus|chatterbox|cosyvoice|parler|bark|f5[-_]?tts|dia[-_]|lingv3_tts|spark[-_]?tts)/.test(`${id.toLowerCase()} ${ctype}`)
    ) {
        return { kind: "tts", kindLabel: "TTS" };
    }

    return null;
}

function originFromEndpoint(endpointStr) {
    if (typeof endpointStr !== "string" || !endpointStr.trim()) return null;
    try {
        const u = new URL(endpointStr.trim());
        if (u.protocol !== "http:" && u.protocol !== "https:") return null;
        return u.origin;
    } catch {
        return null;
    }
}

async function resolveAudioServerBase({ endpointBase, activeDeckDir, packsRoot }) {
    const explicit = originFromEndpoint(endpointBase);
    if (explicit) return explicit;
    const envBase = originFromEndpoint(process.env.OMLX_BASE_URL || process.env.NARRATED_WALKTHROUGH_TTS_ENDPOINT || "");
    if (envBase) return envBase;

    // Inspect active pack first, then any sibling pack in packsRoot for an existing tts.endpoint
    const candidateDirs = [];
    if (activeDeckDir) candidateDirs.push(path.resolve(activeDeckDir));
    if (packsRoot) {
        const packs = await discoverArtifactPacks({ packsRoot, activeDeckDir }).catch(() => []);
        for (const p of packs) {
            if (p.deckDir && !candidateDirs.includes(p.deckDir)) candidateDirs.push(p.deckDir);
        }
    }
    for (const dir of candidateDirs) {
        try {
            const tl = JSON.parse(await readFile(path.join(dir, "timeline.json"), "utf8"));
            const found = originFromEndpoint(tl?.tts?.endpoint);
            if (found) return found;
        } catch {
            // ignore unreadable timeline
        }
    }
    return "http://127.0.0.1:18001";
}

function isAllowedAudioEndpoint(endpointUrl, discoveredBaseUrl) {
    try {
        const u = new URL(endpointUrl);
        if (u.protocol !== "http:" && u.protocol !== "https:") return false;
        if (u.hostname === "127.0.0.1" || u.hostname === "localhost" || u.hostname === "::1") return true;
        if (discoveredBaseUrl) {
            const d = new URL(discoveredBaseUrl);
            if (u.origin === d.origin) return true;
        }
        return false;
    } catch {
        return false;
    }
}

/**
 * Dynamically discover available oMLX TTS and Speech-to-Speech models and their voices.
 */
export async function discoverAudioModels({
    endpointBase,
    activeDeckDir,
    packsRoot,
    fetchImpl = globalThis.fetch,
    timeoutMs = 2500,
} = {}) {
    const baseUrl = await resolveAudioServerBase({ endpointBase, activeDeckDir, packsRoot });
    const speechEndpoint = `${baseUrl}/v1/audio/speech`;
    if (typeof fetchImpl !== "function") {
        return { ok: true, available: false, baseUrl, endpoint: speechEndpoint, models: [], error: "fetch unavailable" };
    }

    async function fetchJson(url) {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), timeoutMs);
        try {
            const resp = await fetchImpl(url, { signal: ac.signal });
            if (!resp.ok) return null;
            return await resp.json();
        } catch {
            return null;
        } finally {
            clearTimeout(timer);
        }
    }

    // Prefer /v1/models/status (rich oMLX metadata incl. realtime_voice & config_model_type),
    // falling back to OpenAI-standard /v1/models.
    let rawList = null;
    const statusJson = await fetchJson(`${baseUrl}/v1/models/status`);
    if (statusJson && Array.isArray(statusJson.models)) {
        rawList = statusJson.models;
    } else {
        const modelsJson = await fetchJson(`${baseUrl}/v1/models`);
        if (modelsJson && Array.isArray(modelsJson.data)) {
            rawList = modelsJson.data;
        }
    }

    if (!rawList) {
        return {
            ok: true,
            available: false,
            baseUrl,
            endpoint: speechEndpoint,
            models: [],
            error: `oMLX server unreachable at ${baseUrl}`,
        };
    }

    const candidates = [];
    for (const entry of rawList) {
        const cls = classifyAudioModel(entry);
        if (!cls) continue;
        candidates.push({ entry, cls });
    }

    const enriched = await Promise.all(
        candidates.map(async ({ entry, cls }) => {
            const id = String(entry.id);
            const vJson = await fetchJson(`${baseUrl}/v1/audio/voices?model=${encodeURIComponent(id)}`);
            const serverVoices = Array.isArray(vJson?.voices)
                ? vJson.voices.map((v) => String(v)).filter(Boolean)
                : [];
            const hasExplicitVoices = serverVoices.length > 0;
            const voices = hasExplicitVoices ? serverVoices : [...OPEN_TTS_VOICE_PRESETS];
            const defaultVoice = hasExplicitVoices
                ? (serverVoices.includes("male") ? "male" : serverVoices[0])
                : voices[0];
            return {
                id,
                label: id,
                kind: cls.kind,
                kindLabel: cls.kindLabel,
                voices,
                defaultVoice,
                supportsAnchor: cls.kind === "tts" && !hasExplicitVoices,
                openVoice: !hasExplicitVoices,
                loaded: Boolean(entry.loaded),
                size: entry.estimated_size_formatted || null,
            };
        }),
    );

    // Order TTS models first, then Speech-to-Speech, alphabetically within each group
    enriched.sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "tts" ? -1 : 1;
        return a.id.localeCompare(b.id);
    });

    return {
        ok: true,
        available: true,
        baseUrl,
        endpoint: speechEndpoint,
        models: enriched,
    };
}

/**
 * Discover all browsable repository HUDs (sibling `<repo>-artifacts/` directories
 * plus the currently active HUD root) so the Architect can switch across HUDs.
 */
export async function discoverHuds({ activePacksRoot, activeRepoPath }) {
    const byRoot = new Map();
    const activeRoot = activePacksRoot ? path.resolve(activePacksRoot) : null;
    const scanParents = new Set();
    if (activeRoot) scanParents.add(path.dirname(activeRoot));
    if (activeRepoPath) scanParents.add(path.dirname(path.resolve(activeRepoPath)));

    const registerHud = async (packsRoot, fallbackRepoPath = null) => {
        const absRoot = path.resolve(packsRoot);
        if (byRoot.has(absRoot)) return;
        const base = path.basename(absRoot);
        const repoName = base.endsWith("-artifacts") ? base.slice(0, -"-artifacts".length) : base;
        let repoPath = fallbackRepoPath ? path.resolve(fallbackRepoPath) : null;
        if (!repoPath) {
            const siblingRepo = path.join(path.dirname(absRoot), repoName);
            const rSt = await stat(siblingRepo).catch(() => null);
            if (rSt && rSt.isDirectory()) repoPath = siblingRepo;
        }
        const packs = await discoverArtifactPacks({ packsRoot: absRoot });
        byRoot.set(absRoot, {
            id: repoName,
            name: repoName,
            packsRoot: absRoot,
            repoPath,
            packCount: packs.length,
            latestUpdatedAt: packs[0]?.updatedAt || null,
            active: activeRoot === absRoot,
        });
    };

    if (activeRoot) {
        await registerHud(activeRoot, activeRepoPath);
    }

    for (const parentDir of scanParents) {
        let entries = [];
        try {
            entries = await readdir(parentDir, { withFileTypes: true });
        } catch {
            continue;
        }
        for (const ent of entries) {
            if (!ent.isDirectory() || !ent.name.endsWith("-artifacts")) continue;
            await registerHud(path.join(parentDir, ent.name));
        }
    }

    return [...byRoot.values()].sort((a, b) => {
        if (a.active !== b.active) return a.active ? -1 : 1;
        return String(b.latestUpdatedAt || "").localeCompare(String(a.latestUpdatedAt || ""))
            || a.name.localeCompare(b.name);
    });
}

/**
 * Fast-path (< 1s) Markdown + Mermaid -> A-HUD Architecture Doc pack generator.
 */
export async function generateDocWalkthroughPack({
    docPath,
    packsRoot,
    repoPath,
    title,
    producer = "Architecture Doc Brief",
    withAudio = false,
}) {
    if (typeof docPath !== "string" || !path.isAbsolute(docPath) || !docPath.toLowerCase().endsWith(".md")) {
        throw new Error("docPath must be an absolute path to a .md file.");
    }
    const docSt = await stat(docPath).catch(() => null);
    if (!docSt || !docSt.isFile()) {
        throw new Error(`Markdown document not found: ${docPath}`);
    }
    const resolvedPacksRoot = packsRoot
        ? path.resolve(packsRoot)
        : (await inferArtifactRootFromRepo(repoPath) || `${path.resolve(repoPath || path.dirname(docPath))}-artifacts`);

    const nowIso = new Date().toISOString();
    const utcCompact = nowIso.replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
    const rand = randomBytes(3).toString("hex");
    const slug = path.basename(docPath, path.extname(docPath)).toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 32);
    const runId = `${utcCompact}-${rand}`;
    const runDir = path.join(resolvedPacksRoot, "docs", runId);
    const outDir = path.join(runDir, "planning");

    const args = [DOC_BUILDER_PATH, "--doc", path.resolve(docPath), "--out-dir", outDir, "--producer", producer];
    if (title) args.push("--title", title);
    if (withAudio) args.push("--with-audio");

    const { stdout } = await execFileAsync("python3", args, {
        cwd: repoPath || path.dirname(docPath),
        timeout: 30000,
        maxBuffer: 4 * 1024 * 1024,
    });
    const parsed = JSON.parse(stdout.trim());

    // Record the newly built doc pack into <packsRoot>/runs.jsonl so A-HUD discovers it immediately
    const runsIndex = path.join(resolvedPacksRoot, "runs.jsonl");
    const record = {
        run: runId,
        utc: nowIso,
        updatedUtc: nowIso,
        phase: "planning",
        spec: `doc-${slug}`,
        status: "passed",
        path: runDir,
        phasePath: outDir,
        worktreePath: repoPath ? path.resolve(repoPath) : null,
        docPath: path.resolve(docPath),
    };
    await appendFile(runsIndex, `${JSON.stringify(record)}\n`, "utf8");

    return {
        ...parsed,
        runId,
        packsRoot: resolvedPacksRoot,
        deckDir: outDir,
    };
}

function sendJson(res, status, value) {
    res.writeHead(status, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    });
    res.end(JSON.stringify(value));
}

function sendSse(res, event, data) {
    res.write(`event: ${event}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
}

async function readJsonBody(req) {
    const chunks = [];
    let total = 0;
    for await (const chunk of req) {
        total += chunk.length;
        if (total > MAX_BODY_BYTES) {
            const err = new Error(`Request body exceeds ${MAX_BODY_BYTES} bytes.`);
            err.statusCode = 413;
            throw err;
        }
        chunks.push(chunk);
    }
    const text = Buffer.concat(chunks).toString("utf8").trim();
    if (!text) return {};
    try {
        return JSON.parse(text);
    } catch {
        const err = new Error("Invalid JSON request body.");
        err.statusCode = 400;
        throw err;
    }
}

/**
 * Start a loopback-only HTTP server hosting a built narrated-walkthrough deck
 * with an authenticated deterministic A2UI v1.0 callback bridge and SSE stream.
 */
export async function startWalkthroughServer({
    instanceId,
    canvasId = "narrated-walkthrough",
    deckDir,
    packsRoot,
    repoPath,
    title = "Narrated Walkthrough",
    session,
    telemetryRunner,
    audioFetchImpl,
    audioEndpointBase,
    audioRegenerator,
    telemetryPollMs = 45000,
    telemetryTtlMs = 15000,
}) {
    const token = randomBytes(24).toString("hex");
    const clients = new Set();
    const regenJobs = new Map(); // deckDir -> { jobId, child, status, ... }
    const resolvedDeck = deckDir ? path.resolve(deckDir) : null;
    const resolvedRepo = repoPath ? path.resolve(repoPath) : null;
    const resolvedPacksRoot = packsRoot
        ? path.resolve(packsRoot)
        : (resolvedDeck ? await inferPacksRoot(resolvedDeck) : await inferArtifactRootFromRepo(resolvedRepo));
    const state = {
        title,
        deckDir: resolvedDeck,
        packsRoot: resolvedPacksRoot,
        repoPath: resolvedRepo,
        oversight: "lead",
        lastSceneId: null,
        lastTimestampSec: 0,
        agentStatus: "ready",
        lastMessageId: null,
        recentCallbacks: [],
        liveA2ui: null,
        telemetryCache: null,
        telemetryCachedAt: 0,
        telemetryFingerprint: "",
        lastDeterministicResult: null,
    };

    async function getTelemetry({ force = false } = {}) {
        const nowMs = Date.now();
        if (!force && state.telemetryCache && (nowMs - state.telemetryCachedAt) < telemetryTtlMs) {
            return state.telemetryCache;
        }
        const packs = await discoverArtifactPacks({
            packsRoot: state.packsRoot,
            activeDeckDir: state.deckDir,
        });
        const activePack = packs.find((p) => p.active) || null;
        const effectiveRepoPath = state.repoPath || activePack?.worktreePath || null;
        const tel = await collectLiveTelemetry({
            repoPath: effectiveRepoPath,
            deckDir: state.deckDir,
            packMeta: activePack,
            ...(telemetryRunner ? { run: telemetryRunner } : {}),
        });
        state.telemetryCache = tel;
        state.telemetryCachedAt = Date.now();
        const fp = JSON.stringify({
            headSha: tel.headSha,
            stats: tel.stats,
            nodes: tel.nodes.map((n) => [n.id, n.state, n.state_label, n.queue_depth, n.quick_actions]),
            packFreshness: tel.packFreshness,
        });
        const changed = fp !== state.telemetryFingerprint;
        state.telemetryFingerprint = fp;
        if (changed) {
            runtime.broadcast("telemetry_update", tel);
        }
        return tel;
    }

    const runtime = {
        instanceId,
        canvasId,
        token,
        clients,
        state,
        session,
        telemetryRunner,
        getTelemetry,
        get deckDir() {
            return state.deckDir;
        },
        broadcast(event, data) {
            for (const client of clients) {
                try {
                    sendSse(client, event, data);
                } catch {
                    clients.delete(client);
                }
            }
        },
    };

    function summarizeRegenJob(job) {
        if (!job) return null;
        return {
            jobId: job.jobId,
            deckDir: job.deckDir,
            model: job.model,
            voice: job.voice,
            endpoint: job.endpoint,
            status: job.status,
            stage: job.stage,
            completedScenes: job.completedScenes,
            totalScenes: job.totalScenes,
            currentScene: job.currentScene,
            startedAt: job.startedAt,
            error: job.error || null,
        };
    }

    function startBackgroundAudioRegen({ targetDeckDir, model, voice, endpoint, supportsAnchor, firstSceneId, totalScenes }) {
        const resolved = path.resolve(targetDeckDir);
        const existing = regenJobs.get(resolved);
        if (existing && existing.status === "running") {
            existing.superseded = true;
            if (existing.child && !existing.child.killed) {
                try { existing.child.kill("SIGTERM"); } catch {}
            }
        }

        const jobId = `regen_${randomBytes(6).toString("hex")}`;
        const job = {
            jobId,
            deckDir: resolved,
            model,
            voice,
            endpoint,
            status: "running",
            stage: "synthesizing",
            completedScenes: 0,
            totalScenes: totalScenes || 0,
            currentScene: null,
            startedAt: new Date().toISOString(),
            superseded: false,
            child: null,
            error: null,
        };
        regenJobs.set(resolved, job);
        runtime.broadcast("audio_regen_started", summarizeRegenJob(job));

        const onComplete = async () => {
            if (job.superseded) return;
            job.status = "complete";
            job.stage = "complete";
            job.completedScenes = job.totalScenes;
            if (resolved === state.deckDir) {
                state.telemetryCache = null;
                state.telemetryCachedAt = 0;
                state.telemetryFingerprint = "";
            }
            const freshData = await buildPackRuntimeData(resolved, state.title).catch(() => null);
            runtime.broadcast("audio_regen_complete", {
                ...summarizeRegenJob(job),
                totalDuration: freshData?.total ?? null,
                data: freshData,
                timestamp: new Date().toISOString(),
            });
        };

        const onError = (errMsg) => {
            if (job.superseded) return;
            job.status = "error";
            job.stage = "error";
            job.error = String(errMsg || "Audio regeneration failed").slice(0, 400);
            runtime.broadcast("audio_regen_error", summarizeRegenJob(job));
        };

        if (typeof audioRegenerator === "function") {
            Promise.resolve(audioRegenerator({
                deckDir: resolved,
                model,
                voice,
                endpoint,
                supportsAnchor,
                firstSceneId,
                onProgress: (patch) => {
                    if (job.superseded) return;
                    Object.assign(job, patch);
                    runtime.broadcast("audio_regen_progress", summarizeRegenJob(job));
                },
            })).then(() => onComplete()).catch((err) => onError(err?.message || String(err)));
            return summarizeRegenJob(job);
        }

        const args = [
            BUILD_NARRATION_PATH,
            "--plan", path.join(resolved, "timeline.json"),
            "--endpoint", endpoint,
            "--model", model,
            "--voice", voice,
            "--force",
            "--balance-pace",
            "--language", "en",
            "--temperature", "0.0",
        ];
        if (supportsAnchor && firstSceneId) {
            args.push("--anchor-scene", firstSceneId);
        } else {
            args.push("--clear-anchor");
        }

        const child = spawn("python3", args, {
            cwd: resolved,
            stdio: ["ignore", "pipe", "pipe"],
            env: { ...process.env, PYTHONUNBUFFERED: "1" },
        });
        job.child = child;
        let stdoutBuf = "";
        let stderrBuf = "";

        child.stdout.on("data", (chunk) => {
            stdoutBuf += chunk.toString("utf8");
            let nl;
            while ((nl = stdoutBuf.indexOf("\n")) >= 0) {
                const line = stdoutBuf.slice(0, nl).trim();
                stdoutBuf = stdoutBuf.slice(nl + 1);
                if (job.superseded) continue;
                const m = line.match(/^synth\s+(\S+)/);
                if (m) {
                    job.currentScene = m[1];
                    job.stage = "synthesizing";
                    job.completedScenes = Math.min(job.totalScenes || 999, job.completedScenes + 1);
                    runtime.broadcast("audio_regen_progress", summarizeRegenJob(job));
                } else if (line.startsWith("pace balanced:")) {
                    job.stage = "mastering";
                    runtime.broadcast("audio_regen_progress", summarizeRegenJob(job));
                }
            }
        });
        child.stderr.on("data", (chunk) => {
            stderrBuf = (stderrBuf + chunk.toString("utf8")).slice(-2000);
        });
        child.on("error", (err) => onError(err?.message || String(err)));
        child.on("close", (code) => {
            if (job.superseded) return;
            if (code === 0) {
                onComplete();
            } else {
                const tail = (stderrBuf || stdoutBuf || `build_narration.py exited with code ${code}`).trim();
                onError(tail);
            }
        });

        return summarizeRegenJob(job);
    }

    const telemetryPoller = telemetryPollMs > 0
        ? setInterval(() => {
            if (clients.size === 0) return;
            getTelemetry({ force: true }).catch(() => {});
        }, telemetryPollMs)
        : null;
    if (telemetryPoller && typeof telemetryPoller.unref === "function") {
        telemetryPoller.unref();
    }

    const server = createServer(async (req, res) => {
        try {
            const url = new URL(req.url || "/", "http://127.0.0.1");

            if (req.method === "GET" && url.pathname === "/events") {
                res.writeHead(200, {
                    "content-type": "text/event-stream; charset=utf-8",
                    "cache-control": "no-cache, no-transform",
                    connection: "keep-alive",
                });
                clients.add(res);
                sendSse(res, "bridge_ready", {
                    instanceId,
                    canvasId,
                    title: state.title,
                    agentStatus: state.agentStatus,
                    oversight: state.oversight,
                    liveA2ui: state.liveA2ui,
                    lastDeterministicResult: state.lastDeterministicResult,
                });
                const keepalive = setInterval(() => {
                    try {
                        res.write(": keepalive\n\n");
                    } catch {
                        clearInterval(keepalive);
                        clients.delete(res);
                    }
                }, 20000);
                req.on("close", () => {
                    clearInterval(keepalive);
                    clients.delete(res);
                });
                return;
            }

            if (req.method === "GET" && url.pathname === "/state") {
                sendJson(res, 200, {
                    ok: true,
                    instanceId,
                    canvasId,
                    deckDir: state.deckDir,
                    packsRoot: state.packsRoot,
                    title: state.title,
                    oversight: state.oversight,
                    agentStatus: state.agentStatus,
                    lastSceneId: state.lastSceneId,
                    lastTimestampSec: state.lastTimestampSec,
                    recentCallbacks: state.recentCallbacks.slice(-10),
                });
                return;
            }

            if (req.method === "GET" && url.pathname === "/telemetry") {
                const force = url.searchParams.get("force") === "1";
                const tel = await getTelemetry({ force });
                sendJson(res, 200, tel);
                return;
            }

            if (req.method === "GET" && url.pathname === "/huds") {
                const huds = await discoverHuds({
                    activePacksRoot: state.packsRoot,
                    activeRepoPath: state.repoPath,
                });
                sendJson(res, 200, {
                    ok: true,
                    activePacksRoot: state.packsRoot,
                    activeRepoPath: state.repoPath,
                    huds,
                });
                return;
            }

            if (req.method === "POST" && url.pathname === "/huds/select") {
                const provided = req.headers["x-canvas-token"];
                if (!tokensMatch(token, typeof provided === "string" ? provided : "")) {
                    sendJson(res, 403, { ok: false, error: "Invalid or missing x-canvas-token." });
                    return;
                }
                const payload = await readJsonBody(req);
                const wantRoot = typeof payload?.packsRoot === "string" ? path.resolve(payload.packsRoot) : "";
                const huds = await discoverHuds({
                    activePacksRoot: state.packsRoot,
                    activeRepoPath: state.repoPath,
                });
                const matched = huds.find((h) => h.packsRoot === wantRoot || (payload?.id && h.id === payload.id));
                if (!matched) {
                    sendJson(res, 403, { ok: false, error: "Requested HUD is not in the discovered workspace." });
                    return;
                }
                const hudPacks = await discoverArtifactPacks({ packsRoot: matched.packsRoot });
                state.packsRoot = matched.packsRoot;
                state.repoPath = matched.repoPath || state.repoPath;
                state.deckDir = hudPacks[0]?.deckDir || null;
                state.title = hudPacks[0]?.title || `A-HUD — ${matched.name}`;
                state.liveA2ui = null;
                state.telemetryCache = null;
                state.telemetryCachedAt = 0;
                state.telemetryFingerprint = "";
                state.lastDeterministicResult = null;
                runtime.broadcast("deck_reloaded", {
                    packsRoot: state.packsRoot,
                    deckDir: state.deckDir,
                    hudId: matched.id,
                    title: state.title,
                    timestamp: new Date().toISOString(),
                });
                sendJson(res, 200, {
                    ok: true,
                    dispatchedToAgent: false,
                    hud: { ...matched, active: true },
                    activeDeckDir: state.deckDir,
                });
                return;
            }

            if (req.method === "GET" && url.pathname === "/packs") {
                const packs = await discoverArtifactPacks({
                    packsRoot: state.packsRoot,
                    activeDeckDir: state.deckDir,
                });
                sendJson(res, 200, {
                    ok: true,
                    packsRoot: state.packsRoot,
                    activeDeckDir: state.deckDir,
                    packs,
                });
                return;
            }

            if (req.method === "GET" && url.pathname === "/pack-data") {
                const data = state.deckDir
                    ? await buildPackRuntimeData(state.deckDir, state.title).catch(() => null)
                    : null;
                sendJson(res, 200, { ok: true, deckDir: state.deckDir, data });
                return;
            }

            if (req.method === "GET" && url.pathname === "/audio/models") {
                const info = await discoverAudioModels({
                    endpointBase: audioEndpointBase,
                    activeDeckDir: state.deckDir,
                    packsRoot: state.packsRoot,
                    ...(audioFetchImpl ? { fetchImpl: audioFetchImpl } : {}),
                });
                const activeJob = state.deckDir ? summarizeRegenJob(regenJobs.get(path.resolve(state.deckDir))) : null;
                sendJson(res, 200, {
                    ...info,
                    activeJob: activeJob && activeJob.status === "running" ? activeJob : null,
                });
                return;
            }

            if (req.method === "POST" && url.pathname === "/audio/regenerate") {
                const provided = req.headers["x-canvas-token"];
                if (!tokensMatch(token, typeof provided === "string" ? provided : "")) {
                    sendJson(res, 403, { ok: false, error: "Invalid or missing x-canvas-token." });
                    return;
                }
                const payload = await readJsonBody(req);
                const targetDir = typeof payload?.deckDir === "string" && payload.deckDir.trim()
                    ? path.resolve(payload.deckDir)
                    : state.deckDir;
                if (!targetDir) {
                    sendJson(res, 400, { ok: false, error: "No active presentation deck to regenerate." });
                    return;
                }
                const packs = await discoverArtifactPacks({
                    packsRoot: state.packsRoot,
                    activeDeckDir: state.deckDir,
                });
                const allowed = (state.deckDir && path.resolve(state.deckDir) === targetDir) ||
                    packs.some((p) => p.deckDir === targetDir);
                if (!allowed) {
                    sendJson(res, 403, { ok: false, error: "Target deckDir is outside the discovered workspace." });
                    return;
                }
                const model = typeof payload?.model === "string" ? payload.model.trim() : "";
                const voice = typeof payload?.voice === "string" ? payload.voice.trim() : "";
                if (!model || model === "__system__") {
                    sendJson(res, 400, { ok: false, error: "A valid oMLX TTS or Speech-to-Speech model is required." });
                    return;
                }
                if (!voice) {
                    sendJson(res, 400, { ok: false, error: "A speaker voice name is required." });
                    return;
                }
                const discovered = await discoverAudioModels({
                    endpointBase: audioEndpointBase,
                    activeDeckDir: targetDir,
                    packsRoot: state.packsRoot,
                    ...(audioFetchImpl ? { fetchImpl: audioFetchImpl } : {}),
                });
                const endpoint = typeof payload?.endpoint === "string" && payload.endpoint.trim()
                    ? payload.endpoint.trim()
                    : discovered.endpoint;
                if (!isAllowedAudioEndpoint(endpoint, discovered.baseUrl)) {
                    sendJson(res, 403, { ok: false, error: "Audio endpoint must be local loopback or the configured oMLX server." });
                    return;
                }
                const modelMeta = discovered.models.find((m) => m.id === model) || null;
                let firstSceneId = null;
                let totalScenes = 0;
                try {
                    const tl = JSON.parse(await readFile(path.join(targetDir, "timeline.json"), "utf8"));
                    const scs = Array.isArray(tl.scenes) ? tl.scenes : [];
                    totalScenes = scs.length;
                    firstSceneId = scs[0]?.id || null;
                } catch {
                    sendJson(res, 404, { ok: false, error: "timeline.json not found for target presentation." });
                    return;
                }
                const job = startBackgroundAudioRegen({
                    targetDeckDir: targetDir,
                    model,
                    voice,
                    endpoint,
                    supportsAnchor: modelMeta ? Boolean(modelMeta.supportsAnchor) : false,
                    firstSceneId,
                    totalScenes,
                });
                sendJson(res, 202, { ok: true, dispatchedToAgent: false, job });
                return;
            }

            if (req.method === "POST" && url.pathname === "/packs/select") {
                const provided = req.headers["x-canvas-token"];
                if (!tokensMatch(token, typeof provided === "string" ? provided : "")) {
                    sendJson(res, 403, { ok: false, error: "Invalid or missing x-canvas-token." });
                    return;
                }
                const payload = await readJsonBody(req);
                const wantDir = typeof payload?.deckDir === "string" ? path.resolve(payload.deckDir) : "";
                const packs = await discoverArtifactPacks({
                    packsRoot: state.packsRoot,
                    activeDeckDir: state.deckDir,
                });
                const matched = packs.find((p) => p.deckDir === wantDir || (payload?.id && p.id === payload.id));
                if (!matched) {
                    sendJson(res, 403, { ok: false, error: "Requested artifact pack is not in the discovered landscape." });
                    return;
                }
                state.deckDir = matched.deckDir;
                state.title = matched.title;
                state.liveA2ui = null;
                state.telemetryCache = null;
                state.telemetryCachedAt = 0;
                state.telemetryFingerprint = "";
                const data = await buildPackRuntimeData(matched.deckDir, matched.title).catch(() => null);
                // Broadcast pack_switched (in-place SPA swap, zero scroll jump) instead of full reload
                runtime.broadcast("pack_switched", {
                    deckDir: matched.deckDir,
                    packId: matched.id,
                    title: matched.title,
                    data,
                    timestamp: new Date().toISOString(),
                });
                sendJson(res, 200, {
                    ok: true,
                    dispatchedToAgent: false,
                    pack: { ...matched, active: true },
                    data,
                });
                return;
            }

            if (req.method === "POST" && url.pathname === "/a2ui/action") {
                const provided = req.headers["x-canvas-token"];
                if (!tokensMatch(token, typeof provided === "string" ? provided : "")) {
                    sendJson(res, 403, { ok: false, error: "Invalid or missing x-canvas-token." });
                    return;
                }
                const payload = await readJsonBody(req);
                const result = await dispatchA2uiCallback(payload, runtime);
                sendJson(res, result.status, result.body);
                return;
            }

            if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/slides.html")) {
                const html = state.deckDir
                    ? await readFile(path.join(state.deckDir, "slides.html"), "utf8")
                    : await renderEmptyLandingHtml({ title: state.title, repoPath: state.repoPath });
                const injected = injectBridgeBootstrap(html, {
                    version: "v1.0",
                    instanceId,
                    canvasId,
                    token,
                    actionUrl: "/a2ui/action",
                    eventsUrl: "/events",
                    stateUrl: "/state",
                    packsUrl: "/packs",
                    selectPackUrl: "/packs/select",
                    hudsUrl: "/huds",
                    selectHudUrl: "/huds/select",
                    telemetryUrl: "/telemetry",
                    audioModelsUrl: "/audio/models",
                    audioRegenerateUrl: "/audio/regenerate",
                });
                res.writeHead(200, {
                    "content-type": "text/html; charset=utf-8",
                    "cache-control": "no-store",
                });
                res.end(injected);
                return;
            }

            if (req.method === "GET" || req.method === "HEAD") {
                if (!state.deckDir) {
                    sendJson(res, 404, { ok: false, error: "No active deck loaded." });
                    return;
                }
                const assetPath = resolveSafeAsset(state.deckDir, url.pathname);
                if (!assetPath) {
                    sendJson(res, 403, { ok: false, error: "Forbidden path." });
                    return;
                }
                const st = await stat(assetPath).catch(() => null);
                if (!st || !st.isFile()) {
                    sendJson(res, 404, { ok: false, error: "Not found." });
                    return;
                }
                const ext = path.extname(assetPath).toLowerCase();
                const mime = MIME_BY_EXT[ext] || "application/octet-stream";
                const data = await readFile(assetPath);
                const cacheHdr = (ext === ".wav" || ext === ".mp4" || ext === ".webm") && url.searchParams.has("v")
                    ? "public, max-age=300"
                    : "no-store";
                // Support HTTP Range requests for smooth HTML5 <audio>/<video> seeking
                const range = req.headers.range;
                if (range && /^bytes=\d*-\d*$/.test(range)) {
                    const [startStr, endStr] = range.replace("bytes=", "").split("-");
                    const start = startStr ? Number.parseInt(startStr, 10) : 0;
                    const end = endStr ? Math.min(data.length - 1, Number.parseInt(endStr, 10)) : data.length - 1;
                    if (Number.isNaN(start) || Number.isNaN(end) || start > end || start >= data.length) {
                        res.writeHead(416, { "content-range": `bytes */${data.length}` });
                        res.end();
                        return;
                    }
                    const slice = data.subarray(start, end + 1);
                    res.writeHead(206, {
                        "content-type": mime,
                        "content-length": String(slice.length),
                        "content-range": `bytes ${start}-${end}/${data.length}`,
                        "accept-ranges": "bytes",
                        "cache-control": cacheHdr,
                    });
                    res.end(req.method === "HEAD" ? undefined : slice);
                    return;
                }
                res.writeHead(200, {
                    "content-type": mime,
                    "content-length": String(data.length),
                    "accept-ranges": "bytes",
                    "cache-control": cacheHdr,
                });
                res.end(req.method === "HEAD" ? undefined : data);
                return;
            }

            sendJson(res, 405, { ok: false, error: "Method not allowed." });
        } catch (error) {
            const status = typeof error?.statusCode === "number" ? error.statusCode : 500;
            sendJson(res, status, {
                ok: false,
                error: error instanceof Error ? error.message : String(error),
            });
        }
    });

    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = server.address().port;
    const url = `http://127.0.0.1:${port}/`;

    return {
        server,
        url,
        port,
        token,
        runtime,
        async close() {
            if (telemetryPoller) clearInterval(telemetryPoller);
            for (const job of regenJobs.values()) {
                job.superseded = true;
                if (job.child && !job.child.killed) {
                    try { job.child.kill("SIGTERM"); } catch {}
                }
            }
            regenJobs.clear();
            for (const client of clients) {
                try {
                    client.end();
                } catch {}
            }
            clients.clear();
            await new Promise((resolve) => server.close(() => resolve()));
        },
    };
}
