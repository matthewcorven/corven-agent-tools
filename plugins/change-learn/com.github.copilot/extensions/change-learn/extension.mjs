// Extension: change-learn
// Copilot canvas for the change-learn plugin: renders AI/deterministic change
// summaries, Mermaid lineage diagrams, and Marp deck previews produced by the
// change-learn skill and MCP host.

import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CanvasError, createCanvas, joinSession } from "@github/copilot-sdk/extension";
import {
    analyzeCommitRange,
    buildAnalyzePrompt,
    loadCommitRange,
    scaffoldDeckPreview,
} from "./git-range.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const indexPath = path.join(__dirname, "web", "index.html");
const pluginRoot = path.resolve(__dirname, "../../..");
const lineagePath = path.join(pluginRoot, "skills", "change-learn", "scripts", "lineage.py");
const deckPath = path.join(pluginRoot, "skills", "change-learn", "scripts", "deck.py");

// One local loopback server + content state per open canvas instance.
const servers = new Map();
let session;

function emptyState() {
    return {
        title: "Change Learn",
        summaryMarkdown: "",
        diagrams: [], // { title, code } — Mermaid source
        deckMarkdown: "",
        deckPath: "",
        repository: "",
        commits: [],
        selectedRange: undefined,
        statusMessage: "",
    };
}

