#!/usr/bin/env python3
"""Tests for read_byok_config and its workspace/redaction helpers."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import server  # noqa: E402

PROVIDER_ID = "or-provider"
LOCAL_PROVIDER_ID = "local-provider"
PROJECT_ID = "project-1"
WORKSPACE_ID = "workspace-1"

SCHEMA = """
CREATE TABLE model_providers (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    type TEXT NOT NULL DEFAULT 'openai',
    settings_json TEXT,
    account_id TEXT
);
CREATE TABLE provider_models (
    id TEXT PRIMARY KEY NOT NULL,
    provider_id TEXT NOT NULL REFERENCES model_providers(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    wire_model TEXT,
    display_name TEXT NOT NULL,
    max_prompt_tokens INTEGER,
    max_output_tokens INTEGER,
    wire_api_override TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    supported_reasoning_efforts TEXT,
    UNIQUE (provider_id, model_id)
);
CREATE TABLE app_state (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE projects (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    main_repo_path TEXT,
    default_branch TEXT
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT,
    branch TEXT,
    workspace_type TEXT
);
CREATE TABLE workspace_checkout_bindings (
    workspace_id TEXT NOT NULL,
    repo_path TEXT,
    repo_full_name TEXT,
    display_name TEXT,
    checkout_kind TEXT,
    checkout_path TEXT,
    worktree_id TEXT
);
"""


class ReadByokConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "data.db")
        self.repo_path = os.path.join(self.tmpdir.name, "repo")
        self.worktree_path = os.path.join(self.tmpdir.name, "worktrees", "repo", "feat")
        os.makedirs(self.repo_path)
        os.makedirs(self.worktree_path)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) VALUES (?, ?, ?, ?)",
            (
                PROVIDER_ID,
                "OpenRouter",
                "custom",
                json.dumps(
                    {
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "authKind": "api_key",
                        "apiKey": "sk-or-secret-value",
                        "headersJson": "{}",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) VALUES (?, ?, ?, ?)",
            (
                LOCAL_PROVIDER_ID,
                "oMLX",
                "ollama",
                json.dumps({"baseUrl": "http://127.0.0.1:8001/v1"}),
            ),
        )
        conn.execute(
            "INSERT INTO provider_models "
            "(id, provider_id, model_id, display_name, max_prompt_tokens, "
            " max_output_tokens, supported_reasoning_efforts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "row-reasoning",
                PROVIDER_ID,
                "vendor/reasoning-model",
                "vendor/reasoning-model",
                872000,
                128000,
                json.dumps(["max", "high", "low"]),
            ),
        )
        conn.execute(
            "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
            "VALUES (?, ?, ?, ?)",
            ("row-plain", PROVIDER_ID, "vendor/plain-model", "vendor/plain-model"),
        )
        conn.execute(
            "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
            "VALUES (?, ?, ?, ?)",
            ("row-local", LOCAL_PROVIDER_ID, "local-model", "local-model"),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_REASONING_KEY,
                json.dumps({f"{PROVIDER_ID}/vendor/reasoning-model": "high"}),
            ),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_CONTEXT_TIER_KEY,
                json.dumps({f"{PROVIDER_ID}/vendor/reasoning-model": "long_context"}),
            ),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_SELECTED_MODEL_KEY,
                f"{PROVIDER_ID}/vendor/reasoning-model",
            ),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_CATALOG_KEY,
                json.dumps(
                    {
                        "accountId": "acct",
                        "models": [
                            {
                                "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                                "name": "vendor/reasoning-model",
                                "providerId": PROVIDER_ID,
                                "capabilities": {"supports": {"reasoningEffort": True}},
                                "supportedReasoningEfforts": ["max", "high", "low"],
                            },
                            {
                                "id": "builtin-model",
                                "name": "Built-in",
                                "capabilities": {"limits": {"max_context_window_tokens": 1}},
                            },
                        ],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO projects (id, name, main_repo_path, default_branch) "
            "VALUES (?, ?, ?, ?)",
            (PROJECT_ID, "repo", self.repo_path, "main"),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, branch, workspace_type) VALUES (?, ?, ?, ?)",
            (WORKSPACE_ID, "Feature work", "feat", "worktree"),
        )
        conn.execute(
            "INSERT INTO workspace_checkout_bindings "
            "(workspace_id, repo_path, checkout_kind, checkout_path) "
            "VALUES (?, ?, ?, ?)",
            (WORKSPACE_ID, self.repo_path, "worktree", self.worktree_path),
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _read(self, **kwargs):
        return server.read_byok_config(db_path=self.db_path, **kwargs)

    def test_returns_every_provider_and_model(self) -> None:
        res = self._read()
        self.assertEqual(res["summary"]["provider_count"], 2)
        self.assertEqual(res["summary"]["model_count"], 3)
        self.assertEqual(
            {p["name"] for p in res["providers"]}, {"OpenRouter", "oMLX"}
        )
        self.assertEqual(
            {m["model_id"] for m in res["models"]},
            {"vendor/reasoning-model", "vendor/plain-model", "local-model"},
        )

    def test_reports_metadata_presence_and_gaps(self) -> None:
        res = self._read()
        by_id = {m["model_id"]: m for m in res["models"]}

        reasoning = by_id["vendor/reasoning-model"]
        self.assertTrue(reasoning["has_metadata"])
        self.assertEqual(reasoning["missing_metadata"], [])
        self.assertTrue(reasoning["supports_reasoning"])
        self.assertEqual(
            reasoning["supported_reasoning_efforts"], ["max", "high", "low"]
        )

        plain = by_id["vendor/plain-model"]
        self.assertFalse(plain["has_metadata"])
        self.assertEqual(
            plain["missing_metadata"],
            ["max_prompt_tokens", "max_output_tokens", "supported_reasoning_efforts"],
        )
        self.assertFalse(plain["supports_reasoning"])

        self.assertEqual(res["summary"]["models_with_metadata"], 1)
        self.assertEqual(res["summary"]["models_without_metadata"], 2)

    def test_reports_user_selections(self) -> None:
        res = self._read()
        self.assertEqual(
            res["selections"]["selected_model"],
            f"{PROVIDER_ID}/vendor/reasoning-model",
        )
        self.assertEqual(
            res["selections"]["reasoning_effort"][
                f"{PROVIDER_ID}/vendor/reasoning-model"
            ],
            "high",
        )
        by_id = {m["model_id"]: m for m in res["models"]}
        self.assertEqual(by_id["vendor/reasoning-model"]["reasoning_effort"], "high")
        self.assertEqual(
            by_id["vendor/reasoning-model"]["context_tier"], "long_context"
        )
        self.assertIsNone(by_id["vendor/plain-model"]["reasoning_effort"])

    def test_redacts_credentials_in_provider_settings(self) -> None:
        res = self._read()
        openrouter = next(p for p in res["providers"] if p["name"] == "OpenRouter")
        self.assertEqual(openrouter["settings"]["apiKey"], "<redacted>")
        self.assertEqual(
            openrouter["settings"]["baseUrl"], "https://openrouter.ai/api/v1"
        )
        self.assertNotIn("sk-or-secret-value", json.dumps(res))

    def test_marks_provider_source_support(self) -> None:
        res = self._read()
        by_name = {p["name"]: p for p in res["providers"]}
        self.assertEqual(by_name["OpenRouter"]["provider_kind"], "openrouter")
        self.assertTrue(by_name["OpenRouter"]["provider_supported"])
        self.assertEqual(
            by_name["OpenRouter"]["catalog_url"],
            "https://openrouter.ai/api/v1/models",
        )
        self.assertIsNone(by_name["oMLX"]["provider_kind"])
        self.assertFalse(by_name["oMLX"]["provider_supported"])
        self.assertEqual(
            res["summary"]["providers_with_supported_source"], ["openrouter"]
        )
        self.assertEqual(
            res["summary"]["providers_without_supported_source"], ["oMLX"]
        )

    def test_workspace_path_resolves_project_and_workspace(self) -> None:
        res = self._read(workspace_path=self.worktree_path)
        workspace = res["workspace"]
        self.assertTrue(workspace["matched"])
        self.assertEqual(workspace["project"]["id"], PROJECT_ID)
        self.assertEqual(workspace["workspace"]["id"], WORKSPACE_ID)
        self.assertEqual(workspace["workspace"]["checkout_kind"], "worktree")

    def test_workspace_path_prefers_longest_prefix(self) -> None:
        res = self._read(workspace_path=os.path.join(self.worktree_path, "src"))
        self.assertEqual(res["workspace"]["workspace"]["id"], WORKSPACE_ID)

    def test_unknown_workspace_path_is_unmatched(self) -> None:
        res = self._read(workspace_path=os.path.join(self.tmpdir.name, "elsewhere"))
        self.assertFalse(res["workspace"]["matched"])
        self.assertIsNone(res["workspace"]["project"])
        self.assertIsNone(res["workspace"]["workspace"])

    def test_workspace_path_is_optional(self) -> None:
        self.assertIsNone(self._read()["workspace"])

    def test_catalog_cache_can_be_excluded(self) -> None:
        res = self._read(include_catalog_cache=False)
        self.assertFalse(res["catalog_cache"]["included"])
        self.assertEqual(res["catalog_cache"]["byok_entry_count"], 0)

    def test_catalog_cache_includes_only_byok_entries(self) -> None:
        res = self._read()
        self.assertTrue(res["catalog_cache"]["included"])
        self.assertEqual(res["catalog_cache"]["byok_entry_count"], 1)
        entry = res["catalog_cache"]["byok_entries"][0]
        self.assertEqual(entry["providerId"], PROVIDER_ID)
        self.assertEqual(entry["supportedReasoningEfforts"], ["max", "high", "low"])

    def test_catalog_cache_surfaces_limits_per_entry(self) -> None:
        """The agent must be able to see whether the cache patch landed."""
        res = self._read()
        entry = res["catalog_cache"]["byok_entries"][0]
        self.assertFalse(entry["has_limits"])
        self.assertIsNone(entry["max_context_window_tokens"])
        self.assertEqual(res["catalog_cache"]["byok_entries_with_limits"], 0)
        self.assertTrue(
            any("capabilities.limits" in note for note in res["notes"]),
            f"expected a limits note, got {res['notes']}",
        )

    def test_catalog_cache_reports_both_caches(self) -> None:
        res = self._read()
        caches = res["catalog_cache"]["caches"]
        self.assertEqual(
            set(caches), {server.APP_STATE_CATALOG_KEY, server.APP_STATE_CLOUD_CATALOG_KEY}
        )
        self.assertEqual(caches[server.APP_STATE_CATALOG_KEY]["shape"], "object")
        self.assertEqual(caches[server.APP_STATE_CATALOG_KEY]["byok_entry_count"], 1)
        self.assertFalse(caches[server.APP_STATE_CLOUD_CATALOG_KEY]["present"])

    def test_limits_are_surfaced_when_present(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE app_state SET value = ? WHERE key = ?",
            (
                json.dumps(
                    {
                        "accountId": "acct",
                        "models": [
                            {
                                "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                                "name": "vendor/reasoning-model",
                                "providerId": PROVIDER_ID,
                                "capabilities": {
                                    "limits": {
                                        "max_context_window_tokens": 1000000,
                                        "max_prompt_tokens": 872000,
                                        "max_output_tokens": 128000,
                                    }
                                },
                            }
                        ],
                    }
                ),
                server.APP_STATE_CATALOG_KEY,
            ),
        )
        conn.commit()
        conn.close()

        res = self._read()
        entry = res["catalog_cache"]["byok_entries"][0]
        self.assertTrue(entry["has_limits"])
        self.assertEqual(entry["max_context_window_tokens"], 1000000)
        self.assertEqual(entry["max_prompt_tokens"], 872000)
        self.assertEqual(entry["max_output_tokens"], 128000)
        self.assertEqual(res["catalog_cache"]["byok_entries_with_limits"], 1)
        self.assertFalse(any("capabilities.limits" in note for note in res["notes"]))

    def _seed_catalog(self, entries: list[dict]) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_CATALOG_KEY,
                json.dumps({"accountId": "acct", "models": entries}),
            ),
        )
        conn.commit()
        conn.close()

    def test_limits_note_asks_for_apply_when_patchable(self) -> None:
        self._seed_catalog(
            [
                {
                    "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                    "name": "vendor/reasoning-model",
                    "providerId": PROVIDER_ID,
                    "capabilities": {"supports": {"reasoningEffort": True}},
                }
            ]
        )

        res = self._read()
        self.assertEqual(res["catalog_cache"]["byok_entries_with_limits"], 0)
        self.assertTrue(
            any("Run apply_metadata_updates" in note for note in res["notes"]),
            res["notes"],
        )

    def test_limits_note_is_expected_when_no_patchable_provider(self) -> None:
        self._seed_catalog(
            [
                {
                    "id": f"{LOCAL_PROVIDER_ID}/local-model",
                    "name": "local-model",
                    "providerId": LOCAL_PROVIDER_ID,
                    "capabilities": {"supports": {"reasoningEffort": False}},
                }
            ]
        )

        res = self._read()
        self.assertEqual(res["catalog_cache"]["byok_entries_with_limits"], 0)
        self.assertFalse(
            any("Run apply_metadata_updates" in note for note in res["notes"]),
            res["notes"],
        )
        self.assertTrue(
            any("nothing to patch" in note for note in res["notes"]), res["notes"]
        )

    def test_missing_database_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            server.read_byok_config(
                db_path=os.path.join(self.tmpdir.name, "absent.db")
            )


class RedactSettingsTest(unittest.TestCase):
    def test_masks_credential_keys(self) -> None:
        redacted = server.redact_settings(
            {
                "apiKey": "secret",
                "api_key": "secret",
                "Authorization": "Bearer secret",
                "accessToken": "secret",
                "baseUrl": "https://example.test/v1",
                "wireApi": "completions",
            }
        )
        self.assertEqual(redacted["apiKey"], "<redacted>")
        self.assertEqual(redacted["api_key"], "<redacted>")
        self.assertEqual(redacted["Authorization"], "<redacted>")
        self.assertEqual(redacted["accessToken"], "<redacted>")
        self.assertEqual(redacted["baseUrl"], "https://example.test/v1")
        self.assertEqual(redacted["wireApi"], "completions")

    def test_preserves_empty_values(self) -> None:
        self.assertEqual(server.redact_settings({"apiKey": ""})["apiKey"], "")


class LoadAppStateScalarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_reads_bare_string(self) -> None:
        self.conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('k', 'provider/model')"
        )
        self.assertEqual(
            server.load_app_state_scalar(self.conn, "k"), "provider/model"
        )

    def test_reads_json_string(self) -> None:
        self.conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('k', ?)",
            (json.dumps("provider/model"),),
        )
        self.assertEqual(
            server.load_app_state_scalar(self.conn, "k"), "provider/model"
        )

    def test_missing_key_returns_none(self) -> None:
        self.assertIsNone(server.load_app_state_scalar(self.conn, "absent"))


if __name__ == "__main__":
    unittest.main()
