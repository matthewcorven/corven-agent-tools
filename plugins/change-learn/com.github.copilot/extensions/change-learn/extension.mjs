// Extension: change-learn
// Copilot canvas for the change-learn plugin: renders AI/deterministic change
// summaries, Mermaid lineage diagrams, and Marp deck previews produced by the
// change-learn skill and MCP host.

import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CanvasError, createCanvas, joinSession } from "@github/copilot-sdk/extension";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const indexPath = path.join(__dirname, "web", "index.html");

// One local loopback server + content state per open canvas instance.
const servers = new Map();

function emptyState() {
    return {
        title: "Change Learn",
        summaryMarkdown: "",
        diagrams: [], // { title, code } — Mermaid source
        deckMarkdown: "",
        deckPath: "",
    };
}

function sendJson(res, value) {
    res.writeHead(200, {
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

async function applyInput(entry, input) {
    if (!input || typeof input !== "object") return;
    if (typeof input.title === "string" && input.title.trim()) entry.state.title = input.title.trim();
    if (typeof input.summaryMarkdown === "string") entry.state.summaryMarkdown = input.summaryMarkdown;
    if (input.diagrams !== undefined) entry.state.diagrams = normalizeDiagrams(input.diagrams);
    if (typeof input.deckMarkdown === "string" && input.deckMarkdown.trim()) {
        entry.state.deckMarkdown = input.deckMarkdown;
        entry.state.deckPath = "";
    } else if (typeof input.deckPath === "string" && input.deckPath.trim()) {
        entry.state.deckMarkdown = await readDeckFile(input.deckPath.trim());
        entry.state.deckPath = input.deckPath.trim();
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

async function startServer() {
    const entry = {
        clients: new Set(),
        state: emptyState(),
        server: undefined,
        url: undefined,
    };
    const server = createServer((req, res) => {
        handleRequest(entry, req, res).catch((error) => {
            res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
            res.end(error instanceof Error ? error.message : "change-learn canvas server error");
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

await joinSession({
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
                    entry = await startServer();
                    servers.set(ctx.instanceId, entry);
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
