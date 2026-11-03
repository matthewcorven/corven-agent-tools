#!/usr/bin/env python3
"""MCP protocol smoke test for the copilot-byok-metadata server."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PLUGIN_ROOT / "server.py"


class CopilotByokMetadataMCPSmokeTest(unittest.TestCase):
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
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr_out = self.proc.stderr.read()
            raise RuntimeError(f"Server closed connection unexpectedly. Stderr: {stderr_out}")
        return json.loads(line)

    def test_initialize_and_tools_list(self) -> None:
        init_res = self.send_request(
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
        self.assertEqual(init_res["id"], 1)
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "copilot-byok-metadata")
        self.assertIn("capabilities", init_res["result"])

        tools_res = self.send_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tool_names = {tool["name"] for tool in tools_res["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {
                "read_byok_config",
                "list_byok_models",
                "fetch_provider_metadata",
                "plan_metadata_updates",
                "apply_metadata_updates",
                "get_provider_registry",
            },
        )

    def test_get_provider_registry(self) -> None:
        res = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_provider_registry", "arguments": {}},
            }
        )
        self.assertFalse(res["result"]["isError"])
        structured = res["result"]["structuredContent"]
        self.assertIn("openrouter", structured["providers"])
        self.assertEqual(
            structured["providers"]["openrouter"]["catalog_url"],
            "https://openrouter.ai/api/v1/models",
        )
        self.assertEqual(
            structured["copilot_reasoning_efforts"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            structured["copilot_context_tiers"], ["default", "long_context"]
        )
        self.assertTrue(structured["targets"]["github_copilot_app"]["supported"])
        self.assertFalse(structured["targets"]["github_copilot_cli"]["supported"])

    def test_unknown_tool_returns_error(self) -> None:
        res = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            }
        )
        self.assertIn("error", res)
        self.assertEqual(res["error"]["code"], -32601)

    def test_server_discover(self) -> None:
        res = self.send_request(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "server/discover",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
                },
            }
        )
        self.assertEqual(res["result"]["resultType"], "complete")
        self.assertIn("2026-07-28", res["result"]["supportedVersions"])


if __name__ == "__main__":
    unittest.main()
