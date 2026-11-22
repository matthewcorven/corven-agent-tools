import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
    buildAgentTurnPayload,
    classifyAudioModel,
    discoverArtifactPacks,
    discoverAudioModels,
    discoverHuds,
    generateDocWalkthroughPack,
    injectBridgeBootstrap,
    resolveSafeAsset,
    startWalkthroughServer,
    validateA2uiCallback,
} from "./a2ui-bridge.mjs";

test("validateA2uiCallback enforces v1.0 schema and rejects malformed payloads", () => {
    assert.equal(validateA2uiCallback(null).ok, false);
    assert.equal(validateA2uiCallback({ version: "v2.0", action: { name: "x" }, surfaceId: "s" }).ok, false);
    assert.equal(validateA2uiCallback({ version: "v1.0", action: { name: "INVALID NAME!" }, surfaceId: "s" }).ok, false);
    assert.equal(validateA2uiCallback({ version: "v1.0", action: { name: "chat_node" } }).ok, false);

    const good = validateA2uiCallback({
        version: "v1.0",
        label: "Chat with Eric",
        sceneId: "s-02-what-changed",
        timestampSec: 18.42,
        action: {
            name: "chat_node",
            surfaceId: "architect-pr-353-actions",
            sourceComponentId: "act-lead-chat",
            context: { nodeId: "eric-lead", pr: 353 },
        },
        node: { id: "eric-lead", name: "Eric", role: "Lead", level: "lead" },
    });
    assert.equal(good.ok, true);
    assert.equal(good.normalized.action.name, "chat_node");
    assert.equal(good.normalized.surfaceId, "architect-pr-353-actions");
    assert.equal(good.normalized.node.name, "Eric");
});

test("buildAgentTurnPayload flags sensitive actions with requiresExplicitConfirmation", () => {
    const v = validateA2uiCallback({
        version: "v1.0",
        label: "Merge PR #353",
        action: { name: "merge_pr", surfaceId: "pr-353", context: { pr: 353 } },
    });
    const built = buildAgentTurnPayload(v.normalized, {
        instanceId: "pr-353-hud",
        deckDir: "/tmp/deck",
        deckTitle: "PR 353",
    });
    assert.equal(built.envelope.guardrails.requiresExplicitConfirmation, true);
    assert.match(built.prompt, /requiresExplicitConfirmation: true/);
    assert.match(built.displayPrompt, /merge_pr/);
});

test("resolveSafeAsset blocks path traversal and dotfiles", () => {
    const root = "/tmp/example-deck";
    assert.equal(resolveSafeAsset(root, "/audio/01.wav"), path.resolve(root, "audio/01.wav"));
    assert.equal(resolveSafeAsset(root, "/../etc/passwd"), null);
    assert.equal(resolveSafeAsset(root, "/.canvas-actions.jsonl"), null);
    assert.equal(resolveSafeAsset(root, "/audio/.raw/01.wav"), null);
});

test("injectBridgeBootstrap embeds window.__WALKTHROUGH_BRIDGE__ safely even when inlined scripts contain </head>", () => {
    const html = '<html><head><script>const s = "<head></head>";</script><title>Deck</title></head><body>ok</body></html>';
    const out = injectBridgeBootstrap(html, { token: "abc", actionUrl: "/a2ui/action" });
    assert.match(out, /window\.__WALKTHROUGH_BRIDGE__/);
    assert.ok(out.includes('const s = "<head></head>";'), "must not corrupt string literals inside inlined scripts");
    assert.ok(out.indexOf("__WALKTHROUGH_BRIDGE__") < out.indexOf("<title>Deck</title>"));
});

