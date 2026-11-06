#!/usr/bin/env python3
"""Write-path tests for apply_metadata_updates against a temporary database."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
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
    supports_vision INTEGER NOT NULL DEFAULT 0,
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
                    # The app stores the full "<providerId>/<modelId>" key in `id`.
                    "id": f"{PROVIDER_ID}/vendor/reasoning-model",
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

    def test_catalog_cache_accepts_bare_id_entries(self) -> None:
        """Entries whose `id` lacks the provider prefix must still be patched."""
        cache = {
            "models": [
                {
                    "id": "vendor/reasoning-model",
                    "name": "Reasoning",
                    "providerId": PROVIDER_ID,
                    "capabilities": {"supports": {"reasoningEffort": False}},
                }
            ]
        }
        updates = {
            f"{PROVIDER_ID}/vendor/reasoning-model": {
                "max_prompt_tokens": 872000,
                "max_output_tokens": 128000,
                "max_context_window_tokens": 1000000,
                "supported_reasoning_efforts": ["max", "high", "low"],
                "default_reasoning_effort": "high",
            }
        }
        patched_cache, patched = server._patch_catalog_cache(cache, updates)
        self.assertEqual(patched, 1)
        limits = patched_cache["models"][0]["capabilities"]["limits"]
        self.assertEqual(limits["max_context_window_tokens"], 1000000)

    def test_catalog_cache_does_not_double_provider_prefix(self) -> None:
        """A full slash key must not become "<pid>/<pid>/<mid>"."""
        cache = {
            "models": [
                {
                    "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                    "name": "Reasoning",
                    "providerId": PROVIDER_ID,
                    "capabilities": {"supports": {"reasoningEffort": False}},
                }
            ]
        }
        updates = {
            f"{PROVIDER_ID}/vendor/reasoning-model": {
                "max_prompt_tokens": 872000,
                "max_output_tokens": 128000,
                "max_context_window_tokens": 1000000,
                "supported_reasoning_efforts": ["max", "high", "low"],
                "default_reasoning_effort": "high",
            }
        }
        patched_cache, patched = server._patch_catalog_cache(cache, updates)
        self.assertEqual(patched, 1)
        self.assertEqual(
            patched_cache["models"][0]["id"],
            f"{PROVIDER_ID}/vendor/reasoning-model",
        )

    def test_refresh_catalog_cache_warns_when_nothing_matched(self) -> None:
        cache = {
            "models": [
                {
                    "id": f"{PROVIDER_ID}/some-other-model",
                    "name": "Other",
                    "providerId": PROVIDER_ID,
                }
            ]
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
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 0)
        self.assertTrue(
            any("WARNING" in note for note in result["notes"]),
            f"expected a WARNING note, got {result['notes']}",
        )

    def test_missing_database_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            server.apply_metadata_updates(
                db_path=os.path.join(self.tmpdir.name, "nope.db"), catalog=CATALOG
            )

    def test_cloud_catalog_bare_list_is_patched(self) -> None:
        """The cloud catalog is a bare JSON list, not {"models": [...]}."""
        cloud = [
            {
                "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                "name": "Reasoning",
                "providerId": PROVIDER_ID,
                "capabilities": {"supports": {"reasoningEffort": False}},
            },
            {
                "id": "claude-sonnet-5",
                "name": "Claude",
                "capabilities": {"limits": {"max_context_window_tokens": 1000000}},
            },
        ]
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_CLOUD_CATALOG_KEY, json.dumps(cloud)),
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
        self.assertEqual(
            result["applied"]["catalog_caches_patched"],
            {server.APP_STATE_CLOUD_CATALOG_KEY: 1},
        )

        patched = self._app_state(server.APP_STATE_CLOUD_CATALOG_KEY)
        self.assertIsInstance(patched, list)
        limits = patched[0]["capabilities"]["limits"]
        self.assertEqual(limits["max_context_window_tokens"], 1000000)
        self.assertEqual(limits["max_prompt_tokens"], 872000)
        self.assertEqual(patched[1]["capabilities"]["limits"], {"max_context_window_tokens": 1000000})

    def test_both_catalogs_are_patched_and_counted(self) -> None:
        entry = {
            "id": f"{PROVIDER_ID}/vendor/reasoning-model",
            "name": "Reasoning",
            "providerId": PROVIDER_ID,
            "capabilities": {"supports": {"reasoningEffort": False}},
        }
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_CATALOG_KEY, json.dumps({"models": [dict(entry)]})),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_CLOUD_CATALOG_KEY, json.dumps([dict(entry)])),
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
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 2)
        self.assertEqual(
            result["applied"]["catalog_caches_patched"],
            {server.APP_STATE_CATALOG_KEY: 1, server.APP_STATE_CLOUD_CATALOG_KEY: 1},
        )
        self.assertFalse(any("WARNING" in note for note in result["notes"]))

    def test_patch_catalog_cache_ignores_unknown_shapes(self) -> None:
        for value in (None, "text", 42, {"no_models": []}):
            patched_cache, patched = server._patch_catalog_cache(value, {"a/b": {}})
            self.assertEqual(patched, 0)
            self.assertIs(patched_cache, value)

    def test_stale_cache_is_repaired_when_rows_are_already_correct(self) -> None:
        """The cache is a separate channel and can be stale on its own."""
        first = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            dry_run=False,
            backup=False,
            refresh_catalog_cache=False,
        )
        self.assertGreater(first["applied"]["models_updated"], 0)

        # Simulate the app rebuilding the catalog with no limits.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (
                server.APP_STATE_CATALOG_KEY,
                json.dumps(
                    {
                        "accountId": "acct",
                        "models": [
                            {
                                "id": f"{PROVIDER_ID}/vendor/reasoning-model",
                                "name": "Reasoning",
                                "providerId": PROVIDER_ID,
                                "capabilities": {"supports": {"reasoningEffort": False}},
                            }
                        ],
                    }
                ),
            ),
        )
        conn.commit()
        conn.close()

        second = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            dry_run=False,
            backup=False,
            refresh_catalog_cache=True,
        )
        self.assertEqual(second["applied"]["models_updated"], 0)
        self.assertEqual(second["applied"]["catalog_cache_entries_patched"], 1)
        self.assertNotIn("Nothing to apply", " ".join(second["notes"]))
        patched = self._app_state(server.APP_STATE_CATALOG_KEY)
        self.assertEqual(
            patched["models"][0]["capabilities"]["limits"]["max_context_window_tokens"],
            1000000,
        )

    def test_refresh_catalog_cache_defaults_to_true(self) -> None:
        """The context window only reaches the app through the cache."""
        schema = next(
            tool
            for tool in server.TOOLS
            if tool["name"] == "apply_metadata_updates"
        )["inputSchema"]["properties"]["refresh_catalog_cache"]
        self.assertTrue(schema["default"])

    def _settle_rows(self) -> dict:
        """Write the catalog once so every row already matches the provider."""
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=CATALOG, dry_run=False, backup=False
        )
        return {
            model_id: row["updated_at"] for model_id, row in self._rows().items()
        }

    def test_app_state_only_run_reports_no_model_update(self) -> None:
        """A tier-only run writes app state, never a provider_models row."""
        before = self._settle_rows()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )

        self.assertEqual(result["applied"]["models_updated"], 0)
        self.assertGreaterEqual(result["applied"]["app_state_entries_updated"], 1)
        self.assertEqual(
            self._app_state(server.APP_STATE_CONTEXT_TIER_KEY)[
                f"{PROVIDER_ID}/vendor/reasoning-model"
            ],
            "long_context",
        )
        self.assertEqual(
            {model_id: row["updated_at"] for model_id, row in self._rows().items()},
            before,
        )

    def test_app_state_only_run_still_takes_a_backup(self) -> None:
        """App state is a real write to the same database, so it stays backed up."""
        self._settle_rows()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=True,
        )

        self.assertIsNotNone(result["backup_path"])
        self.assertTrue(os.path.exists(result["backup_path"]))
        self.assertIn(
            ".byok-metadata-backup-", os.path.basename(result["backup_path"])
        )

    def test_row_plus_app_state_on_one_entry_counts_one(self) -> None:
        """One entry can need both a row write and an app-state change."""
        self._settle_rows()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE provider_models SET max_prompt_tokens = 1, updated_at = '2020-01-01T00:00:00.000Z' "
            "WHERE model_id = 'vendor/reasoning-model'"
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )

        self.assertEqual(result["applied"]["models_updated"], 1)
        self.assertGreaterEqual(result["applied"]["app_state_entries_updated"], 1)
        row = self._rows()["vendor/reasoning-model"]
        self.assertEqual(row["max_prompt_tokens"], 872000)
        self.assertNotEqual(row["updated_at"], "2020-01-01T00:00:00.000Z")

    def test_mixed_entries_count_only_row_writes(self) -> None:
        """Only entries with real changes reach the write loop."""
        before = self._settle_rows()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE provider_models SET max_prompt_tokens = 1, updated_at = '2020-01-01T00:00:00.000Z' "
            "WHERE model_id = 'vendor/reasoning-model'"
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )

        self.assertEqual(result["applied"]["models_updated"], 1)
        rows = self._rows()
        self.assertNotEqual(
            rows["vendor/reasoning-model"]["updated_at"], "2020-01-01T00:00:00.000Z"
        )
        self.assertEqual(
            rows["vendor/plain-model"]["updated_at"], before["vendor/plain-model"]
        )

    def test_dry_run_on_app_state_only_plan_writes_nothing(self) -> None:
        self._settle_rows()

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=True,
        )

        self.assertEqual(result["applied"]["models_updated"], 0)
        self.assertEqual(result["applied"]["app_state_entries_updated"], 0)
        self.assertIsNone(self._app_state(server.APP_STATE_CONTEXT_TIER_KEY))

    def test_second_identical_app_state_only_run_is_a_no_op(self) -> None:
        self._settle_rows()
        first = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )
        self.assertGreaterEqual(first["applied"]["app_state_entries_updated"], 1)

        second = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=False,
            backup=False,
        )

        self.assertEqual(second["applied"]["models_updated"], 0)
        self.assertEqual(second["applied"]["app_state_entries_updated"], 0)
        self.assertIn("Nothing to apply", " ".join(second["notes"]))

    def test_plan_summary_update_row_is_a_subset_of_update(self) -> None:
        self._settle_rows()
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=CATALOG,
            set_context_tier="long_context",
            dry_run=True,
        )
        summary = result["plan"]["summary"]

        # App-state-only work is planned but needs no row write.
        self.assertGreater(summary["update"], 0)
        self.assertEqual(summary["update_row"], 0)
        self.assertLessEqual(summary["update_row"], summary["update"])
        self.assertEqual(
            sum(
                summary[key]
                for key in (
                    "update",
                    "unchanged",
                    "unresolved",
                    "unsupported_provider",
                    "unsupported_model_type",
                )
            ),
            summary["total"],
        )


class ApplyVisionFlagsTest(unittest.TestCase):
    """Vision is a one-way flag: only definitive `image`/`vlm` evidence writes it."""

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
        for row_id, provider_id, model_id in (
            ("row-vlm", PROVIDER_ID, "vendor/vlm"),
            ("row-text", PROVIDER_ID, "vendor/text"),
            ("row-local-vlm", LOCAL_PROVIDER_ID, "flint-oQ4e"),
            ("row-local-text", LOCAL_PROVIDER_ID, "reranker-oQ4e"),
        ):
            conn.execute(
                "INSERT INTO provider_models (id, provider_id, model_id, display_name, "
                "max_prompt_tokens, max_output_tokens) VALUES (?, ?, ?, ?, 872000, 128000)",
                (row_id, provider_id, model_id, model_id),
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

    def _catalog(self, overrides: dict) -> dict:
        return {
            "vendor/vlm": {
                "model_id": "vendor/vlm",
                "max_context_window_tokens": 1000000,
                "max_output_tokens": 128000,
                "supports_vision": True,
                "vision_media_types": [
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "image/gif",
                ],
                "vision_source": "openrouter.architecture",
                **overrides.get("vendor/vlm", {}),
            },
            "vendor/text": {
                "model_id": "vendor/text",
                "max_context_window_tokens": 1000000,
                "max_output_tokens": 128000,
                "supports_vision": False,
                "vision_media_types": [],
                **overrides.get("vendor/text", {}),
            },
        }

    def _insert_cache(self, cache) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (server.APP_STATE_CATALOG_KEY, json.dumps(cache)),
        )
        conn.commit()
        conn.close()

    def _cache(self):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (server.APP_STATE_CATALOG_KEY,)
        ).fetchone()
        conn.close()
        return None if row is None else json.loads(row[0])

    def test_vision_model_sets_column_and_media_types(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["vision_flags_written"], 1)
        rows = self._rows()
        self.assertEqual(rows["vendor/vlm"]["supports_vision"], 1)
        self.assertEqual(rows["vendor/text"]["supports_vision"], 0)

    def test_text_only_model_leaves_column_and_cache_untouched(self) -> None:
        self._insert_cache(
            {
                "models": [
                    {
                        "id": f"{PROVIDER_ID}/vendor/text",
                        "name": "Text",
                        "providerId": PROVIDER_ID,
                        "capabilities": {"supports": {"reasoningEffort": False}},
                    }
                ]
            }
        )
        server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(self._rows()["vendor/text"]["supports_vision"], 0)
        entry = self._cache()["models"][0]
        self.assertNotIn("vision", entry["capabilities"]["supports"])
        self.assertNotIn("vision", entry["capabilities"]["limits"])

        report = server.plan_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({})
        )
        text_entry = next(
            entry for entry in report["entries"] if entry["model_id"] == "vendor/text"
        )
        self.assertIs(text_entry["proposed"]["supports_vision"], False)
        self.assertNotIn("supports_vision", text_entry["changes"])

    def test_cache_gains_vision_supports_and_limits(self) -> None:
        self._insert_cache(
            {
                "models": [
                    {
                        "id": f"{PROVIDER_ID}/vendor/vlm",
                        "name": "VLM",
                        "providerId": PROVIDER_ID,
                        "capabilities": {"supports": {"reasoningEffort": False}},
                    }
                ]
            }
        )
        server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        capabilities = self._cache()["models"][0]["capabilities"]
        self.assertIs(capabilities["supports"]["vision"], True)
        self.assertEqual(
            capabilities["limits"]["vision"]["supported_media_types"],
            ["image/jpeg", "image/png", "image/webp", "image/gif"],
        )
        # Neither provider publishes these, so they must never be invented.
        self.assertNotIn("max_prompt_images", capabilities["limits"]["vision"])
        self.assertNotIn("max_prompt_image_size", capabilities["limits"]["vision"])

    def test_second_identical_run_reports_nothing_to_apply(self) -> None:
        self._insert_cache(
            {
                "models": [
                    {
                        "id": f"{PROVIDER_ID}/vendor/vlm",
                        "name": "VLM",
                        "providerId": PROVIDER_ID,
                        "capabilities": {"supports": {"reasoningEffort": False}},
                    }
                ]
            }
        )
        server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        second = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(second["applied"]["vision_flags_written"], 0)
        self.assertEqual(second["applied"]["catalog_cache_entries_patched"], 0)
        self.assertIn("Nothing to apply", " ".join(second["notes"]))

    def test_converting_vision_to_text_does_not_reset_the_column(self) -> None:
        server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(self._rows()["vendor/vlm"]["supports_vision"], 1)

        converted = self._catalog(
            {"vendor/vlm": {"supports_vision": False, "vision_media_types": []}}
        )
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=converted,
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["vision_flags_written"], 0)
        self.assertEqual(self._rows()["vendor/vlm"]["supports_vision"], 1)
        self.assertNotIn(
            "supports_vision", result["plan"]["entries"][0]["changes"]
        )

    def test_omlx_vlm_is_flagged_from_model_type(self) -> None:
        catalog = {
            "flint-oQ4e": server.normalize_omlx_model(
                {"id": "flint-oQ4e", "max_model_len": 262144},
                {"id": "flint-oQ4e", "model_type": "vlm", "max_tokens": 32768},
            ),
            "reranker-oQ4e": server.normalize_omlx_model(
                {"id": "reranker-oQ4e"}, {"id": "reranker-oQ4e", "model_type": "reranker"}
            ),
        }
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=catalog,
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["vision_flags_written"], 1)
        rows = self._rows()
        self.assertEqual(rows["flint-oQ4e"]["supports_vision"], 1)
        self.assertEqual(rows["reranker-oQ4e"]["supports_vision"], 0)

    def test_pre_vision_database_skips_the_column_instead_of_raising(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE legacy_provider_models (
                id TEXT PRIMARY KEY NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                wire_model TEXT,
                display_name TEXT NOT NULL,
                max_prompt_tokens INTEGER,
                max_output_tokens INTEGER,
                wire_api_override TEXT,
                created_at TEXT,
                updated_at TEXT,
                supported_reasoning_efforts TEXT,
                UNIQUE (provider_id, model_id)
            );
            INSERT INTO legacy_provider_models SELECT
                id, provider_id, model_id, wire_model, display_name,
                max_prompt_tokens, max_output_tokens, wire_api_override,
                created_at, updated_at, supported_reasoning_efforts
            FROM provider_models;
            DROP TABLE provider_models;
            ALTER TABLE legacy_provider_models RENAME TO provider_models;
            """
        )
        conn.commit()
        conn.close()

        plan = server.plan_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({})
        )
        vision_entry = next(
            entry for entry in plan["entries"] if entry["model_id"] == "vendor/vlm"
        )
        self.assertIsNone(vision_entry["current"]["supports_vision"])
        self.assertIn("supports_vision", vision_entry["changes"])

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["vision_flags_written"], 0)
        self.assertGreaterEqual(result["applied"]["models_updated"], 1)
        self.assertNotIn(
            "supports_vision", self._rows()["vendor/vlm"]
        )

    def test_pre_vision_database_settles_instead_of_reapplying_forever(self) -> None:
        """A schema without the column must converge, not rewrite every run.

        ``build_plan`` always proposes ``supports_vision`` because the current
        value reads as NULL, but the column does not exist to store it. Without
        a fallback that still compares the remaining columns, every run counts
        a row update, so the tool never reports "Nothing to apply" and keeps
        writing a backup each time.
        """
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE legacy_provider_models (
                id TEXT PRIMARY KEY NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                wire_model TEXT,
                display_name TEXT NOT NULL,
                max_prompt_tokens INTEGER,
                max_output_tokens INTEGER,
                wire_api_override TEXT,
                created_at TEXT,
                updated_at TEXT,
                supported_reasoning_efforts TEXT,
                UNIQUE (provider_id, model_id)
            );
            INSERT INTO legacy_provider_models SELECT
                id, provider_id, model_id, wire_model, display_name,
                max_prompt_tokens, max_output_tokens, wire_api_override,
                created_at, updated_at, supported_reasoning_efforts
            FROM provider_models;
            DROP TABLE provider_models;
            ALTER TABLE legacy_provider_models RENAME TO provider_models;
            """
        )
        conn.commit()
        conn.close()
        self._insert_cache(
            {
                "models": [
                    {
                        "id": f"{PROVIDER_ID}/vendor/vlm",
                        "providerId": PROVIDER_ID,
                        "capabilities": {"supports": {}},
                    }
                ]
            }
        )

        first = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False
        )
        self.assertGreaterEqual(first["applied"]["models_updated"], 1)
        self.assertEqual(first["applied"]["vision_flags_written"], 0)

        second = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False
        )
        self.assertEqual(second["applied"]["models_updated"], 0)
        self.assertIsNone(second["backup_path"])
        self.assertIn("Nothing to apply", " ".join(second["notes"]))

    def test_in_sync_cache_with_row_update_does_not_warn(self) -> None:
        """The WARNING is about an unmatched cache, not about unsaved cache edits.

        Once the cache already carries the provider values, a row update must
        still leave the cache patch count at zero *without* claiming the app
        will miss the metadata.
        """
        self._insert_cache(
            {
                "models": [
                    {
                        "id": f"{PROVIDER_ID}/vendor/vlm",
                        "providerId": PROVIDER_ID,
                        "capabilities": {
                            "supports": {"reasoningEffort": False, "vision": True},
                            "limits": {
                                "max_prompt_tokens": 872000,
                                "max_output_tokens": 128000,
                                "max_context_window_tokens": 1000000,
                                "vision": {
                                    "supported_media_types": [
                                        "image/jpeg",
                                        "image/png",
                                        "image/webp",
                                        "image/gif",
                                    ]
                                },
                            },
                        },
                    }
                ]
            }
        )

        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertGreaterEqual(result["applied"]["models_updated"], 1)
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 0)
        self.assertFalse(
            any("WARNING" in note for note in result["notes"]),
            f"cache was already in sync, got {result['notes']}",
        )

    def _drop_vision_column(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE legacy_provider_models (
                id TEXT PRIMARY KEY NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                wire_model TEXT,
                display_name TEXT NOT NULL,
                max_prompt_tokens INTEGER,
                max_output_tokens INTEGER,
                wire_api_override TEXT,
                created_at TEXT,
                updated_at TEXT,
                supported_reasoning_efforts TEXT,
                UNIQUE (provider_id, model_id)
            );
            INSERT INTO legacy_provider_models SELECT
                id, provider_id, model_id, wire_model, display_name,
                max_prompt_tokens, max_output_tokens, wire_api_override,
                created_at, updated_at, supported_reasoning_efforts
            FROM provider_models;
            DROP TABLE provider_models;
            ALTER TABLE legacy_provider_models RENAME TO provider_models;
            """
        )
        conn.commit()
        conn.close()

    def test_legacy_suppression_holds_when_another_model_has_real_work(self) -> None:
        """A suppressed row must stay out of the write loop, not just the guard.

        The suppression set only decides whether the run is a no-op. If the
        write loop still walks every planned row, any unrelated pending change
        (here another model's stale tokens) silently re-counts and re-writes the
        suppressed row, so the backup and the inflated ``models_updated`` come
        back and the next run repeats it.
        """
        self._drop_vision_column()
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        settled = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(settled["applied"]["models_updated"], 0)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE provider_models SET max_prompt_tokens = 1 WHERE model_id = 'vendor/text'"
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(result["applied"]["models_updated"], 1)

    def test_legacy_suppression_holds_when_app_state_work_opens_the_write_path(
        self,
    ) -> None:
        """An unrelated app-state change must not rewrite suppressed rows.

        App-state-only work never counts as a row write, so the suppressed row
        must neither be counted nor given a fresh ``updated_at``.
        """
        self._drop_vision_column()
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(
            server.apply_metadata_updates(
                db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
            )["applied"]["models_updated"],
            0,
        )
        before = self._rows()["vendor/vlm"]["updated_at"]

        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog({}),
            dry_run=False,
            backup=False,
            set_context_tier="long_context",
        )
        self.assertTrue(result["applied"]["app_state_entries_updated"] >= 1)
        self.assertEqual(result["applied"]["models_updated"], 0)
        self.assertEqual(self._rows()["vendor/vlm"]["updated_at"], before)

    def test_suppressed_row_still_applies_a_real_token_change(self) -> None:
        """Suppression covers only the unstorable vision flag, never real work."""
        self._drop_vision_column()
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE provider_models SET max_prompt_tokens = 1 WHERE model_id = 'vendor/vlm'"
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(result["applied"]["models_updated"], 1)
        self.assertEqual(self._rows()["vendor/vlm"]["max_prompt_tokens"], 872000)

    def test_vision_column_added_later_un_suppresses_the_row(self) -> None:
        """An app upgrade that adds the column must let vision be written."""
        self._drop_vision_column()
        server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(
            server.apply_metadata_updates(
                db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
            )["applied"]["models_updated"],
            0,
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "ALTER TABLE provider_models ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        conn.close()

        result = server.apply_metadata_updates(
            db_path=self.db_path, catalog=self._catalog({}), dry_run=False, backup=False
        )
        self.assertEqual(result["applied"]["vision_flags_written"], 1)
        self.assertEqual(self._rows()["vendor/vlm"]["supports_vision"], 1)



class ApplyNoReasoningEvidenceTest(unittest.TestCase):
    """No definitive reasoning evidence must never clear stored metadata.

    A 401 from oMLX's admin surface is absence of evidence, not evidence that
    the model lost its effort vocabulary. The ``provider_models`` column and
    both catalog-cache arrays must survive such a run untouched.
    """

    PREVIOUS_EFFORTS = ["low", "medium", "high"]

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "data.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) "
            "VALUES (?, ?, ?, ?)",
            (
                LOCAL_PROVIDER_ID,
                "oMLX",
                "ollama",
                json.dumps(
                    {
                        "baseUrl": "http://127.0.0.1:8001/v1",
                        "authKind": "api_key",
                        "apiKey": "REALKEY",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO provider_models (id, provider_id, model_id, display_name, "
            "max_prompt_tokens, max_output_tokens, supported_reasoning_efforts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "row-flint",
                LOCAL_PROVIDER_ID,
                "flint-oQ4e",
                "flint-oQ4e",
                99328,
                32768,
                json.dumps(self.PREVIOUS_EFFORTS),
            ),
        )
        cache = {
            "accountId": "acct",
            "models": [
                {
                    "id": "flint-oQ4e",
                    "providerId": LOCAL_PROVIDER_ID,
                    "capabilities": {
                        "limits": {
                            "max_prompt_tokens": 99328,
                            "max_output_tokens": 32768,
                            "max_context_window_tokens": 131072,
                        },
                        "supports": {"reasoningEffort": True},
                    },
                    "supportedReasoningEfforts": list(self.PREVIOUS_EFFORTS),
                    "defaultReasoningEffort": "low",
                }
            ],
        }
        for key in server.CATALOG_CACHE_KEYS:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?)",
                (key, json.dumps(cache)),
            )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _install_admin_401(self) -> None:
        original = server.http_get_json
        self.addCleanup(setattr, server, "http_get_json", original)

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            if url.endswith("/models/status"):
                return {"models": [{"id": "flint-oQ4e", "model_type": "vlm"}]}
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        server.http_get_json = fake_get

    def test_401_run_leaves_stored_efforts_and_caches_untouched(self) -> None:
        self._install_admin_401()
        result = server.apply_metadata_updates(
            db_path=self.db_path, dry_run=False, backup=False
        )
        self.assertTrue(result["dry_run"] is False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT supported_reasoning_efforts FROM provider_models "
                "WHERE id = 'row-flint'"
            ).fetchone()
            caches = {
                key: json.loads(
                    conn.execute(
                        "SELECT value FROM app_state WHERE key = ?", (key,)
                    ).fetchone()["value"]
                )
                for key in server.CATALOG_CACHE_KEYS
            }
        finally:
            conn.close()

        self.assertEqual(
            json.loads(row["supported_reasoning_efforts"]), self.PREVIOUS_EFFORTS
        )
        for key, cache in caches.items():
            entry = cache["models"][0]
            self.assertEqual(
                entry["supportedReasoningEfforts"], self.PREVIOUS_EFFORTS, key
            )
            self.assertEqual(entry["defaultReasoningEffort"], "low", key)
            self.assertTrue(entry["capabilities"]["supports"]["reasoningEffort"], key)

    def test_401_plan_proposes_no_reasoning_change(self) -> None:
        self._install_admin_401()
        plan = server.plan_metadata_updates(db_path=self.db_path)
        entry = plan["entries"][0]
        self.assertNotIn("supported_reasoning_efforts", entry["changes"])
        self.assertFalse(entry["proposed"]["reasoning_efforts_definitive"])


class ApplyUnsupportedModelTypeTest(unittest.TestCase):
    """Non-chat oMLX models are never written, flag or no flag... unless asked.

    The excluded models are seeded with metadata that a normal plan would
    happily rewrite (their window differs), so "nothing happened" is a real
    assertion rather than a trivially clean fixture.
    """

    UNSUPPORTED = (
        ("jina-reranker-v3.5-mlx", "reranker"),
        ("Ming-3.0-tts-tiny-4bit", "audio_tts"),
        ("MarkItDown", "markitdown"),
    )

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "data.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) "
            "VALUES (?, ?, ?, ?)",
            (
                LOCAL_PROVIDER_ID,
                "oMLX",
                "ollama",
                json.dumps({"baseUrl": "http://127.0.0.1:8001/v1"}),
            ),
        )
        for model_id, _ in self.UNSUPPORTED:
            conn.execute(
                "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
                "VALUES (?, ?, ?, ?)",
                (f"row-{model_id}", LOCAL_PROVIDER_ID, model_id, model_id),
            )
        cache_models = [
            {
                "id": f"{LOCAL_PROVIDER_ID}/{model_id}",
                "name": model_id,
                "providerId": LOCAL_PROVIDER_ID,
                "capabilities": {},
            }
            for model_id, _ in self.UNSUPPORTED
        ]
        cache_models.append(
            {
                "id": f"{LOCAL_PROVIDER_ID}/flint-oQ4e",
                "name": "flint-oQ4e",
                "providerId": LOCAL_PROVIDER_ID,
                "capabilities": {},
            }
        )
        cache = {"accountId": "acct", "models": cache_models}
        for key in server.CATALOG_CACHE_KEYS:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?)",
                (key, json.dumps(cache)),
            )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _catalog(self) -> dict:
        return {
            model_id: server.normalize_omlx_model(
                {"id": model_id, "max_model_len": 8192},
                {"id": model_id, "model_type": model_type, "max_tokens": 2048},
                {
                    "id": model_id,
                    "model_type": model_type,
                    "reasoning_effort_options": ["low", "medium", "high", "max"],
                    "reasoning_effort_default": "low",
                },
            )
            for model_id, model_type in self.UNSUPPORTED
        }

    def _snapshot(self) -> tuple[dict, dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = {
                row["model_id"]: dict(row)
                for row in conn.execute("SELECT * FROM provider_models")
            }
            caches = {
                key: json.loads(
                    conn.execute(
                        "SELECT value FROM app_state WHERE key = ?", (key,)
                    ).fetchone()["value"]
                )
                for key in server.CATALOG_CACHE_KEYS
            }
        finally:
            conn.close()
        return rows, caches

    def test_apply_without_the_flag_writes_nothing(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog(),
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["models_updated"], 0)
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 0)
        self.assertIsNone(result["backup_path"])
        self.assertIn("Nothing to apply", " ".join(result["notes"]))
        self.assertTrue(
            all(
                entry["status"] == "unsupported_model_type"
                for entry in result["plan"]["entries"]
            )
        )

        rows, caches = self._snapshot()
        for model_id, _ in self.UNSUPPORTED:
            self.assertIsNone(rows[model_id]["max_prompt_tokens"], model_id)
            self.assertIsNone(rows[model_id]["max_output_tokens"], model_id)
            self.assertIsNone(
                rows[model_id]["supported_reasoning_efforts"], model_id
            )
        for key, cache in caches.items():
            for entry in cache["models"]:
                self.assertEqual(entry["capabilities"], {}, key)

    def test_no_backup_is_taken_for_a_plan_that_only_excludes_models(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog(),
            dry_run=False,
            backup=True,
        )
        self.assertIsNone(result["backup_path"])
        self.assertEqual(
            [
                name
                for name in os.listdir(self.tmpdir.name)
                if ".byok-metadata-backup-" in name
            ],
            [],
        )

    def test_flag_genuinely_writes_them(self) -> None:
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=self._catalog(),
            dry_run=False,
            backup=False,
            include_unsupported_model_types=True,
        )
        self.assertEqual(result["applied"]["models_updated"], 3)
        self.assertEqual(result["applied"]["catalog_cache_entries_patched"], 6)

        rows, caches = self._snapshot()
        for model_id, _ in self.UNSUPPORTED:
            self.assertEqual(rows[model_id]["max_prompt_tokens"], 6144, model_id)
            self.assertEqual(rows[model_id]["max_output_tokens"], 2048, model_id)
            # #3's trust gate still holds with the flag on: the admin endpoint
            # advertised a full level set, but a non-chat type never gets it.
            self.assertIsNone(
                rows[model_id]["supported_reasoning_efforts"], model_id
            )
        for key, cache in caches.items():
            by_id = {entry["id"]: entry for entry in cache["models"]}
            for model_id, _ in self.UNSUPPORTED:
                entry = by_id[f"{LOCAL_PROVIDER_ID}/{model_id}"]
                limits = entry["capabilities"]["limits"]
                self.assertEqual(limits["max_context_window_tokens"], 8192, key)
                self.assertEqual(limits["max_prompt_tokens"], 6144, key)
                self.assertEqual(limits["max_output_tokens"], 2048, key)
                self.assertNotIn("supportedReasoningEfforts", entry, key)

    def test_excluded_model_does_not_block_real_work(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
            "VALUES (?, ?, ?, ?)",
            ("row-flint", LOCAL_PROVIDER_ID, "flint-oQ4e", "flint-oQ4e"),
        )
        conn.commit()
        conn.close()

        catalog = self._catalog()
        catalog["flint-oQ4e"] = server.normalize_omlx_model(
            {"id": "flint-oQ4e", "max_model_len": 131072},
            {"id": "flint-oQ4e", "model_type": "llm", "max_tokens": 32768},
        )
        result = server.apply_metadata_updates(
            db_path=self.db_path,
            catalog=catalog,
            dry_run=False,
            backup=False,
        )
        self.assertEqual(result["applied"]["models_updated"], 1)
        rows, caches = self._snapshot()
        self.assertEqual(rows["flint-oQ4e"]["max_prompt_tokens"], 98304)
        for model_id, _ in self.UNSUPPORTED:
            self.assertIsNone(rows[model_id]["max_prompt_tokens"], model_id)
        # Only the chat model's cache entry was touched.
        for key, cache in caches.items():
            by_id = {entry["id"]: entry for entry in cache["models"]}
            self.assertIn(
                "limits", by_id[f"{LOCAL_PROVIDER_ID}/flint-oQ4e"]["capabilities"], key
            )
            for model_id, _ in self.UNSUPPORTED:
                self.assertEqual(
                    by_id[f"{LOCAL_PROVIDER_ID}/{model_id}"]["capabilities"],
                    {},
                    key,
                )


if __name__ == "__main__":
    unittest.main()
