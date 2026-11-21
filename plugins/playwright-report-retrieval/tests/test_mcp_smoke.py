#!/usr/bin/env python3
"""MCP protocol smoke test for the playwright-report-retrieval server."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PLUGIN_ROOT / "server.py"


def build_index_html(
    directory: Path,
    files: dict[str, object],
    *,
    pad: bool = True,
    tag: str = "template",
    after_html: bool = False,
) -> Path:
    """Write a self-contained ``index.html`` shaped like a real Playwright report.

    Playwright embeds the whole report as a base64 zip inside an element carrying
    ``id="playwrightReportBase64"``. ``@playwright/test`` 1.64.x uses a
    ``<template>`` inside ``<body>``; 1.57.x uses a ``<script>`` appended after
    ``</html>``. Both shapes must be readable.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, payload in files.items():
            zf.writestr(name, json.dumps(payload))

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if not pad:
        encoded = encoded.rstrip("=")

    element = (
        f'<{tag} id="playwrightReportBase64" type="application/zip">'
        f"data:application/zip;base64,{encoded}</{tag}>"
    )
    if after_html:
        document = f"<!DOCTYPE html><html><body><div id='root'></div></body></html>\n{element}"
    else:
        document = f"<!DOCTYPE html><html><body>{element}</body></html>"

    directory.mkdir(parents=True, exist_ok=True)
    index_html = directory / "index.html"
    index_html.write_text(document, encoding="utf-8")
    return index_html


def aborted_report_files(*, errors: int = 2) -> dict[str, object]:
    """Return the single ``report.json`` a report aborted in globalSetup contains.

    Playwright still reports ``stats.ok: true`` here — the only failure signal is
    the non-empty ``errors[]``.
    """
    return {
        "report.json": {
            "metadata": {},
            "files": [],
            "projectNames": ["chromium"],
            "stats": {"total": 0, "expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0, "ok": True},
            "errors": [
                f"Error: global-setup: form template seed failed (500): {{...}} (shard {i})"
                for i in range(errors)
            ],
        }
    }


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
        self.assertEqual(init["result"]["serverInfo"]["version"], "0.3.0")
        self.assertIn("tools", init["result"]["capabilities"])

        tools = self.send_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("fetch_report", names)
        self.assertIn("list_failed_tests", names)
        self.assertIn("create_artifact_run", names)
        self.assertIn("find_artifact_runs", names)
        self.assertIn("check_artifact_budget", names)

    def test_artifact_tools_require_an_explicit_repo_path(self) -> None:
        """The server's cwd is not a safe default for a repo-scoped tool."""
        for tool in ("create_artifact_run", "find_artifact_runs", "check_artifact_budget"):
            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {}},
                }
            )
            self.assertTrue(res["result"]["isError"], f"{tool} accepted a missing repo_path")
            self.assertIn("repo_path is required", res["result"]["structuredContent"]["error"])

    def test_artifact_tools_reject_a_nonexistent_repo_path(self) -> None:
        res = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "find_artifact_runs",
                    "arguments": {"repo_path": "/nonexistent/repo/path/xyz"},
                },
            }
        )
        self.assertTrue(res["result"]["isError"])
        self.assertIn("does not exist", res["result"]["structuredContent"]["error"])

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

    def test_script_embedded_report_after_html_is_decoded(self) -> None:
        """DEF-9: 1.57.x emits a <script> payload after </html>, not a <template>."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            build_index_html(report, modern_report_files(failing=True), tag="script", after_html=True)

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertFalse(res["result"]["isError"], payload)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["failed"][0]["test_id"], "bbb")

    def test_global_errors_are_never_reported_as_a_clean_pass(self) -> None:
        """DEF-9: zero tests plus globalSetup errors must not look green."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            build_index_html(report, aborted_report_files(errors=2), tag="script", after_html=True)

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "list_failed_tests",
                        "arguments": {"report_dir": str(report)},
                    },
                }
            )

        payload = res["result"]["structuredContent"]
        self.assertFalse(res["result"]["isError"], payload)
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["failed_count"], 2)
        self.assertEqual(payload["error_count"], 2)
        self.assertEqual(len(payload["errors"]), 2)
        self.assertIn("global-setup", payload["errors"][0])
        self.assertIn("global-setup", payload["failed"][0]["title"])

    def test_report_json_with_inline_files_is_flattened(self) -> None:
        """DEF-9: a single report.json carrying files[] must yield its tests."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            files = modern_report_files(failing=True)
            inline = {
                "report.json": {
                    "metadata": {},
                    "files": [files["chat-abc123.json"]],
                    "projectNames": ["chromium"],
                    "stats": {"total": 2, "unexpected": 1, "ok": False},
                    "errors": [],
                }
            }
            build_index_html(report, inline, tag="script", after_html=True)

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
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
        self.assertNotIn("errors", payload)

    def test_clean_report_with_errors_key_stays_green(self) -> None:
        """DEF-9 AC5: a genuinely clean report still reports failed_count 0."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            files = modern_report_files(failing=False)
            files["report.json"]["errors"] = []
            build_index_html(report, files, tag="script", after_html=True)

            res = self.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
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
        self.assertNotIn("errors", payload)

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


