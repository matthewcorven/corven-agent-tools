#!/usr/bin/env node

import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";

const hostLauncher = process.argv[2];
const innerProtocolVersion = "2025-11-25";
const parentPid = process.ppid;
if (!hostLauncher) {
    process.stderr.write("change-learn: missing inner MCP host launcher.\n");
    process.exit(1);
}

const pluginManifestPath = path.resolve(path.dirname(hostLauncher), "..", "plugin.json");
let pluginVersion = "unknown";
try {
    pluginVersion = JSON.parse(readFileSync(pluginManifestPath, "utf8")).version || pluginVersion;
} catch {
    process.stderr.write(`change-learn: could not read plugin version from ${pluginManifestPath}.\n`);
}

const diagnosticsDirectory = process.env.CHANGE_LEARN_MCP_LOG_DIR
    ?? path.join(tmpdir(), "change-learn-mcp");
const diagnosticsPath = path.join(diagnosticsDirectory, `bridge-${process.pid}.log`);

function log(message) {
    const line = `${new Date().toISOString()} ${message}`;
    process.stderr.write(`change-learn: ${message}\n`);
    try {
        mkdirSync(diagnosticsDirectory, { recursive: true, mode: 0o700 });
        appendFileSync(diagnosticsPath, `${line}\n`, { mode: 0o600 });
    } catch (error) {
        process.stderr.write(`change-learn: could not write diagnostics: ${error.message}\n`);
    }
}

log(`starting bridge v${pluginVersion}; parent=${parentPid}; diagnostics=${diagnosticsPath}`);

const host = spawn("bash", [hostLauncher], {
    stdio: ["pipe", "pipe", "pipe"],
});
log(`started inner MCP host pid=${host.pid}`);

let shuttingDown = false;
let forcedShutdownTimer;

function shutdown(reason, exitCode = 0) {
    if (shuttingDown) {
        return;
    }

    shuttingDown = true;
    log(`shutting down MCP host (${reason})`);
    host.stdin.end();
    forcedShutdownTimer = setTimeout(() => {
        if (!host.killed) {
            log("MCP host did not stop gracefully; sending SIGTERM");
            host.kill("SIGTERM");
        }
    }, 5_000);
    forcedShutdownTimer.unref();

    host.once("exit", () => process.exit(exitCode));
}

host.stderr.on("data", (chunk) => process.stderr.write(chunk));
host.on("error", (error) => {
    log(`could not start the MCP host: ${error.message}`);
    shutdown("inner MCP host launch failure", 1);
});
host.on("exit", (code, signal) => {
    clearTimeout(forcedShutdownTimer);
    log(`MCP host exited${signal ? ` from ${signal}` : ` with code ${code ?? "unknown"}`}`);
    process.exit(shuttingDown ? 0 : (code ?? 1));
});

let innerReady = false;
let outerReady = false;
let nextRequestId = 1;
const pendingRequests = new Map();
const pendingInnerIds = new Map();
let outerDiscoveryRequest;
let outerInitializeRequest;
const queuedRequests = [];

function write(stream, message) {
    log(`writing ${message.method ?? "response"}${message.id === undefined ? "" : ` id=${message.id}`}`);
    stream.write(`${JSON.stringify(message)}\n`);
}

function requestInner(method, params, outerId) {
    const innerId = nextRequestId++;
    pendingRequests.set(innerId, outerId);
    pendingInnerIds.set(outerId, innerId);
    log(`forwarding ${method} id=${outerId} to inner MCP host as id=${innerId}`);
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
                version: pluginVersion,
            },
        },
    });
    outerInitializeRequest = undefined;
}

function respondToOuterDiscovery() {
    if (!outerDiscoveryRequest || !innerReady) {
        return;
    }

    write(process.stdout, {
        jsonrpc: "2.0",
        id: outerDiscoveryRequest.id,
        result: {
            resultType: "complete",
            supportedVersions: ["2026-07-28"],
            capabilities: {
                tools: { listChanged: false },
            },
            _meta: {
                "io.modelcontextprotocol/serverInfo": {
                    name: "ChangeLearn.Host",
                    version: pluginVersion,
                },
            },
        },
    });
    outerDiscoveryRequest = undefined;
    outerReady = true;
    log("outer MCP discovery completed");
    flushQueue();
}

createInterface({ input: host.stdout }).on("line", (line) => {
    let message;
    try {
        message = JSON.parse(line);
    } catch {
        log(`inner host emitted invalid JSON (${line.length} bytes)`);
        return;
    }

    if (message.id === 0) {
        if (message.error) {
            log(`inner MCP initialization failed: ${message.error.message}`);
            process.exit(1);
        }
        innerReady = true;
        log("inner MCP initialization completed");
        respondToOuterDiscovery();
        respondToOuterInitialize();
        flushQueue();
        return;
    }

    const outerId = pendingRequests.get(message.id);
    if (outerId === undefined) {
        log(`received an inner response for unknown request id=${message.id}`);
        return;
    }
    pendingRequests.delete(message.id);
    pendingInnerIds.delete(outerId);
    log(`returning ${message.error ? "an error" : "a result"} for outer request id=${outerId}`);
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
        log(`received invalid outer MCP JSON (${line.length} bytes)`);
        return;
    }

    log(`received ${request.method ?? "response"}${request.id === undefined ? "" : ` id=${request.id}`}`);

    if (request.method === "server/discover") {
        if (outerDiscoveryRequest) {
            log("received a duplicate outer MCP discovery request");
            return;
        }
        outerDiscoveryRequest = request;
        respondToOuterDiscovery();
        return;
    }

    if (request.method === "initialize") {
        if (outerInitializeRequest) {
            log("received a duplicate outer MCP initialization request");
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
        log("outer MCP initialization completed");
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
            log(`forwarding MCP request cancellation for outer id=${outerRequestId}`);
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
        log(`queueing ${request.method} id=${request.id} until MCP initialization completes`);
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
    log(`outer MCP stdout failed: ${error.message}`);
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
