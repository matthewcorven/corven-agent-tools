#!/usr/bin/env python3
"""MCP protocol smoke test for squad-model-resolver server."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = PLUGIN_ROOT / "server.py"


class SquadModelResolverMCPSmokeTest(unittest.TestCase):
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
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()

    def send_request(self, req: dict) -> dict:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr_out = self.proc.stderr.read()
            raise RuntimeError(f"Server closed connection unexpectedly. Stderr: {stderr_out}")
        return json.loads(line)

    def test_mcp_initialize_and_tools_call(self) -> None:
        # 1. Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
        init_res = self.send_request(init_req)
        self.assertEqual(init_res["id"], 1)
        self.assertIn("capabilities", init_res["result"])
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "squad-model-resolver")

        # 2. tools/list
        tools_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        tools_res = self.send_request(tools_req)
        self.assertEqual(tools_res["id"], 2)
        tool_names = [t["name"] for t in tools_res["result"]["tools"]]
        self.assertIn("resolve_model", tool_names)
        self.assertIn("resolve_for_agent", tool_names)
        self.assertIn("verify_spawn", tool_names)

        # 3. tools/call - resolve_model
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "resolve_model",
                "arguments": {
                    "reference": "claude-sonnet-5.5",
                    "catalog": ["claude-sonnet-5.5", "gpt-5.6-terra"],
                },
            },
        }
        call_res = self.send_request(call_req)
        self.assertEqual(call_res["id"], 3)
        structured = call_res["result"]["structuredContent"]
        self.assertEqual(structured["status"], "resolved")
        self.assertEqual(structured["model_id"], "claude-sonnet-5.5")

        # 4. tools/call - verify_spawn
        verify_req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "verify_spawn",
                "arguments": {
                    "selected_model_id": "claude-sonnet-5.5",
                    "reported_model_id": "claude-sonnet-5.5",
                },
            },
        }
        verify_res = self.send_request(verify_req)
        self.assertEqual(verify_res["id"], 4)
        verify_struct = verify_res["result"]["structuredContent"]
        self.assertTrue(verify_struct["match"])
        self.assertEqual(verify_struct["status"], "match")

    def test_mcp_server_discover(self) -> None:
        discover_req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                },
            },
        }
        res = self.send_request(discover_req)
        self.assertEqual(res["id"], 10)
        self.assertEqual(res["result"]["resultType"], "complete")
        self.assertIn("2026-07-28", res["result"]["supportedVersions"])


if __name__ == "__main__":
    unittest.main()
