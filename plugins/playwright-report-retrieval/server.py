#!/usr/bin/env python3
"""playwright-report-retrieval MCP server.

Exposes two tools over JSON-RPC 2.0 stdio:

- ``fetch_report``      download a CI Playwright report and return its deep link
- ``list_failed_tests`` list the failing tests recorded in a fetched report

Both delegate to ``scripts/fetch_report.py`` so the CLI and the MCP share one
implementation. Read-only with respect to the repository: the only writes are
inside the report namespace.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Optional

PLUGIN_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from fetch_report import (  # noqa: E402
    DEFAULT_ARTIFACT_NAME,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_NAMESPACE,
    PROVIDERS,
    RetrievalError,
    fetch_report,
)

SERVER_NAME = "playwright-report-retrieval"
SERVER_VERSION = "0.2.3"
PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "fetch_report",
        "description": (
            "Download a Playwright HTML report from a CI run (by run ID or commit SHA), "
            "extract it to a local namespace, and return a file:// URL deep-linked to an "
            "optional test title or test ID. Cached reports are reused; entries older than "
            "max_age_days are pruned first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "CI run ID."},
                "commit": {"type": "string", "description": "Commit SHA; newest matching run is used."},
                "test": {"type": "string", "description": "Test title to deep-link to (#?q=...)."},
                "test_id": {"type": "string", "description": "Test ID to deep-link to (#?testId=...)."},
                "artifact_name": {
                    "type": "string",
                    "default": DEFAULT_ARTIFACT_NAME,
                    "description": "CI artifact name.",
                },
                "namespace": {
                    "type": "string",
                    "description": (
                        f"Extraction root (default: {DEFAULT_NAMESPACE} at the git worktree "
                        "root, else the server's working directory, else the system temp "
                        "directory). Pass it explicitly when the server's cwd is not your "
                        "checkout."
                    ),
                },
                "provider": {
                    "type": "string",
                    "enum": sorted(PROVIDERS),
                    "default": "github-actions",
                    "description": "CI provider implementation.",
                },
                "repo": {"type": "string", "description": "owner/repo override."},
                "workflow": {
                    "type": "string",
                    "description": (
                        "Workflow file name (e2e.yml) or numeric workflow id. Recommended "
                        "with commit when the repository runs several workflows per commit."
                    ),
                },
                "max_age_days": {
                    "type": "integer",
                    "default": DEFAULT_MAX_AGE_DAYS,
                    "description": "Purge cached reports older than this (0 disables).",
                },
                "open_browser": {
                    "type": "boolean",
                    "default": False,
                    "description": "Launch the default browser at the deep link.",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Re-download even if the report is already cached.",
                },
            },
            "anyOf": [{"required": ["run_id"]}, {"required": ["commit"]}],
        },
    },
    {
        "name": "list_failed_tests",
        "description": (
            "List the failing tests recorded in a Playwright report. Fetches the report "
            "first when run_id or commit is supplied, otherwise reads an already-extracted "
            "report directory. Global errors (for example a globalSetup abort that ran zero "
            "tests) are reported in errors[] and counted in failed_count, so a zero-test run "
            "is never returned as a clean pass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "CI run ID."},
                "commit": {"type": "string", "description": "Commit SHA."},
                "report_dir": {
                    "type": "string",
                    "description": "Path to an already-extracted report directory.",
                },
                "artifact_name": {"type": "string", "default": DEFAULT_ARTIFACT_NAME},
                "namespace": {
                    "type": "string",
                    "description": (
                        f"Extraction root (default: {DEFAULT_NAMESPACE} at the git worktree "
                        "root, else the server's working directory, else the system temp "
                        "directory). Pass it explicitly when the server's cwd is not your "
                        "checkout."
                    ),
                },
                "provider": {"type": "string", "enum": sorted(PROVIDERS), "default": "github-actions"},
                "repo": {"type": "string"},
                "workflow": {"type": "string"},
                "max_age_days": {"type": "integer", "default": DEFAULT_MAX_AGE_DAYS},
            },
        },
    },
]


# Playwright has shipped the embedded payload in two shapes: a ``<template>``
# (1.64.x) and a ``<script>`` appended after ``</html>`` (1.57.x). Match either,
# anywhere in the document, so the reader is not coupled to one reporter version.
_EMBEDDED_REPORT_RE = re.compile(
    r'<(?:template|script)[^>]*\bid=["\']playwrightReportBase64["\'][^>]*>(.*?)</(?:template|script)>',
    re.DOTALL,
)


def _decode_embedded_report(index_html: Path) -> dict[str, Any]:
    """Decode the zip payload Playwright embeds in a self-contained ``index.html``.

    Modern Playwright HTML reports ship as one ``index.html`` with the whole
    report base64-encoded inside an element carrying
    ``id="playwrightReportBase64"``. The payload is a zip of ``report.json`` plus
    one ``<fileId>.json`` per test file; a report that aborted before any spec ran
    contains only ``report.json``.
    """
    try:
        html = index_html.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    match = _EMBEDDED_REPORT_RE.search(html)
    if not match:
        return {}

    payload = match.group(1).strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    payload = "".join(payload.split())
    # The payload is not always padded; base64 requires a multiple of four.
    payload += "=" * (-len(payload) % 4)

    try:
        raw = base64.b64decode(payload)
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (binascii.Error, ValueError, zipfile.BadZipFile):
        return {}

    data: dict[str, Any] = {}
    with archive:
        for name in archive.namelist():
            if not name.endswith(".json"):
                continue
            try:
                data[name] = json.loads(archive.read(name).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                continue
    return data


def _read_report_data(report_dir: Path) -> dict[str, Any]:
    """Load the report's data, keyed by source file name.

    Playwright's HTML reporter writes a single self-contained ``index.html`` with
    the report embedded as base64. Older versions wrote a ``data/`` directory of
    plain JSON; both shapes are supported.
    """
    index_html = report_dir / "index.html"
    if index_html.is_file():
        embedded = _decode_embedded_report(index_html)
        if embedded:
            return embedded

    data_dir = report_dir / "data"
    if data_dir.is_dir():
        for name in ("report.json", "index.json"):
            candidate = data_dir / name
            if candidate.is_file():
                return {name: json.loads(candidate.read_text(encoding="utf-8"))}

    for candidate in sorted(report_dir.glob("*.json")):
        try:
            return {candidate.name: json.loads(candidate.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            continue

    return {}


def _test_status(test: dict[str, Any]) -> str:
    """Derive a single status for a test from its outcome and result statuses."""
    outcome = test.get("outcome")
    if outcome:
        return str(outcome)

    statuses = [r.get("status") for r in test.get("results") or [] if r.get("status")]
    if not statuses:
        return "unknown"
    for status in ("failed", "timedOut", "interrupted"):
        if status in statuses:
            return status
    return str(statuses[-1])


def _flatten_modern_tests(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the per-file ``{fileId}.json`` entries into test records."""
    found: list[dict[str, Any]] = []

    for name, payload in data.items():
        if name == "report.json" or not isinstance(payload, dict):
            continue
        file_name = payload.get("fileName") or ""
        for test in payload.get("tests") or []:
            location = test.get("location") or {}
            found.append(
                {
                    "title": test.get("title") or "",
                    "file": file_name or location.get("file") or "",
                    "suite": test.get("path") and " › ".join(test["path"]) or "",
                    "status": _test_status(test),
                    "test_id": test.get("testId") or "",
                    "project": test.get("projectName") or "",
                }
            )

    return found


