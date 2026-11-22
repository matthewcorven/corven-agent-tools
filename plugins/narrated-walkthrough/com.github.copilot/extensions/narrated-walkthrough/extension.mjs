// Extension: narrated-walkthrough
// Copilot App native Extension Canvas for narrated-walkthrough decks.
// Hosts built decks on loopback HTTP with a deterministic JS A2UI v1.0 callback
// bridge that validates UI clicks and wakes the yielded Copilot session via
// session.send(), while streaming agent status and live A2UI updates back over SSE.

import { readFile, stat } from "node:fs/promises";
import path from "node:path";

import { CanvasError, createCanvas, joinSession } from "@github/copilot-sdk/extension";
import { startWalkthroughServer } from "./a2ui-bridge.mjs";

const instances = new Map(); // instanceId -> server entry
let session;

async function validateDeckDir(deckDir) {
    if (typeof deckDir !== "string" || !path.isAbsolute(deckDir)) {
        throw new CanvasError("invalid_deck_dir", "deckDir must be an absolute path to a built walkthrough folder.");
    }
    const resolved = path.resolve(deckDir);
    const slidesPath = path.join(resolved, "slides.html");
    const st = await stat(slidesPath).catch(() => null);
    if (!st || !st.isFile()) {
        throw new CanvasError(
            "deck_not_found",
            `No built slides.html found in ${resolved}. Run build_narration.py first.`,
        );
    }
    let title = path.basename(resolved);
    try {
        const tl = JSON.parse(await readFile(path.join(resolved, "timeline.json"), "utf8"));
        if (typeof tl?.title === "string" && tl.title.trim()) {
            title = tl.title.trim();
        }
    } catch {
        // Optional timeline.json metadata
    }
    return { resolved, title };
}

function getInstance(instanceId) {
    const entry = instances.get(instanceId);
    if (!entry) {
        throw new CanvasError("canvas_not_open", `No open narrated-walkthrough canvas for instance '${instanceId}'.`);
    }
    return entry;
}

session = await joinSession({
    canvases: [
        createCanvas({
            id: "narrated-walkthrough",
            displayName: "Narrated Walkthrough",
            description:
                "Interactive narrated walkthrough with Architect HUD, sticky minimal media controls, and a live deterministic A2UI v1.0 callback bridge back to the Copilot agent.",
            inputSchema: {
                type: "object",
                properties: {
                    deckDir: {
                        type: "string",
                        description: "Absolute path to a built walkthrough directory containing slides.html and audio/.",
                    },
                    title: {
                        type: "string",
                        description: "Optional display title override shown in host chrome.",
                    },
                },
                required: ["deckDir"],
                additionalProperties: false,
            },
            actions: [
                {
                    name: "load_deck",
                    description: "Load or reload a built narrated walkthrough directory into this open canvas.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            deckDir: {
                                type: "string",
                                description: "Absolute path to the built walkthrough directory.",
                            },
                            title: {
                                type: "string",
                                description: "Optional display title override.",
                            },
                        },
                        required: ["deckDir"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = getInstance(ctx.instanceId);
                        const { resolved, title } = await validateDeckDir(ctx.input?.deckDir);
                        entry.runtime.state.deckDir = resolved;
                        entry.runtime.state.title = ctx.input?.title?.trim() || title;
                        entry.runtime.state.liveA2ui = null;
                        entry.runtime.broadcast("deck_reloaded", {
                            deckDir: resolved,
                            title: entry.runtime.state.title,
                            timestamp: new Date().toISOString(),
                        });
                        return {
                            deckDir: resolved,
                            title: entry.runtime.state.title,
                            url: entry.url,
                        };
                    },
                },
                {
                    name: "push_a2ui",
                    description:
                        "Push a fresh A2UI v1.0 message array live into the open canvas's action surface over SSE without interrupting audio playback.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            messages: {
                                type: "array",
                                minItems: 1,
                                description: "A2UI v1.0 JSON messages (createSurface, updateComponents, updateDataModel).",
                                items: { type: "object" },
                            },
                            statusNote: {
                                type: "string",
                                description: "Optional short status line displayed above the updated A2UI surface.",
                            },
                        },
                        required: ["messages"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = getInstance(ctx.instanceId);
                        const messages = ctx.input?.messages;
                        if (!Array.isArray(messages) || messages.length === 0) {
                            throw new CanvasError("invalid_a2ui", "messages must be a non-empty A2UI v1.0 array.");
                        }
                        const payload = {
                            messages,
                            statusNote: typeof ctx.input?.statusNote === "string" ? ctx.input.statusNote : "",
                            timestamp: new Date().toISOString(),
                        };
                        entry.runtime.state.liveA2ui = payload;
                        entry.runtime.broadcast("a2ui_update", payload);
                        return {
                            pushed: true,
                            messageCount: messages.length,
                            connectedClients: entry.runtime.clients.size,
                        };
                    },
                },
                {
                    name: "get_state",
                    description:
                        "Inspect the open Narrated Walkthrough canvas: active deckDir, connected SSE clients, agent status, and recent A2UI callbacks.",
                    handler: async (ctx) => {
                        const entry = getInstance(ctx.instanceId);
                        return {
                            instanceId: ctx.instanceId,
                            url: entry.url,
                            deckDir: entry.runtime.state.deckDir,
                            title: entry.runtime.state.title,
                            oversight: entry.runtime.state.oversight,
                            agentStatus: entry.runtime.state.agentStatus,
                            connectedClients: entry.runtime.clients.size,
                            recentCallbacks: entry.runtime.state.recentCallbacks.slice(-10),
                        };
                    },
                },
            ],
            open: async (ctx) => {
                const { resolved, title } = await validateDeckDir(ctx.input?.deckDir);
                const displayTitle = ctx.input?.title?.trim() || title;
                let entry = instances.get(ctx.instanceId);
                if (!entry) {
                    entry = await startWalkthroughServer({
                        instanceId: ctx.instanceId,
                        canvasId: ctx.canvasId,
                        deckDir: resolved,
                        title: displayTitle,
                        session,
                    });
                    instances.set(ctx.instanceId, entry);
                } else {
                    entry.runtime.state.deckDir = resolved;
                    entry.runtime.state.title = displayTitle;
                    entry.runtime.session = session;
                    entry.runtime.broadcast("deck_reloaded", {
                        deckDir: resolved,
                        title: displayTitle,
                        timestamp: new Date().toISOString(),
                    });
                }
                return {
                    url: entry.url,
                    title: displayTitle,
                    status: "Live A2UI bridge ready",
                };
            },
            onClose: async (ctx) => {
                const entry = instances.get(ctx.instanceId);
                if (!entry) return;
                instances.delete(ctx.instanceId);
                await entry.close();
            },
        }),
    ],
});

// Mirror live agent turn status back into every open walkthrough canvas over SSE
if (session && typeof session.on === "function") {
    session.on((event) => {
        if (!event || typeof event.type !== "string") return;
        let nextStatus = null;
        if (event.type === "assistant.turn_start") nextStatus = "thinking";
        else if (event.type === "assistant.message") nextStatus = "responded";
        else if (event.type === "session.idle") nextStatus = "ready";
        if (!nextStatus) return;

        for (const entry of instances.values()) {
            entry.runtime.state.agentStatus = nextStatus;
            entry.runtime.broadcast("agent_status", {
                status: nextStatus,
                eventType: event.type,
                timestamp: new Date().toISOString(),
            });
        }
    });
}
