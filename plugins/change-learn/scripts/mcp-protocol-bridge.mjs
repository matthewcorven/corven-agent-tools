#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const hostLauncher = process.argv[2];
const innerProtocolVersion = "2025-11-25";
const parentPid = process.ppid;
if (!hostLauncher) {
    process.stderr.write("change-learn: missing inner MCP host launcher.\n");
    process.exit(1);
}

const host = spawn("bash", [hostLauncher], {
    stdio: ["pipe", "pipe", "pipe"],
});

let shuttingDown = false;
let forcedShutdownTimer;

function shutdown(reason, exitCode = 0) {
    if (shuttingDown) {
        return;
    }

    shuttingDown = true;
    process.stderr.write(`change-learn: shutting down MCP host (${reason}).\n`);
    host.stdin.end();
    forcedShutdownTimer = setTimeout(() => {
        if (!host.killed) {
            process.stderr.write("change-learn: MCP host did not stop gracefully; sending SIGTERM.\n");
            host.kill("SIGTERM");
        }
    }, 5_000);
    forcedShutdownTimer.unref();

    host.once("exit", () => process.exit(exitCode));
}

host.stderr.on("data", (chunk) => process.stderr.write(chunk));
host.on("error", (error) => {
    process.stderr.write(`change-learn: could not start the MCP host: ${error.message}\n`);
    shutdown("inner MCP host launch failure", 1);
});
host.on("exit", (code, signal) => {
    clearTimeout(forcedShutdownTimer);
    process.stderr.write(
        `change-learn: MCP host exited${signal ? ` from ${signal}` : ` with code ${code ?? "unknown"}`}.\n`,
    );
    process.exit(shuttingDown ? 0 : (code ?? 1));
});

let innerReady = false;
let outerReady = false;
let nextRequestId = 1;
const pendingRequests = new Map();
const pendingInnerIds = new Map();
let outerInitializeRequest;
const queuedRequests = [];

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

function flushQueue() {
    if (!innerReady || !outerReady) {
        return;
    }

    while (queuedRequests.length > 0) {
        const request = queuedRequests.shift();
        requestInner(request.method, request.params, request.id);
    }
}

function respondToOuterInitialize() {
    if (!outerInitializeRequest) {
        return;
    }

    write(process.stdout, {
        jsonrpc: "2.0",
        id: outerInitializeRequest.id,
        result: {
            protocolVersion: innerProtocolVersion,
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
        respondToOuterInitialize();
        flushQueue();
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
        outerReady = true;
        write(host.stdin, {
            jsonrpc: "2.0",
            method: "notifications/initialized",
            params: request.params ?? {},
        });
        flushQueue();
        return;
    }

    if (request.method === "notifications/cancelled" || request.method === "$/cancelRequest") {
        const outerRequestId = request.params?.requestId ?? request.params?.id;
        const innerRequestId = pendingInnerIds.get(outerRequestId);
        if (innerRequestId !== undefined) {
            pendingInnerIds.delete(outerRequestId);
            pendingRequests.delete(innerRequestId);
            process.stderr.write("change-learn: forwarding MCP request cancellation to inner host.\n");
            write(host.stdin, {
                jsonrpc: "2.0",
                method: request.method,
                params: request.method === "notifications/cancelled"
                    ? { ...request.params, requestId: innerRequestId }
                    : { ...request.params, id: innerRequestId },
            });
        } else {
            const queuedRequestIndex = queuedRequests.findIndex(
                (queuedRequest) => queuedRequest.id === outerRequestId,
            );
            if (queuedRequestIndex !== -1) {
                queuedRequests.splice(queuedRequestIndex, 1);
            }
        }
        return;
    }

    if (request.id === undefined) {
        return;
    }

    if (!innerReady || !outerReady) {
        process.stderr.write(`change-learn: queueing ${request.method} until MCP initialization completes.\n`);
        queuedRequests.push(request);
        return;
    }
    requestInner(request.method, request.params, request.id);
});

clientInput.on("close", () => shutdown("outer MCP stdin closed"));
process.stdout.on("error", (error) => {
    if (error.code === "EPIPE") {
        shutdown("outer MCP stdout closed");
        return;
    }
    process.stderr.write(`change-learn: outer MCP stdout failed: ${error.message}\n`);
    shutdown("outer MCP stdout failure", 1);
});
process.on("SIGTERM", () => shutdown("SIGTERM received"));
process.on("SIGHUP", () => shutdown("SIGHUP received"));

const parentMonitor = setInterval(() => {
    if (process.ppid !== parentPid) {
        shutdown("outer MCP parent exited");
    }
}, 1_000);
parentMonitor.unref();

write(host.stdin, {
    jsonrpc: "2.0",
    id: 0,
    method: "initialize",
    params: {
        protocolVersion: innerProtocolVersion,
        capabilities: {},
        clientInfo: {
            name: "change-learn-protocol-bridge",
            version: "1.0.0",
        },
    },
});