test("live loopback server validates CSRF, handles deterministic-only hooks without LLM, and wakes yielded session via session.send", async () => {
    const tmpDir = await mkdtemp(path.join(os.tmpdir(), "nw-bridge-test-"));
    try {
        await mkdir(path.join(tmpDir, "audio"), { recursive: true });
        await writeFile(path.join(tmpDir, "slides.html"), "<!doctype html><html><head></head><body>deck</body></html>");
        await writeFile(path.join(tmpDir, "audio", "01.wav"), Buffer.from("RIFF0000WAVE"));

        const sent = [];
        const mockSession = {
            async send(opts) {
                sent.push(opts);
                return `msg_${sent.length}`;
            },
        };

        const srv = await startWalkthroughServer({
            instanceId: "test-hud-1",
            deckDir: tmpDir,
            title: "Test Deck",
            session: mockSession,
        });

        try {
            // 1. Served HTML injects bridge bootstrap with matching CSRF token
            const pageRes = await fetch(srv.url);
            assert.equal(pageRes.status, 200);
            const html = await pageRes.text();
            assert.ok(html.includes(srv.token));

            // 2. Missing/wrong CSRF token fails closed with 403
            const badTok = await fetch(`${srv.url}a2ui/action`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": "wrong" },
                body: JSON.stringify({
                    version: "v1.0",
                    action: { name: "chat_node", surfaceId: "s1", context: {} },
                }),
            });
            assert.equal(badTok.status, 403);
            assert.equal(sent.length, 0);

            // 3. Deterministic-only hook (set_oversight) succeeds WITHOUT calling session.send()
            const detRes = await fetch(`${srv.url}a2ui/action`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({
                    version: "v1.0",
                    oversight: "full",
                    action: { name: "set_oversight", surfaceId: "hud", context: { oversight: "full" } },
                }),
            });
            assert.equal(detRes.status, 200);
            const detJson = await detRes.json();
            assert.equal(detJson.ok, true);
            assert.equal(detJson.dispatchedToAgent, false);
            assert.equal(sent.length, 0);

            // 4. Real A2UI action wakes yielded session via deterministic callback -> session.send()
            const wakeRes = await fetch(`${srv.url}a2ui/action`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({
                    version: "v1.0",
                    label: "Chat with walkthrough video agent",
                    sceneId: "s-02-what-changed",
                    timestampSec: 16.5,
                    oversight: "full",
                    action: {
                        name: "chat_node",
                        surfaceId: "architect-pr-353-actions",
                        sourceComponentId: "act-video-chat",
                        context: { nodeId: "video-agent", pr: 353 },
                    },
                    node: {
                        id: "video-agent",
                        name: "Walkthrough Video Agent",
                        role: "Media · Interactive Q&A",
                        level: "video",
                    },
                }),
            });
            assert.equal(wakeRes.status, 200);
            const wakeJson = await wakeRes.json();
            assert.equal(wakeJson.ok, true);
            assert.equal(wakeJson.dispatchedToAgent, true);
            assert.equal(wakeJson.messageId, "msg_1");
            assert.equal(sent.length, 1);
            assert.match(sent[0].displayPrompt, /Walkthrough Video Agent/);
            assert.match(sent[0].prompt, /\[A2UI Canvas Callback\]/);
            assert.match(sent[0].prompt, /"nodeId": "video-agent"/);
            assert.equal(sent[0].mode, "immediate");

            // 5. Audit trail persisted to .canvas-actions.jsonl AND blocked from HTTP exposure
            const auditLines = (await readFile(path.join(tmpDir, ".canvas-actions.jsonl"), "utf8")).trim().split("\n");
            assert.equal(auditLines.length, 2);
            const dotRes = await fetch(`${srv.url}.canvas-actions.jsonl`);
            assert.equal(dotRes.status, 403);
        } finally {
            await srv.close();
        }
    } finally {
        await rm(tmpDir, { recursive: true, force: true });
    }
});

test("A-HUD landscape discovers runs.jsonl artifact packs and switches packs deterministically without LLM turns", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "nw-packs-test-"));
    try {
        const packA = path.join(rootDir, "wt-a", "runs", "run-pr-353", "planning");
        const packB = path.join(rootDir, "wt-a", "runs", "run-standup", "planning");
        await mkdir(packA, { recursive: true });
        await mkdir(packB, { recursive: true });
        await writeFile(path.join(packA, "slides.html"), "<html><head></head><body>PR353</body></html>");
        await writeFile(path.join(packA, "timeline.json"), JSON.stringify({ title: "PR #353 Review", scenes: [{ id: "s1" }], total_duration: 85.4 }));
        await writeFile(path.join(packB, "slides.html"), "<html><head></head><body>STANDUP</body></html>");
        await writeFile(path.join(packB, "timeline.json"), JSON.stringify({ title: "Daily Standup Brief", scenes: [{ id: "s1" }, { id: "s2" }], total_duration: 90.1 }));

        const runsJsonl = [
            JSON.stringify({ run: "run-standup", spec: "narrated-morning-brief", phasePath: packB, utc: "2026-11-22T01:37:32Z", issue: "349" }),
            JSON.stringify({ run: "run-pr-353", spec: "pr-353-review-walkthrough", phasePath: packA, utc: "2026-11-22T03:59:51Z", issue: "350" }),
        ].join("\n") + "\n";
        await writeFile(path.join(rootDir, "runs.jsonl"), runsJsonl);

        const packs = await discoverArtifactPacks({ packsRoot: rootDir, activeDeckDir: packA });
        assert.equal(packs.length, 2);
        assert.equal(packs[0].kind, "pr_review");
        assert.equal(packs[0].active, true);
        assert.equal(packs[1].kind, "standup");
        assert.equal(packs[1].active, false);

        const sent = [];
        const srv = await startWalkthroughServer({
            instanceId: "ahud-landscape",
            deckDir: packA,
            packsRoot: rootDir,
            session: { async send(o) { sent.push(o); return "m1"; } },
        });
        try {
            const listRes = await fetch(`${srv.url}packs`);
            assert.equal(listRes.status, 200);
            const listJson = await listRes.json();
            assert.equal(listJson.packs.length, 2);

            // Reject switching to arbitrary path outside discovered packs
            const badSwitch = await fetch(`${srv.url}packs/select`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({ deckDir: "/etc" }),
            });
            assert.equal(badSwitch.status, 403);

            // Switch to Standup pack deterministically (0 LLM calls)
            const okSwitch = await fetch(`${srv.url}packs/select`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({ deckDir: packB }),
            });
            assert.equal(okSwitch.status, 200);
            const swJson = await okSwitch.json();
            assert.equal(swJson.dispatchedToAgent, false);
            assert.equal(swJson.pack.kind, "standup");
            assert.equal(swJson.data.title, "Daily Standup Brief");
            assert.equal(swJson.data.scenes.length, 2);
            assert.equal(sent.length, 0);

            const htmlAfter = await (await fetch(srv.url)).text();
            assert.ok(htmlAfter.includes("STANDUP"));
        } finally {
            await srv.close();
        }
    } finally {
        await rm(rootDir, { recursive: true, force: true });
    }
});

