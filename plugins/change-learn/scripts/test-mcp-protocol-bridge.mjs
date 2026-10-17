#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryPath = path.resolve(process.argv[2] ?? process.cwd());
const temporaryDirectory = await mkdtemp(path.join(tmpdir(), "change-learn-bridge-test-"));
const diagnosticsDirectory = path.join(temporaryDirectory, "diagnostics");

function startBridge() {
    const process = spawn(
        "node",
        [
            path.join(scriptDirectory, "mcp-protocol-bridge.mjs"),
            path.join(scriptDirectory, "mcp-stdio.sh"),
        ],
        {
            cwd: repositoryPath,
            env: {
                ...globalThis.process.env,
                TMPDIR: temporaryDirectory,
                CHANGE_LEARN_MCP_LOG_DIR: diagnosticsDirectory,
            },
            stdio: ["pipe", "pipe", "pipe"],
        },
    );
    const responses = new Map();
    let stderr = "";

    process.stderr.setEncoding("utf8");
    process.stderr.on("data", (chunk) => {
        stderr += chunk;
    });
    createInterface({ input: process.stdout }).on("line", (line) => {
        const message = JSON.parse(line);
        if (message.id !== undefined) {
            responses.set(String(message.id), message);
        }
    });

    return { process, responses, getStderr: () => stderr };
}

function send(bridge, message) {
    bridge.process.stdin.write(`${JSON.stringify(message)}\n`);
}

async function responseFor(bridge, id) {
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
        const response = bridge.responses.get(String(id));
        if (response) {
            return response;
        }
        await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw new Error(`Timed out waiting for response ${id}: ${bridge.getStderr()}`);
}

function assertSuccessful(response, description) {
    if (response.error || !response.result) {
        throw new Error(`${description} failed: ${JSON.stringify(response)}`);
    }
}

async function stopBridge(bridge) {
    const finished = new Promise((resolve) => bridge.process.once("exit", resolve));
    bridge.process.stdin.end();
    const timeout = setTimeout(() => bridge.process.kill("SIGTERM"), 10_000);
    await finished;
    clearTimeout(timeout);
}

async function runDiscoveryFirst() {
    const bridge = startBridge();
    try {
        send(bridge, {
            jsonrpc: "2.0",
            id: 0,
            method: "server/discover",
            params: {
                _meta: {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                },
            },
        });
        const discovery = await responseFor(bridge, 0);
        assertSuccessful(discovery, "server/discover");
        if (
            discovery.result.resultType !== "complete"
            || !discovery.result.supportedVersions?.includes("2026-07-28")
        ) {
            throw new Error(`Invalid discovery response: ${JSON.stringify(discovery)}`);
        }

        send(bridge, { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} });
        const tools = await responseFor(bridge, 1);
        assertSuccessful(tools, "tools/list after server/discover");
        if (!tools.result.tools?.some((tool) => tool.name === "get_change_config")) {
            throw new Error("tools/list did not advertise get_change_config.");
        }

        send(bridge, {
            jsonrpc: "2.0",
            id: 2,
            method: "tools/call",
            params: { name: "get_change_config", arguments: {} },
        });
        assertSuccessful(await responseFor(bridge, 2), "get_change_config after server/discover");
    } finally {
        await stopBridge(bridge);
    }
}

async function runLegacyHandshake() {
    const bridge = startBridge();
    try {
        send(bridge, {
            jsonrpc: "2.0",
            id: 3,
            method: "initialize",
            params: {
                protocolVersion: "2025-11-25",
                capabilities: {},
                clientInfo: { name: "bridge-smoke-test", version: "1.0.0" },
            },
        });
        assertSuccessful(await responseFor(bridge, 3), "legacy initialize");
        send(bridge, { jsonrpc: "2.0", method: "notifications/initialized", params: {} });
        send(bridge, {
            jsonrpc: "2.0",
            id: 4,
            method: "tools/call",
            params: { name: "get_change_config", arguments: {} },
        });
        assertSuccessful(await responseFor(bridge, 4), "get_change_config after legacy initialization");
    } finally {
        await stopBridge(bridge);
    }
}

try {
    await runDiscoveryFirst();
    await runLegacyHandshake();

    const diagnosticFiles = await readdir(diagnosticsDirectory);
    const diagnostics = (
        await Promise.all(
            diagnosticFiles.map((file) => readFile(path.join(diagnosticsDirectory, file), "utf8")),
        )
    ).join("\n");
    if (diagnostics.includes("forwarding server/discover")) {
        throw new Error("server/discover was incorrectly forwarded to the legacy host.");
    }

    console.log("Change Learn MCP bridge smoke test passed.");
} finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
}
