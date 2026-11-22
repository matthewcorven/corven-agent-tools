import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
    buildAgentTurnPayload,
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