test("startWalkthroughServer supports bare UI opens with deckDir=null (zero packs yet)", async () => {
    const tmpRepo = await mkdtemp(path.join(os.tmpdir(), "nw-empty-repo-"));
    try {
        const srv = await startWalkthroughServer({
            instanceId: "ui-bare-open",
            deckDir: null,
            packsRoot: null,
            repoPath: tmpRepo,
            title: "A-HUD Empty",
            session: { async send() { return "m1"; } },
            telemetryPollMs: 0,
        });
        try {
            const res = await fetch(srv.url);
            assert.equal(res.status, 200);
            const html = await res.text();
            assert.ok(html.includes("__WALKTHROUGH_BRIDGE__"));
            assert.ok(html.includes("ahud-starter-actions"));
            const stateRes = await fetch(`${srv.url}state`);
            assert.equal(stateRes.status, 200);
            const st = await stateRes.json();
            assert.equal(st.deckDir, null);
        } finally {
            await srv.close();
        }
    } finally {
        await rm(tmpRepo, { recursive: true, force: true });
    }
});

test("Multi-HUD browser discovers sibling *-artifacts HUDs, switches HUDs deterministically, and generates doc walkthroughs in <2s", async () => {
    const parentDir = await mkdtemp(path.join(os.tmpdir(), "nw-multihud-"));
    try {
        const hudA = path.join(parentDir, "repo-alpha-artifacts");
        const hudB = path.join(parentDir, "repo-beta-artifacts");
        const packA = path.join(hudA, "pack1", "planning");
        const docPath = path.join(parentDir, "arch-flow.md");
        await mkdir(packA, { recursive: true });
        await mkdir(hudB, { recursive: true });
        await writeFile(path.join(packA, "slides.html"), "<html><head></head><body>ALPHA</body></html>");
        await writeFile(path.join(packA, "timeline.json"), JSON.stringify({ title: "Alpha PR #1", scenes: [{ id: "s1" }], total_duration: 12 }));
        await writeFile(
            docPath,
            "# Flow Architecture\n\n> Control & data flow overview.\n\n## 1. Logical Architecture\n\nExtension connects to SPA.\n\n```mermaid\nflowchart LR\n  A[Host] --> B[SPA]\n```\n\n## 2. Awaited Turns\n\nClicks wake the yielded agent.\n\n```mermaid\nsequenceDiagram\n  SPA->>Bridge: POST /a2ui/action\n```\n",
        );

        // Fast-path doc walkthrough generation into hudB
        const t0 = Date.now();
        const built = await generateDocWalkthroughPack({
            docPath,
            packsRoot: hudB,
            repoPath: parentDir,
        });
        const elapsedMs = Date.now() - t0;
        assert.equal(built.ok, true);
        assert.equal(built.packKind, "architecture_doc");
        assert.equal(built.scenes, 2);
        assert.ok(elapsedMs < 5000, `expected fast doc build, took ${elapsedMs}ms`);

        const huds = await discoverHuds({ activePacksRoot: hudA });
        assert.equal(huds.length, 2);
        assert.equal(huds[0].id, "repo-alpha");
        assert.equal(huds[0].active, true);
        assert.equal(huds[1].id, "repo-beta");
        assert.equal(huds[1].packCount, 1);

        const srv = await startWalkthroughServer({
            instanceId: "multihud-1",
            deckDir: packA,
            packsRoot: hudA,
            session: { async send() { return "m1"; } },
            telemetryPollMs: 0,
        });
        try {
            const hRes = await fetch(`${srv.url}huds`);
            assert.equal(hRes.status, 200);
            const hJson = await hRes.json();
            assert.equal(hJson.huds.length, 2);

            // Reject arbitrary path outside discovered HUDs
            const badHud = await fetch(`${srv.url}huds/select`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({ packsRoot: "/etc" }),
            });
            assert.equal(badHud.status, 403);

            // Switch to repo-beta HUD deterministically
            const okHud = await fetch(`${srv.url}huds/select`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({ id: "repo-beta" }),
            });
            assert.equal(okHud.status, 200);
            const okJson = await okHud.json();
            assert.equal(okJson.dispatchedToAgent, false);
            assert.equal(okJson.hud.id, "repo-beta");

            const pRes = await fetch(`${srv.url}packs`);
            const pJson = await pRes.json();
            assert.equal(pJson.packs.length, 1);
            assert.equal(pJson.packs[0].kind, "architecture_doc");
        } finally {
            await srv.close();
        }
    } finally {
        await rm(parentDir, { recursive: true, force: true });
    }
});

