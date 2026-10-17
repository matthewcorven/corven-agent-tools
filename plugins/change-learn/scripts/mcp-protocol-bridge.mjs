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
const pendingInnerIds = new Map();
let outerInitializeRequest;

function write(stream, message) {
    stream.write(`${JSON.stringify(message)}\n`);
}

function requestInner(method, params, outerId) {
    const innerId = nextRequestId++;
    pendingRequests.set(innerId, outerId);
    pendingInnerIds.set(outerId, innerId);
    process.stderr.write(`change-learn: forwarding ${method} to inner MCP host.\n`);
    write(host.stdin, {
        jsonrpc: "2.0",
        id: innerId,
        method,
        params,
    });
}

function respondToOuterInitialize() {
    if (!outerInitializeRequest) {
        return;
    }

    write(process.stdout, {
        jsonrpc: "2.0",
        id: outerInitializeRequest.id,
        result: {
            protocolVersion: outerInitializeRequest.params?.protocolVersion ?? "2025-11-25",
            capabilities: { tools: { listChanged: false } },
            serverInfo: {
                name: "ChangeLearn.Host",
                version: "1.0.0.0",
            },
        },
    });
    outerInitializeRequest = undefined;
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
        respondToOuterInitialize();
        return;
    }

    const outerId = pendingRequests.get(message.id);
    if (outerId === undefined) {
        process.stderr.write(`change-learn: received an inner response for unknown request ${message.id}.\n`);
        return;
    }
    pendingRequests.delete(message.id);
    pendingInnerIds.delete(outerId);
    process.stderr.write(`change-learn: returning ${message.error ? "an error" : "a result"} to the MCP client.\n`);
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
        if (outerInitializeRequest) {
            process.stderr.write("change-learn: received a duplicate outer MCP initialization request.\n");
            return;
        }
        outerInitializeRequest = request;
        if (innerReady) {
            respondToOuterInitialize();
        }
        return;
    }

    if (request.method === "notifications/initialized") {
        return;
    }

    if (request.method === "notifications/cancelled") {
        const outerRequestId = request.params?.requestId;
        const innerRequestId = pendingInnerIds.get(outerRequestId);
        if (innerRequestId !== undefined) {
            pendingInnerIds.delete(outerRequestId);
            pendingRequests.delete(innerRequestId);
            process.stderr.write("change-learn: forwarding MCP request cancellation to inner host.\n");
            write(host.stdin, {
                jsonrpc: "2.0",
                method: "notifications/cancelled",
                params: { ...request.params, requestId: innerRequestId },
            });
        }
        return;
    }

    if (request.id === undefined) {
        return;
    }

    if (!innerReady) {
        process.stderr.write(`change-learn: rejected ${request.method} before MCP initialization completed.\n`);
        write(process.stdout, {
            jsonrpc: "2.0",
            id: request.id,
            error: {
                code: -32002,
                message: "MCP server initialization is still in progress.",
            },
        });
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