def _walk_suites(suites: list[dict[str, Any]], path: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Flatten the legacy nested suite tree into a list of test records."""
    path = path or []
    found: list[dict[str, Any]] = []

    for suite in suites or []:
        title = suite.get("title") or ""
        here = [*path, title] if title else path

        for spec in suite.get("specs") or []:
            for test in spec.get("tests") or []:
                found.append(
                    {
                        "title": spec.get("title") or test.get("title") or "",
                        "file": suite.get("file") or "",
                        "suite": " › ".join(here),
                        "status": _test_status(test) if test.get("outcome") else (
                            test.get("status") or ("passed" if spec.get("ok") else "unknown")
                        ),
                        "test_id": test.get("testId") or spec.get("id") or "",
                        "project": test.get("projectName") or "",
                    }
                )

        found.extend(_walk_suites(suite.get("suites") or [], here))

    return found


def _flatten_report_json_tests(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a single ``report.json`` that carries top-level ``files[]``.

    Some reporter versions inline every test into ``report.json`` instead of
    writing one ``<fileId>.json`` per file. Each entry in ``files[]`` mirrors the
    per-file shape, so the same flattening applies.
    """
    found: list[dict[str, Any]] = []

    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        file_name = entry.get("fileName") or ""
        for test in entry.get("tests") or []:
            location = test.get("location") or {}
            found.append(
                {
                    "title": test.get("title") or "",
                    "file": file_name or location.get("file") or "",
                    "suite": test.get("path") and " › ".join(test["path"]) or "",
                    "status": _test_status(test),
                    "test_id": test.get("testId") or "",
                    "project": test.get("projectName") or "",
                }
            )

    return found


def _collect_tests(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every test in the report, modern shape first, legacy second."""
    tests = _flatten_modern_tests(data)
    if tests:
        return tests

    for payload in data.values():
        if not isinstance(payload, dict):
            continue
        tests.extend(_flatten_report_json_tests(payload))
        if payload.get("suites"):
            tests.extend(_walk_suites(payload["suites"]))
    return tests


def _collect_errors(data: dict[str, Any]) -> list[str]:
    """Return the report's global errors (``globalSetup`` aborts and friends).

    Playwright records a global-setup failure in ``errors[]`` while still
    reporting ``stats.ok: true``, so this is the only signal that the run did not
    actually execute its suite.
    """
    errors: list[str] = []

    for payload in data.values():
        if not isinstance(payload, dict):
            continue
        for error in payload.get("errors") or []:
            if isinstance(error, dict):
                message = error.get("message") or error.get("value") or ""
                stack = error.get("stack") or ""
                text = f"{message}\n{stack}".strip() if stack else str(message)
            else:
                text = str(error)
            if text.strip():
                errors.append(text.strip())

    return errors


def list_failed_tests(
    *,
    run_id: Optional[str] = None,
    commit: Optional[str] = None,
    report_dir: Optional[str] = None,
    artifact_name: str = DEFAULT_ARTIFACT_NAME,
    namespace: Optional[str] = None,
    provider: str = "github-actions",
    repo: Optional[str] = None,
    workflow: Optional[str] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Return the failing tests for a report, fetching it when needed."""
    if report_dir:
        root = Path(report_dir).expanduser()
        if not root.is_dir():
            raise RetrievalError(f"Report directory not found: {root}")
    elif run_id or commit:
        result = fetch_report(
            run_id=run_id,
            commit=commit,
            artifact_name=artifact_name,
            namespace=Path(namespace).expanduser() if namespace else None,
            provider_name=provider,
            repo=repo,
            workflow=workflow,
            max_age_days=max_age_days,
            open_browser=False,
        )
        root = result.report_dir
    else:
        raise RetrievalError("Provide run_id, commit, or report_dir.")

    data = _read_report_data(root)
    tests = _collect_tests(data)
    errors = _collect_errors(data)
    failed = [t for t in tests if t["status"] in {"unexpected", "failed", "timedOut", "interrupted"}]

    # A report with global errors never ran its suite to completion, so it can
    # never be a pass — even when Playwright reports ``stats.ok: true``.
    if errors:
        failed.extend(
            {
                "title": error.splitlines()[0],
                "file": "",
                "suite": "",
                "status": "failed",
                "test_id": "",
                "project": "",
                "error": error,
            }
            for error in errors
        )

    result: dict[str, Any] = {
        "report_dir": str(root),
        "total": len(tests),
        "failed_count": len(failed),
        "failed": failed,
    }
    if errors:
        result["errors"] = errors
        result["error_count"] = len(errors)
    if not tests and not errors:
        result["warning"] = (
            "The report contains no tests and no errors. Treat this as a failure: "
            "a run that executed zero tests is never a pass."
        )
    return result


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "fetch_report":
        result = fetch_report(
            run_id=arguments.get("run_id"),
            commit=arguments.get("commit"),
            test=arguments.get("test"),
            test_id=arguments.get("test_id"),
            artifact_name=arguments.get("artifact_name") or DEFAULT_ARTIFACT_NAME,
            namespace=Path(arguments["namespace"]).expanduser() if arguments.get("namespace") else None,
            provider_name=arguments.get("provider") or "github-actions",
            repo=arguments.get("repo"),
            workflow=arguments.get("workflow"),
            max_age_days=int(arguments.get("max_age_days", DEFAULT_MAX_AGE_DAYS)),
            open_browser=bool(arguments.get("open_browser", False)),
            force=bool(arguments.get("force", False)),
        )
        return _tool_result(result.to_dict())

    if name == "list_failed_tests":
        return _tool_result(
            list_failed_tests(
                run_id=arguments.get("run_id"),
                commit=arguments.get("commit"),
                report_dir=arguments.get("report_dir"),
                artifact_name=arguments.get("artifact_name") or DEFAULT_ARTIFACT_NAME,
                namespace=arguments.get("namespace"),
                provider=arguments.get("provider") or "github-actions",
                repo=arguments.get("repo"),
                workflow=arguments.get("workflow"),
                max_age_days=int(arguments.get("max_age_days", DEFAULT_MAX_AGE_DAYS)),
            )
        )

    raise RetrievalError(f"Unknown tool: {name}")


def handle_rpc_message(req: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(tool_name, arguments)}
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as a tool error
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": _tool_result({"error": str(exc)}, is_error=True),
            }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }
    return None


def run_stdio_server() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"{SERVER_NAME}: malformed JSON request: {exc}\n")
            continue

        resp = handle_rpc_message(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