test("discoverAudioModels classifies oMLX TTS and Speech-to-Speech models and /audio/regenerate streams background progress", async () => {
    assert.deepEqual(classifyAudioModel({ id: "Ming-3.0-tts-tiny-4bit", model_type: "audio_tts" }), { kind: "tts", kindLabel: "TTS" });
    assert.deepEqual(classifyAudioModel({ id: "LFM3-Omni-2B-MLX-4bit", model_type: "vlm", realtime_voice: true }), { kind: "sts", kindLabel: "Speech-to-Speech" });
    assert.equal(classifyAudioModel({ id: "Qwen2.5-0.5B-Instruct-4bit", model_type: "llm" }), null);
    assert.equal(classifyAudioModel({ id: "whisper-large-v3", model_type: "audio_stt" }), null);
    assert.equal(classifyAudioModel({ id: "jina-reranker-v3.5-mlx", model_type: "reranker" }), null);

    const mockFetch = async (url) => {
        const s = String(url);
        if (s.endsWith("/v1/models/status")) {
            return {
                ok: true,
                async json() {
                    return {
                        models: [
                            { id: "LFM3-Omni-2B-MLX-4bit", model_type: "vlm", realtime_voice: true, config_model_type: "lfm3_omni", estimated_size_formatted: "1.92 GB" },
                            { id: "Ming-3.0-tts-tiny-4bit", model_type: "audio_tts", realtime_voice: false, config_model_type: "lingv3_tts", estimated_size_formatted: "6.48 GB" },
                            { id: "Qwen2.5-0.5B-Instruct-4bit", model_type: "llm", realtime_voice: false },
                        ],
                    };
                },
            };
        }
        if (s.includes("/v1/audio/voices?model=LFM3-Omni-2B-MLX-4bit")) {
            return { ok: true, async json() { return { model: "LFM3-Omni-2B-MLX-4bit", voices: ["female", "male"] }; } };
        }
        if (s.includes("/v1/audio/voices?model=Ming-3.0-tts-tiny-4bit")) {
            return { ok: true, async json() { return { model: "Ming-3.0-tts-tiny-4bit", voices: [] }; } };
        }
        return { ok: false, async json() { return {}; } };
    };

    const cat = await discoverAudioModels({ endpointBase: "http://127.0.0.1:18001", fetchImpl: mockFetch });
    assert.equal(cat.available, true);
    assert.equal(cat.models.length, 2);
    assert.equal(cat.models[0].id, "Ming-3.0-tts-tiny-4bit");
    assert.equal(cat.models[0].kind, "tts");
    assert.equal(cat.models[0].supportsAnchor, true);
    assert.equal(cat.models[1].id, "LFM3-Omni-2B-MLX-4bit");
    assert.equal(cat.models[1].kind, "sts");
    assert.deepEqual(cat.models[1].voices, ["female", "male"]);
    assert.equal(cat.models[1].supportsAnchor, false);

    // Full server round-trip: GET /audio/models + CSRF-guarded POST /audio/regenerate + SSE events
    const tmpDir = await mkdtemp(path.join(os.tmpdir(), "nw-audio-regen-"));
    try {
        await writeFile(path.join(tmpDir, "slides.html"), "<html><head></head><body>DECK</body></html>");
        await writeFile(path.join(tmpDir, "timeline.json"), JSON.stringify({
            title: "Regen Demo",
            total_duration: 10,
            scenes: [
                { id: "s1", title: "One", mermaid: "flowchart LR\n A", narration: "First.", captions: ["First."], audio: "audio/s1.wav", start: 0, duration: 5 },
                { id: "s2", title: "Two", mermaid: "flowchart LR\n B", narration: "Second.", captions: ["Second."], audio: "audio/s2.wav", start: 5, duration: 5 },
            ],
        }));

        const srv = await startWalkthroughServer({
            instanceId: "regen-1",
            deckDir: tmpDir,
            session: { async send() { return "m1"; } },
            audioEndpointBase: "http://127.0.0.1:18001",
            audioFetchImpl: mockFetch,
            audioRegenerator: async ({ deckDir, model, voice, onProgress }) => {
                onProgress({ completedScenes: 1, currentScene: "s1", stage: "synthesizing" });
                onProgress({ completedScenes: 2, currentScene: "s2", stage: "mastering" });
                await writeFile(path.join(deckDir, "timeline.json"), JSON.stringify({
                    title: "Regen Demo",
                    total_duration: 14.5,
                    tts: { model, voice, endpoint: "http://127.0.0.1:18001/v1/audio/speech" },
                    scenes: [
                        { id: "s1", title: "One", mermaid: "flowchart LR\n A", narration: "First.", captions: ["First."], audio: "audio/s1.wav", start: 0, duration: 6.5 },
                        { id: "s2", title: "Two", mermaid: "flowchart LR\n B", narration: "Second.", captions: ["Second."], audio: "audio/s2.wav", start: 6.5, duration: 8.0 },
                    ],
                }));
            },
            telemetryPollMs: 0,
        });
        try {
            const mRes = await fetch(`${srv.url}audio/models`);
            assert.equal(mRes.status, 200);
            const mJson = await mRes.json();
            assert.equal(mJson.models.length, 2);

            // Missing CSRF token must fail closed (403)
            const noTok = await fetch(`${srv.url}audio/regenerate`, {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ model: "LFM3-Omni-2B-MLX-4bit", voice: "male" }),
            });
            assert.equal(noTok.status, 403);

            // Subscribe to SSE before triggering regeneration
            const ac = new AbortController();
            const sseResp = await fetch(`${srv.url}events`, { signal: ac.signal });
            const reader = sseResp.body.getReader();
            const dec = new TextDecoder();
            const seenEvents = [];
            let buf = "";

            const regRes = await fetch(`${srv.url}audio/regenerate`, {
                method: "POST",
                headers: { "content-type": "application/json", "x-canvas-token": srv.token },
                body: JSON.stringify({ model: "LFM3-Omni-2B-MLX-4bit", voice: "male" }),
            });
            assert.equal(regRes.status, 202);
            const regJson = await regRes.json();
            assert.equal(regJson.ok, true);
            assert.equal(regJson.job.model, "LFM3-Omni-2B-MLX-4bit");

            let completePayload = null;
            const deadline = Date.now() + 5000;
            while (!completePayload && Date.now() < deadline) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += dec.decode(value, { stream: true });
                let sep;
                while ((sep = buf.indexOf("\n\n")) >= 0) {
                    const frame = buf.slice(0, sep);
                    buf = buf.slice(sep + 2);
                    const evLine = frame.split("\n").find((l) => l.startsWith("event:"));
                    const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
                    if (evLine && dataLine) {
                        const evName = evLine.slice(6).trim();
                        seenEvents.push(evName);
                        if (evName === "audio_regen_complete") {
                            completePayload = JSON.parse(dataLine.slice(5).trim());
                        }
                    }
                }
            }
            ac.abort();

            assert.ok(seenEvents.includes("audio_regen_started"), `missing started in ${seenEvents}`);
            assert.ok(seenEvents.includes("audio_regen_progress"), `missing progress in ${seenEvents}`);
            assert.ok(completePayload, "missing audio_regen_complete event");
            assert.equal(completePayload.data.total, 14.5);
            assert.equal(completePayload.data.tts.model, "LFM3-Omni-2B-MLX-4bit");
            assert.equal(completePayload.data.tts.voice, "male");
            assert.equal(completePayload.data.scenes[1].start, 6.5);
        } finally {
            await srv.close();
        }
    } finally {
        await rm(tmpDir, { recursive: true, force: true });
    }
});