class ArtifactToolSmokeTest(unittest.TestCase):
    """End-to-end coverage of the three artifact tools through the MCP transport."""

    def setUp(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self._tmp.name))
        self.repo = self._init_repo()

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
        self._tmp.cleanup()

    def _init_repo(self) -> Path:
        repo = self.base / "sample-repo"
        repo.mkdir()
        env = dict(os.environ)
        env.pop("E2E_ARTIFACTS_ROOT", None)
        env.pop("VDS_ARTIFACTS_ROOT", None)

        def git(*args: str) -> None:
            proc = subprocess.run(
                ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=False
            )
            assert proc.returncode == 0, proc.stderr

        git("init", "-b", "squad/12-e2e-artifacts")
        git("config", "user.email", "test@example.local")
        git("config", "user.name", "Test User")
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-m", "init")
        return Path(os.path.realpath(repo))

    def call(self, tool: str, arguments: dict) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
            + "\n"
        )
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertTrue(line, "server closed the connection instead of answering")
        return json.loads(line)["result"]

    def test_create_run_then_find_it_by_issue_and_latest(self) -> None:
        created = self.call(
            "create_artifact_run",
            {"repo_path": str(self.repo), "phase": "qa", "spec": "e2e-artifacts", "issue": "12"},
        )
        self.assertFalse(created["isError"], created["structuredContent"])
        record = created["structuredContent"]
        run_path = Path(record["path"])
        self.assertTrue(run_path.is_dir())
        for phase in ("planning", "mockups", "dev", "debug", "qa"):
            self.assertTrue((run_path / phase).is_dir(), f"missing {phase}/")
        self.assertEqual(record["issue"], "12")

        found = self.call("find_artifact_runs", {"repo_path": str(self.repo), "issue": "12"})
        self.assertFalse(found["isError"], found["structuredContent"])
        payload = found["structuredContent"]
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["runs"][0]["run"], record["run"])
        self.assertEqual(payload["latest"]["run"], record["run"])

        latest = self.call("find_artifact_runs", {"repo_path": str(self.repo), "latest": True})
        self.assertEqual(latest["structuredContent"]["latest"]["run"], record["run"])

    def test_create_run_rejects_an_invalid_phase(self) -> None:
        res = self.call(
            "create_artifact_run",
            {"repo_path": str(self.repo), "phase": "screenshots"},
        )
        self.assertTrue(res["isError"])
        self.assertIn("Invalid phase", res["structuredContent"]["error"])

    def test_local_budget_mode_measures_the_artifact_root(self) -> None:
        created = self.call(
            "create_artifact_run", {"repo_path": str(self.repo), "phase": "qa"}
        )
        run_path = Path(created["structuredContent"]["path"])
        (run_path / "qa" / "capture.bin").write_bytes(b"x" * 2048)

        budget_file = self.repo / "artifacts.budget.json"
        budget_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "local": {"maxGb": 2000 / (1024**3), "warnAtPercent": 80},
                    "ci": {"maxMbPerRun": 500, "warnAtPercent": 80},
                }
            ),
            encoding="utf-8",
        )

        res = self.call(
            "check_artifact_budget",
            {"repo_path": str(self.repo), "mode": "local", "budget_file": str(budget_file)},
        )
        self.assertFalse(res["isError"], res["structuredContent"])
        payload = res["structuredContent"]
        self.assertEqual(payload["mode"], "local")
        self.assertTrue(payload["warn"])
        self.assertGreaterEqual(payload["totalBytes"], 2048)
        self.assertEqual(Path(payload["artifactRoot"]), self.base / "sample-repo-artifacts")

    def test_ci_budget_mode_annotates_but_never_errors(self) -> None:
        budget_file = self.repo / "artifacts.budget.json"
        budget_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "local": {"maxGb": 2, "warnAtPercent": 80},
                    "ci": {"maxMbPerRun": 0.01, "warnAtPercent": 80},
                }
            ),
            encoding="utf-8",
        )
        report_dir = self.base / "playwright-report"
        report_dir.mkdir()
        (report_dir / "index.html").write_bytes(b"1" * 12_000)

        res = self.call(
            "check_artifact_budget",
            {
                "repo_path": str(self.repo),
                "mode": "ci",
                "paths": [str(report_dir)],
                "budget_file": str(budget_file),
            },
        )
        self.assertFalse(res["isError"], res["structuredContent"])
        payload = res["structuredContent"]
        self.assertTrue(payload["warn"])
        self.assertTrue(payload["overLimit"])
        self.assertIn("::warning title=CI Artifact Budget::", payload["annotation"])

    def test_ci_mode_requires_paths(self) -> None:
        res = self.call(
            "check_artifact_budget", {"repo_path": str(self.repo), "mode": "ci"}
        )
        self.assertTrue(res["isError"])
        self.assertIn("paths is required", res["structuredContent"]["error"])


class DeletedCwdTest(unittest.TestCase):
    """DEF-8: the server must survive its working directory being removed."""

    def test_list_failed_tests_works_after_cwd_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doomed = root / "gone"
            doomed.mkdir()

            # TMPDIR makes the child's tempfile.gettempdir() deterministic, so the
            # namespace fallback lands somewhere this test controls.
            env = dict(os.environ, TMPDIR=str(root))
            env.pop("PLAYWRIGHT_REPORT_NAMESPACE", None)

            proc = subprocess.Popen(
                [sys.executable, str(SERVER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(doomed),
                env=env,
            )
            try:
                doomed.rmdir()

                # Seed the cache the fallback namespace resolves to, so the call
                # never reaches the network.
                cached = root / "playwright-reports" / "42"
                build_index_html(cached, modern_report_files(failing=True))

                assert proc.stdin and proc.stdout
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "list_failed_tests",
                                "arguments": {"run_id": "42"},
                            },
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                line = proc.stdout.readline()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream:
                        stream.close()

        self.assertTrue(line, "server closed the connection instead of answering")
        result = json.loads(line)["result"]
        self.assertFalse(result["isError"], result["structuredContent"])
        self.assertEqual(result["structuredContent"]["total"], 2)
        self.assertEqual(result["structuredContent"]["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