function sendJson(res, value, status = 200) {
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

function broadcast(entry, event, data) {
    for (const client of entry.clients) {
        sendSse(client, event, data);
    }
}

function normalizeDiagrams(input) {
    if (!Array.isArray(input)) return [];
    return input
        .filter((d) => d && typeof d.code === "string" && d.code.trim().length > 0)
        .map((d, i) => ({
            title: typeof d.title === "string" && d.title.trim() ? d.title.trim() : `Diagram ${i + 1}`,
            code: d.code,
        }));
}

async function readDeckFile(deckPath) {
    if (typeof deckPath !== "string" || !path.isAbsolute(deckPath)) {
        throw new CanvasError("invalid_deck_path", "deckPath must be an absolute path to a Marp markdown file.");
    }
    if (!deckPath.endsWith(".md")) {
        throw new CanvasError("invalid_deck_path", "deckPath must point to a .md Marp deck.");
    }
    try {
        return await readFile(deckPath, "utf8");
    } catch (error) {
        throw new CanvasError("deck_read_failed", `Could not read deck at ${deckPath}: ${error.message}`);
    }
}

function rangeSummary(analysis, from, to) {
    const areas = analysis.areas ?? [];
    const areaLines = areas.slice(0, 6).map((area) =>
        `- **${area.area}**: ${area.files} files, +${area.adds} / -${area.dels}`,
    );
    return [
        `## ${from.shortId} to ${to.shortId}`,
        "",
        `**${analysis.totals?.commits ?? 0} commits** across **${analysis.totals?.files ?? 0} files** `
            + `(+${analysis.totals?.adds ?? 0} / -${analysis.totals?.dels ?? 0}).`,
        "",
        `From: \`${from.shortId}\` — ${from.subject}`,
        `To: \`${to.shortId}\` — ${to.subject}`,
        ...(areaLines.length ? ["", "### Areas", ...areaLines] : []),
    ].join("\n");
}

function selectedCommit(entry, id) {
    return entry.state.commits.find((commit) => commit.id === id);
}

function directDiagrams(analysis) {
    const commits = analysis.commits ?? [];
    const areas = (analysis.areas ?? [])
        .filter((area) => (area.adds ?? 0) + (area.dels ?? 0) > 0)
        .slice(0, 8);
    const diagrams = [];

    if (commits.length) {
        const lines = ["gitGraph"];
        for (const commit of [...commits].reverse().slice(-12)) {
            lines.push(`  commit id: "${commit.sha}"`);
        }
        diagrams.push({ title: "Selected commit span", code: lines.join("\n") });
    }
    if (areas.length) {
        const lines = ["pie showData", "  title Changed lines by area"];
        for (const area of areas) {
            lines.push(`  ${JSON.stringify(area.area)} : ${(area.adds ?? 0) + (area.dels ?? 0)}`);
        }
        diagrams.push({ title: "Change distribution", code: lines.join("\n") });
    }
    return diagrams;
}

async function refreshCommits(entry) {
    const range = await loadCommitRange(entry.repository);
    entry.repository = range.repository;
    entry.state.repository = range.repository;
    entry.state.commits = range.commits;

    if (
        entry.state.selectedRange
        && (!selectedCommit(entry, entry.state.selectedRange.from)
            || !selectedCommit(entry, entry.state.selectedRange.to))
    ) {
        entry.state.selectedRange = undefined;
    }
}

async function analyzeRange(entry, input, { triggerAgent = false } = {}) {
    const from = typeof input?.from === "string" ? input.from : "";
    const to = typeof input?.to === "string" ? input.to : "";
    const analysis = await analyzeCommitRange({
        repository: entry.repository,
        scriptPath: lineagePath,
        from,
        to,
        allowedCommitIds: new Set(entry.state.commits.map((commit) => commit.id)),
    });
    const fromCommit = selectedCommit(entry, from);
    const toCommit = selectedCommit(entry, to);
    const deckMarkdown = await scaffoldDeckPreview({
        scriptPath: deckPath,
        analysis,
        title: `Change Learn: ${fromCommit.shortId} to ${toCommit.shortId}`,
        theme: typeof analysis.config?.deck?.theme === "string"
            ? analysis.config.deck.theme
            : undefined,
    });
    entry.state.selectedRange = { from, to };
    entry.state.summaryMarkdown = rangeSummary(analysis, fromCommit, toCommit);
    entry.state.diagrams = directDiagrams(analysis);
    entry.state.deckMarkdown = deckMarkdown;
    entry.state.deckPath = "";
    if (triggerAgent) {
        entry.state.statusMessage = `Full AI analysis requested in session for ${fromCommit.shortId}..${toCommit.shortId}.`;
    }
    broadcast(entry, "state", entry.state);

    if (triggerAgent && session && typeof session.send === "function") {
        try {
            await session.send({
                prompt: buildAnalyzePrompt({
                    instanceId: entry.instanceId || "change-learn",
                    repository: entry.repository,
                    fromCommit,
                    toCommit,
                    analysis,
                }),
                displayPrompt: `Change Learn: Analyze commit span ${fromCommit.shortId}..${toCommit.shortId}`,
            });
        } catch (error) {
            entry.state.statusMessage = `Baseline generated. Could not dispatch to session: ${error.message}`;
            broadcast(entry, "state", entry.state);
        }
    }

    return analysis;
}

async function applyInput(entry, input) {
    if (!input || typeof input !== "object") return;
    if (typeof input.title === "string" && input.title.trim()) entry.state.title = input.title.trim();
    if (typeof input.summaryMarkdown === "string") {
        entry.state.summaryMarkdown = input.summaryMarkdown;
        entry.state.statusMessage = "";
    }
    if (input.diagrams !== undefined) entry.state.diagrams = normalizeDiagrams(input.diagrams);
    if (typeof input.deckMarkdown === "string" && input.deckMarkdown.trim()) {
        entry.state.deckMarkdown = input.deckMarkdown;
        entry.state.deckPath = "";
        entry.state.statusMessage = "";
    } else if (typeof input.deckPath === "string" && input.deckPath.trim()) {
        entry.state.deckMarkdown = await readDeckFile(input.deckPath.trim());
        entry.state.deckPath = input.deckPath.trim();
        entry.state.statusMessage = "";
    }
}

async function readJson(req) {
    const chunks = [];
    let length = 0;
    for await (const chunk of req) {
        length += chunk.length;
        if (length > 8_192) {
            throw new CanvasError("request_too_large", "Canvas request exceeds 8 KiB.");
        }
        chunks.push(chunk);
    }
    try {
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch {
        throw new CanvasError("invalid_request", "Canvas request must be valid JSON.");
    }
}

async function handleRequest(entry, req, res) {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");

    if (url.pathname === "/events") {
        res.writeHead(200, {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
            connection: "keep-alive",
        });
        entry.clients.add(res);
        sendSse(res, "state", entry.state);
        req.on("close", () => entry.clients.delete(res));
        return;
    }

    if (url.pathname === "/state") {
        sendJson(res, entry.state);
        return;
    }

    if (req.method === "POST" && url.pathname === "/analyze") {
        await analyzeRange(entry, await readJson(req), { triggerAgent: true });
        sendJson(res, entry.state);
        return;
    }

    if (req.method === "POST" && url.pathname === "/refresh") {
        await refreshCommits(entry);
        broadcast(entry, "state", entry.state);
        sendJson(res, entry.state);
        return;
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
        const html = await readFile(indexPath, "utf8");
        res.writeHead(200, {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-cache",
        });
        res.end(html);
        return;
    }

    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("Not found");
}

async function startServer(workingDirectory, instanceId) {
    const entry = {
        instanceId,
        clients: new Set(),
        state: emptyState(),
        repository: "",
        server: undefined,
        url: undefined,
    };
    try {
        const range = await loadCommitRange(workingDirectory);
        entry.repository = range.repository;
        entry.state.repository = range.repository;
        entry.state.commits = range.commits;
    } catch (error) {
        throw new CanvasError(
            "git_repository_unavailable",
            `Could not load commits from the active repository: ${error.message}`,
        );
    }
    const server = createServer((req, res) => {
        handleRequest(entry, req, res).catch((error) => {
            sendJson(res, {
                error: error instanceof Error ? error.message : "Change Learn canvas request failed.",
            }, 400);
        });
    });
    entry.server = server;
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    entry.url = `http://127.0.0.1:${port}/`;
    return entry;
}

function getOpenEntry(instanceId) {
    const entry = servers.get(instanceId);
    if (!entry) {
        throw new CanvasError("canvas_not_open", "Open the Change Learn canvas before invoking this action.");
    }
    return entry;
}

const diagramSchema = {
    type: "array",
    description: "Mermaid diagrams to render.",
    items: {
        type: "object",
        properties: {
            title: { type: "string", description: "Diagram heading." },
            code: { type: "string", description: "Mermaid source (plain text, no code fences)." },
        },
        required: ["code"],
        additionalProperties: false,
    },
};

session = await joinSession({
    canvases: [
        createCanvas({
            id: "change-learn",
            displayName: "Change Learn",
            description:
                "Visualizes source-control change windows from the change-learn plugin: an AI or deterministic change summary, Mermaid lineage/impact diagrams, and a Marp deck preview with slide navigation. Feed it output from the change-learn skill or MCP host tools.",
            inputSchema: {
                type: "object",
                properties: {
                    title: { type: "string", description: "Canvas title, e.g. the change window label." },
                    summaryMarkdown: { type: "string", description: "Markdown change summary to show." },
                    diagrams: diagramSchema,
                    deckMarkdown: { type: "string", description: "Marp deck markdown to preview." },
                    deckPath: { type: "string", description: "Absolute path to a Marp .md deck to load." },
                },
                additionalProperties: false,
            },
            actions: [
                {
                    name: "show_changes",
                    description:
                        "Replace the canvas content with a change summary and/or Mermaid diagrams. Pass summaryMarkdown from summarize_changes and diagram code from the diagram tools (no code fences).",
                    inputSchema: {
                        type: "object",
                        properties: {
                            title: { type: "string", description: "Canvas title, e.g. the change window label." },
                            summaryMarkdown: { type: "string", description: "Markdown change summary." },
                            diagrams: diagramSchema,
                        },
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = getOpenEntry(ctx.instanceId);
                        await applyInput(entry, ctx.input);
                        broadcast(entry, "state", entry.state);
                        return {
                            title: entry.state.title,
                            hasSummary: entry.state.summaryMarkdown.length > 0,
                            diagramCount: entry.state.diagrams.length,
                        };
                    },
                },
                {
                    name: "refresh_commits",
                    description:
                        "Refresh the commit, branch, and available pull-request metadata in an open Change Learn canvas.",
                    handler: async (ctx) => {
                        const entry = getOpenEntry(ctx.instanceId);
                        await refreshCommits(entry);
                        broadcast(entry, "state", entry.state);
                        return {
                            repository: entry.repository,
                            commits: entry.state.commits.length,
                        };
                    },
                },
                {
                    name: "analyze_range",
                    description:
                        "Analyze a selected ancestor commit span directly through Change Learn's local script path, without MCP tool dispatch.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            from: { type: "string", description: "Full commit SHA from the canvas From selector." },
                            to: { type: "string", description: "Full commit SHA from the canvas To selector." },
                            triggerAgent: { type: "boolean", description: "Whether to dispatch the full AI workflow to the Copilot session." },
                        },
                        required: ["from", "to"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = getOpenEntry(ctx.instanceId);
                        entry.instanceId = ctx.instanceId;
                        try {
                            const analysis = await analyzeRange(entry, ctx.input, {
                                triggerAgent: Boolean(ctx.input?.triggerAgent),
                            });
                            return {
                                from: analysis.spec?.from,
                                to: analysis.spec?.to,
                                totals: analysis.totals,
                            };
                        } catch (error) {
                            throw new CanvasError(
                                "range_analysis_failed",
                                error instanceof Error ? error.message : "Could not analyze the selected commit range.",
                            );
                        }
                    },
                },
                {
                    name: "load_deck",
                    description:
                        "Load a Marp deck into the canvas slide previewer, either from an absolute .md file path (e.g. a deck scaffolded by deck.py or export_deck) or from inline markdown.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            deckPath: { type: "string", description: "Absolute path to a Marp .md deck." },
                            deckMarkdown: { type: "string", description: "Inline Marp deck markdown." },
                        },
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = getOpenEntry(ctx.instanceId);
                        const { deckPath, deckMarkdown } = ctx.input ?? {};
                        if (!deckPath && !deckMarkdown) {
                            throw new CanvasError("missing_deck", "Provide deckPath or deckMarkdown.");
                        }
                        await applyInput(entry, { deckPath, deckMarkdown });
                        broadcast(entry, "state", entry.state);
                        broadcast(entry, "showTab", { tab: "deck" });
                        return {
                            deckPath: entry.state.deckPath || undefined,
                            deckChars: entry.state.deckMarkdown.length,
                        };
                    },
                },
                {
                    name: "get_state",
                    description: "Report what the Change Learn canvas is currently showing.",
                    handler: (ctx) => {
                        const entry = getOpenEntry(ctx.instanceId);
                        return {
                            title: entry.state.title,
                            hasSummary: entry.state.summaryMarkdown.length > 0,
                            diagramCount: entry.state.diagrams.length,
                            diagramTitles: entry.state.diagrams.map((d) => d.title),
                            hasDeck: entry.state.deckMarkdown.length > 0,
                            deckPath: entry.state.deckPath || undefined,
                        };
                    },
                },
            ],
            open: async (ctx) => {
                let entry = servers.get(ctx.instanceId);
                if (!entry) {
                    const workingDirectory = ctx.session?.workingDirectory;
                    if (!workingDirectory) {
                        throw new CanvasError(
                            "workspace_unavailable",
                            "The active session working directory is unavailable.",
                        );
                    }
                    entry = await startServer(workingDirectory, ctx.instanceId);
                    servers.set(ctx.instanceId, entry);
                } else {
                    entry.instanceId = ctx.instanceId;
                }
                await applyInput(entry, ctx.input);
                broadcast(entry, "state", entry.state);
                return {
                    title: entry.state.title,
                    status: entry.state.diagrams.length
                        ? `${entry.state.diagrams.length} diagram(s)`
                        : "Ready",
                    url: entry.url,
                };
            },
            onClose: async (ctx) => {
                const entry = servers.get(ctx.instanceId);
                if (!entry) return;
                servers.delete(ctx.instanceId);
                for (const client of entry.clients) {
                    client.end();
                }
                await new Promise((resolve) => entry.server.close(() => resolve()));
            },
        }),
    ],
});
