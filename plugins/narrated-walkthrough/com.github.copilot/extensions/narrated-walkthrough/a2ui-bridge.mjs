// Deterministic A2UI v1.0 callback router + loopback deck server for the
// narrated-walkthrough Copilot App Extension Canvas.
//
// Every UI click in the canvas iframe posts here BEFORE any AI agent turn:
//   1. CSRF nonce verification (fail closed, 403)
//   2. A2UI v1.0 payload validation & action allowlist (fail closed, 400)
//   3. Deterministic local handling (oversight, bookmarks, seek, audit log, SSE)
//   4. Structured session.send({ prompt, displayPrompt, mode }) when an AI turn
//      is required — waking the yielded Copilot agent deterministically.

import { randomBytes, timingSafeEqual } from "node:crypto";
import { appendFile, readdir, readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { collectLiveTelemetry } from "./live-telemetry.mjs";

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
    return "walkthrough";
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
        || (kind === "pr_review" ? "PR Review Agent" : kind === "standup" ? "Morning Standup Briefer" : "Walkthrough Agent");
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
    telemetryPollMs = 45000,
    telemetryTtlMs = 15000,
}) {
    const token = randomBytes(24).toString("hex");
    const clients = new Set();
    const resolvedDeck = path.resolve(deckDir);
    const resolvedPacksRoot = packsRoot ? path.resolve(packsRoot) : await inferPacksRoot(resolvedDeck);
    const state = {
        title,
        deckDir: resolvedDeck,
        packsRoot: resolvedPacksRoot,
        repoPath: repoPath ? path.resolve(repoPath) : null,
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
                runtime.broadcast("deck_reloaded", {
                    deckDir: matched.deckDir,
                    packId: matched.id,
                    title: matched.title,
                    timestamp: new Date().toISOString(),
                });
                sendJson(res, 200, {
                    ok: true,
                    dispatchedToAgent: false,
                    pack: { ...matched, active: true },
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
                const slidesFile = path.join(state.deckDir, "slides.html");
                const html = await readFile(slidesFile, "utf8");
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
                    telemetryUrl: "/telemetry",
                });
                res.writeHead(200, {
                    "content-type": "text/html; charset=utf-8",
                    "cache-control": "no-store",
                });
                res.end(injected);
                return;
            }

            if (req.method === "GET" || req.method === "HEAD") {
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
                        "cache-control": "no-store",
                    });
                    res.end(req.method === "HEAD" ? undefined : slice);
                    return;
                }
                res.writeHead(200, {
                    "content-type": mime,
                    "content-length": String(data.length),
                    "accept-ranges": "bytes",
                    "cache-control": "no-store",
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
