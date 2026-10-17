#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const hostLauncher = process.argv[2];
if (!hostLauncher) {
    process.stderr.write("change-learn: missing inner MCP host launcher.\n");
    process.exit(1);
}

const host = spawn("bash", [hostLauncher], {
    stdio: ["pipe", "pipe", "pipe"],
});

host.stderr.on("data", (chunk) => process.stderr.write(chunk));
host.on("error", (error) => {
    process.stderr.write(`change-learn: could not start the MCP host: ${error.message}\n`);
    process.exit(1);
});
host.on("exit", (code, signal) => {
    process.stderr.write(
        `change-learn: MCP host exited${signal ? ` from ${signal}` : ` with code ${code ?? "unknown"}`}.\n`,
    );
    process.exit(code ?? 1);
});

let innerReady = false;
let nextRequestId = 1;
const pendingRequests = new Map();
const queuedRequests = [];

function write(stream, message) {
    stream.write(`${JSON.stringify(message)}\n`);
}

function requestInner(method, params, outerId) {
    const innerId = nextRequestId++;
    pendingRequests.set(innerId, outerId);
    write(host.stdin, {
        jsonrpc: "2.0",
        id: innerId,
        method,
        params,
    });
}

function flushQueue() {
    while (queuedRequests.length > 0) {
        const request = queuedRequests.shift();
        requestInner(request.method, request.params, request.id);
    }
}

createInterface({ input: host.stdout }).on("line", (line) => {
    let message;
    try {
        message = JSON.parse(line);
    } catch {
        process.stderr.write(`change-learn: inner host emitted invalid JSON: ${line}\n`);
        return;
    }

    if (message.id === 0) {
        if (message.error) {
            process.stderr.write(`change-learn: inner MCP initialization failed: ${message.error.message}\n`);
            process.exit(1);
        }
        innerReady = true;
        write(host.stdin, {
            jsonrpc: "2.0",
            method: "notifications/initialized",
            params: {},
        });
        flushQueue();
        return;
    }

    const outerId = pendingRequests.get(message.id);
    if (outerId === undefined) {
        return;
    }
    pendingRequests.delete(message.id);
    write(process.stdout, {
        jsonrpc: "2.0",
        id: outerId,
        ...(message.error ? { error: message.error } : { result: message.result }),
    });
});

const clientInput = createInterface({ input: process.stdin });

clientInput.on("line", (line) => {
    let request;
    try {
        request = JSON.parse(line);
    } catch {
        process.stderr.write(`change-learn: received invalid MCP JSON: ${line}\n`);
        return;
    }

    if (request.method === "initialize") {
        write(process.stdout, {
            jsonrpc: "2.0",
            id: request.id,
            result: {
                protocolVersion: request.params?.protocolVersion ?? "2025-11-25",
                capabilities: { tools: { listChanged: false } },
                serverInfo: {
                    name: "ChangeLearn.Host",
                    version: "1.0.0.0",
                },
            },
        });
        return;
    }

    if (request.method === "notifications/initialized") {
        return;
    }

    if (request.id === undefined) {
        return;
    }

    if (!innerReady) {
        queuedRequests.push(request);
        return;
    }
    requestInner(request.method, request.params, request.id);
});

clientInput.on("close", () => host.stdin.end());

write(host.stdin, {
    jsonrpc: "2.0",
    id: 0,
    method: "initialize",
    params: {
        protocolVersion: "2025-11-25",
        capabilities: {},
        clientInfo: {
            name: "change-learn-protocol-bridge",
            version: "1.0.0",
        },
    },
});
