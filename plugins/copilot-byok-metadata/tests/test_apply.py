#!/usr/bin/env python3
"""Write-path tests for apply_metadata_updates against a temporary database."""

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
"""

CATALOG = {
    "vendor/reasoning-model": {
        "model_id": "vendor/reasoning-model",
        "max_context_window_tokens": 1000000,
        "max_output_tokens": 128000,
        "supported_reasoning_efforts": ["max", "high", "low"],
        "default_reasoning_effort": "high",
    },
    "vendor/plain-model": {
        "model_id": "vendor/plain-model",
        "max_context_window_tokens": 200000,
        "max_output_tokens": 40000,
        "supported_reasoning_efforts": None,
        "default_reasoning_effort": None,
    },
}


class ApplyMetadataUpdatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "data.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) VALUES (?, ?, ?, ?)",
            (
                PROVIDER_ID,
                "OpenRouter",
                "custom",
                json.dumps({"baseUrl": "https://openrouter.ai/api/v1"}),
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
        for row_id, model_id in (
            ("row-reasoning", "vendor/reasoning-model"),
            ("row-plain", "vendor/plain-model"),
            ("row-local", "local-model"),
        ):
            provider = LOCAL_PROVIDER_ID if model_id == "local-model" else PROVIDER_ID
            conn.execute(
                "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
                "VALUES (?, ?, ?, ?)",
                (row_id, provider, model_id, model_id),
            )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_REASONING_KEY, json.dumps({"unrelated/model": "low"})),
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _rows(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {
            row["model_id"]: dict(row)
            for row in conn.execute("SELECT * FROM provider_models")
        }
        conn.close()
        return rows

    def _app_state(self, key: str):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        conn.close()
        return None if row is None else json.loads(row[0])

    def test_dry_run_writes_nothing(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=CATALOG, dry_run=True
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["applied"]["models_updated"], 0)
        self.assertIsNone(result["backup_path"])
        rows = self._rows()
        self.assertIsNone(rows["vendor/reasoning-model"]["max_prompt_tokens"])

    def test_apply_writes_metadata_and_backup(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=CATALOG, dry_run=False
        )
        self.assertEqual(result["applied"]["models_updated"], 2)
        self.assertIsNotNone(result["backup_path"])
        self.assertTrue(os.path.exists(result["backup_path"]))

        rows = self._rows()
        reasoning = rows["vendor/reasoning-model"]
        self.assertEqual(reasoning["max_prompt_tokens"], 872000)
        self.assertEqual(reasoning["max_output_tokens"], 128000)
        self.assertEqual(
            json.loads(reasoning["supported_reasoning_efforts"]),
            ["max", "high", "low"],
        )

        plain = rows["vendor/plain-model"]
        self.assertEqual(plain["max_prompt_tokens"], 160000)
        self.assertEqual(plain["max_output_tokens"], 40000)
        self.assertIsNone(plain["supported_reasoning_efforts"])

        local = rows["local-model"]
        self.assertIsNone(local["max_prompt_tokens"])

    def test_apply_is_idempotent(self) -> None:
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=CATALOG, dry_run=False, backup=False
        )
        second = server.apply_metadata_updates(
            db_path=self.db_path, catalog=CATALOG, dry_run=False, backup=False
        )
        self.assertEqual(second["applied"]["models_updated"], 0)
        self.assertIn("Nothing to apply", " ".join(second["notes"]))

    def test_model_ids_limits_scope(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            model_ids=["vendor/plain-model"],
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["models_updated"], 1)
        rows = self._rows()
        self.assertIsNone(rows["vendor/reasoning-model"]["max_prompt_tokens"])
        self.assertEqual(rows["vendor/plain-model"]["max_prompt_tokens"], 160000)

    def test_set_reasoning_defaults_merges_app_state(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_reasoning_defaults=True,
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["app_state_entries_updated"], 1)
        state = self._app_state(server.APP_STATE_REASONING_KEY)
        self.assertEqual(state["unrelated/model"], "low")
        self.assertEqual(state[f"{PROVIDER_ID}/vendor/reasoning-model"], "high")
        self.assertNotIn(f"{PROVIDER_ID}/vendor/plain-model", state)

    def test_set_context_tier_writes_app_state(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["app_state_entries_updated"], 2)
        state = self._app_state(server.APP_STATE_CONTEXT_TIER_KEY)
        self.assertEqual(state[f"{PROVIDER_ID}/vendor/reasoning-model"], "long_context")
        self.assertEqual(state[f"{PROVIDER_ID}/vendor/plain-model"], "long_context")

    def test_refresh_catalog_cache_patches_byok_entries(self) -> None:
        cache = {
            "accountId": "acct",
            "models": [
                {
                    "id": "vendor/reasoning-model",
                    "name": "Reasoning",
                    "providerId": PROVIDER_ID,
                    "capabilities": {"supports": {"reasoningEffort": False}},
                },
                {
                    "id": "claude-sonnet-5",
                    "name": "Claude",
                    "capabilities": {"limits": {"max_context_window_tokens": 1000000}},
                },
            ],
        }
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_CATALOG_KEY, json.dumps(cache)),
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            dry_run=False,
            backup=False,
            refresh_catalog_cache=True,
        )
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 1)

        patched = self._app_state(server.APP_STATE_CATALOG_KEY)
        byok = patched["models"][0]
        self.assertEqual(byok["capabilities"]["limits"]["max_prompt_tokens"], 872000)
        self.assertEqual(byok["capabilities"]["limits"]["max_output_tokens"], 128000)
        self.assertEqual(
            byok["capabilities"]["limits"]["max_context_window_tokens"], 1000000
        )
        self.assertTrue(byok["capabilities"]["supports"]["reasoningEffort"])
        self.assertEqual(byok["supportedReasoningEfforts"], ["max", "high", "low"])
        self.assertEqual(byok["defaultReasoningEffort"], "high")

        builtin = patched["models"][1]
        self.assertEqual(
            builtin["capabilities"]["limits"], {"max_context_window_tokens": 1000000}
        )

    def test_missing_database_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            server.apply_metadata_updates(
                db_path=os.path.join(self.tmpdir.name, "nope.db"), catalog=CATALOG
            )


if __name__ == "__main__":
    unittest.main()
