#!/usr/bin/env python3
"""MCP protocol smoke test for the playwright-report-retrieval server."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PLUGIN_ROOT / "server.py"


def build_index_html(directory: Path, files: dict[str, object], *, pad: bool = True) -> Path:
    """Write a self-contained ``index.html`` shaped like a real Playwright report.

    Playwright embeds the whole report as a base64 zip inside
    ``<template id="playwrightReportBase64">``.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, payload in files.items():
            zf.writestr(name, json.dumps(payload))

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if not pad:
        encoded = encoded.rstrip("=")

    directory.mkdir(parents=True, exist_ok=True)
    index_html = directory / "index.html"
    index_html.write_text(
        "<!DOCTYPE html><html><body>"
        f'<template id="playwrightReportBase64">data:application/zip;base64,{encoded}</template>'
        "</body></html>",
        encoding="utf-8",
    )
    return index_html


def modern_report_files(*, failing: bool = False) -> dict[str, object]:
    """Return the per-file JSON entries a modern Playwright report contains."""
    tests = [
        {
            "testId": "aaa",
            "title": "happy path: streams a reply",
            "projectName": "chromium",
            "location": {"file": "e2e/tests/chat.spec.ts", "line": 10, "column": 1},
            "path": ["chat.spec.ts", "happy path"],
            "outcome": "expected",
            "results": [{"status": "passed", "retry": 0}],
            "ok": True,
        },
        {
            "testId": "bbb",
            "title": "edge case: blank message",
            "projectName": "chromium",
            "location": {"file": "e2e/tests/chat.spec.ts", "line": 40, "column": 1},
            "path": ["chat.spec.ts", "edge case"],
            "outcome": "unexpected" if failing else "expected",
            "results": [{"status": "failed" if failing else "passed", "retry": 0}],
            "ok": not failing,
        },
    ]
    return {
        "report.json": {
            "metadata": {},
            "files": [],
            "projectNames": ["chromium"],
            "stats": {"expected": 2 - int(failing), "unexpected": int(failing)},
        },
        "chat-abc123.json": {
            "fileId": "chat-abc123",
            "fileName": "chat.spec.ts",
            "tests": tests,
        },
    }


class PlaywrightReportRetrievalMCPSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def tearDown(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()

    def send_request(self, req: dict) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr_out = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"Server closed connection unexpectedly. Stderr: {stderr_out}")
        return json.loads(line)

    def test_initialize_and_tools_list(self) -> None:
        init = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }
        )
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["serverInfo"]["name"], "playwright-report-retrieval")
        self.assertIn("tools", init["result"]["capabilities"])

        tools = self.send_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("fetch_report", names)
        self.assertIn("list_failed_tests", names)

    def test_list_failed_tests_reads_a_report_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            build_index_html(report, modern_report_files(failing=True))

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertFalse(res["result"]["isError"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["failed"][0]["title"], "edge case: blank message")
        self.assertEqual(payload["failed"][0]["test_id"], "bbb")
        self.assertEqual(payload["failed"][0]["file"], "chat.spec.ts")

    def test_all_passing_report_reports_a_non_zero_total(self) -> None:
        """A green report must still count its tests (DEF-4 regression guard)."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            build_index_html(report, modern_report_files(failing=False))

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(payload["failed"], [])

    def test_unpadded_base64_payload_is_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            build_index_html(report, modern_report_files(failing=True), pad=False)

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed_count"], 1)

    def test_legacy_data_directory_is_still_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            (report / "data").mkdir()
            (report / "data" / "report.json").write_text(
                json.dumps(
                    {
                        "suites": [
                            {
                                "title": "chat.spec.ts",
                                "file": "e2e/tests/chat.spec.ts",
                                "specs": [
                                    {
                                        "title": "happy path: streams a reply",
                                        "ok": True,
                                        "tests": [{"status": "expected", "testId": "aaa"}],
                                    },
                                    {
                                        "title": "edge case: blank message",
                                        "ok": False,
                                        "tests": [{"status": "unexpected", "testId": "bbb"}],
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["failed"][0]["test_id"], "bbb")

    def test_unknown_tool_returns_tool_error(self) -> None:
        res = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
        self.assertTrue(res["result"]["isError"])

    def test_unknown_method_returns_rpc_error(self) -> None:
        res = self.send_request({"jsonrpc": "2.0", "id": 5, "method": "bogus", "params": {}})
        self.assertEqual(res["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
