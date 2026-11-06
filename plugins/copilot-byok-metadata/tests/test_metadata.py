#!/usr/bin/env python3
"""Unit tests for the pure metadata-mapping functions in the server."""

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

#: Fake key used by the pure-function tests below. It is only passed in by
#: callers; the tests that need a real DB row seed one instead.
TEST_API_KEY = "test-api-key"


class NormalizeOpenRouterModelTest(unittest.TestCase):
    def test_filters_efforts_to_copilot_vocabulary(self) -> None:
        entry = {
            "id": "anthropic/claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "context_length": 1000000,
            "top_provider": {"max_completion_tokens": 128000},
            "reasoning": {
                "supported_efforts": ["max", "xhigh", "high", "medium", "low", "minimal", "none"],
                "default_effort": "high",
            },
        }
        result = server.normalize_openrouter_model(entry)
        self.assertEqual(
            result["supported_reasoning_efforts"],
            ["max", "xhigh", "high", "medium", "low"],
        )
        self.assertEqual(result["default_reasoning_effort"], "high")
        self.assertTrue(result["supports_reasoning"])
        self.assertEqual(result["max_context_window_tokens"], 1000000)
        self.assertEqual(result["max_output_tokens"], 128000)

    def test_missing_reasoning_object_yields_no_efforts(self) -> None:
        entry = {
            "id": "typesafe/jev-router",
            "context_length": 1000000,
            "top_provider": {"max_completion_tokens": 65536},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertIsNone(result["default_reasoning_effort"])
        self.assertFalse(result["supports_reasoning"])

    def test_efforts_containing_only_unsupported_values_become_none(self) -> None:
        entry = {
            "id": "some/model",
            "context_length": 1000,
            "reasoning": {"supported_efforts": ["minimal", "none"]},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertFalse(result["supports_reasoning"])

    def test_default_effort_dropped_when_not_in_filtered_set(self) -> None:
        entry = {
            "id": "some/model",
            "context_length": 1000,
            "reasoning": {"supported_efforts": ["low", "high"], "default_effort": "minimal"},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertEqual(result["supported_reasoning_efforts"], ["low", "high"])
        self.assertIsNone(result["default_reasoning_effort"])

    def test_duplicate_efforts_are_collapsed_preserving_order(self) -> None:
        entry = {
            "id": "some/model",
            "context_length": 1000,
            "reasoning": {"supported_efforts": ["high", "low", "high"]},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertEqual(result["supported_reasoning_efforts"], ["high", "low"])

    def test_context_length_falls_back_to_top_provider(self) -> None:
        entry = {
            "id": "some/model",
            "top_provider": {"context_length": 200000, "max_completion_tokens": 1000},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertEqual(result["max_context_window_tokens"], 200000)


class OpenRouterVisionTest(unittest.TestCase):
    def test_image_modality_flags_vision_with_image_media_types(self) -> None:
        entry = {
            "id": "vendor/vlm",
            "context_length": 1000,
            "architecture": {"input_modalities": ["text", "image"]},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertTrue(result["supports_vision"])
        self.assertEqual(result["vision_source"], "openrouter.architecture")
        for media_type in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            self.assertIn(media_type, result["vision_media_types"])

    def test_text_only_modality_is_not_flagged(self) -> None:
        entry = {
            "id": "vendor/text",
            "context_length": 1000,
            "architecture": {"input_modalities": ["text"]},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertIs(result["supports_vision"], False)
        self.assertEqual(result["vision_media_types"], [])
        self.assertIsNone(result["vision_source"])

    def test_missing_architecture_leaves_vision_unknown(self) -> None:
        result = server.normalize_openrouter_model({"id": "vendor/x"})
        self.assertIsNone(result["supports_vision"])
        self.assertIsNone(result["vision_source"])
        self.assertEqual(result["vision_media_types"], [])

    def test_non_image_modalities_do_not_flag_vision(self) -> None:
        entry = {
            "id": "vendor/audio-file",
            "architecture": {"input_modalities": ["text", "file", "audio", "video"]},
        }
        result = server.normalize_openrouter_model(entry)
        self.assertIs(result["supports_vision"], False)
        self.assertEqual(
            result["vision_media_types"],
            [
                "application/pdf",
                "audio/wav",
                "audio/mpeg",
                "video/mp4",
            ],
        )

    def test_modality_mapping_deduplicates(self) -> None:
        self.assertEqual(
            server.vision_media_types(["image", "image", "text", "unknown"]),
            ["image/jpeg", "image/png", "image/webp", "image/gif"],
        )
        self.assertEqual(server.vision_media_types(None), [])


class DeriveCopilotLimitsTest(unittest.TestCase):
    def test_prompt_is_context_minus_output(self) -> None:
        limits = server.derive_copilot_limits(
            {
                "max_context_window_tokens": 1000000,
                "max_output_tokens": 128000,
                "supported_reasoning_efforts": ["high", "low"],
            }
        )
        self.assertEqual(limits["max_prompt_tokens"], 872000)
        self.assertEqual(limits["max_output_tokens"], 128000)
        self.assertEqual(limits["supported_reasoning_efforts"], ["high", "low"])

    def test_output_clamped_to_ceiling(self) -> None:
        limits = server.derive_copilot_limits(
            {"max_context_window_tokens": 1048576, "max_output_tokens": 943718}
        )
        self.assertEqual(limits["max_output_tokens"], server.MAX_OUTPUT_TOKENS_CEILING)
        self.assertEqual(
            limits["max_prompt_tokens"], 1048576 - server.MAX_OUTPUT_TOKENS_CEILING
        )

    def test_output_clamped_to_prompt_fraction(self) -> None:
        limits = server.derive_copilot_limits(
            {"max_context_window_tokens": 100000, "max_output_tokens": 90000}
        )
        self.assertEqual(limits["max_output_tokens"], 50000)
        self.assertEqual(limits["max_prompt_tokens"], 50000)

    def test_missing_output_leaves_prompt_as_full_window(self) -> None:
        limits = server.derive_copilot_limits({"max_context_window_tokens": 128000})
        self.assertIsNone(limits["max_output_tokens"])
        self.assertEqual(limits["max_prompt_tokens"], 128000)

    def test_missing_context_leaves_prompt_none(self) -> None:
        limits = server.derive_copilot_limits({"max_output_tokens": 4096})
        self.assertIsNone(limits["max_prompt_tokens"])
        self.assertEqual(limits["max_output_tokens"], 4096)


class DetectProviderKindTest(unittest.TestCase):
    def test_matches_openrouter_by_host(self) -> None:
        provider = {"name": "Anything", "base_url": "https://openrouter.ai/api/v1"}
        self.assertEqual(server.detect_provider_kind(provider), "openrouter")

    def test_matches_openrouter_by_subdomain(self) -> None:
        provider = {"name": "Anything", "base_url": "https://api.openrouter.ai/v1"}
        self.assertEqual(server.detect_provider_kind(provider), "openrouter")

    def test_matches_openrouter_by_name(self) -> None:
        provider = {"name": "OpenRouter", "base_url": "http://127.0.0.1:8001/v1"}
        self.assertEqual(server.detect_provider_kind(provider), "openrouter")

    def test_matches_omlx_by_name_and_base_url(self) -> None:
        provider = {"name": "oMLX", "base_url": "http://127.0.0.1:8001/v1"}
        self.assertEqual(server.detect_provider_kind(provider), "omlx")

    def test_omlx_requires_a_base_url(self) -> None:
        self.assertIsNone(server.detect_provider_kind({"name": "oMLX", "base_url": ""}))

    def test_returns_none_for_unknown_provider(self) -> None:
        provider = {"name": "Local Llama", "base_url": "http://127.0.0.1:8001/v1"}
        self.assertIsNone(server.detect_provider_kind(provider))

    def test_returns_none_for_empty_provider(self) -> None:
        self.assertIsNone(server.detect_provider_kind({"name": "", "base_url": ""}))


def _provider(provider_id: str, name: str, base_url: str, api_key=None) -> dict:
    return {
        "id": provider_id,
        "name": name,
        "type": "custom",
        "base_url": base_url,
        "wire_api": "completions",
        "auth_kind": "api_key",
        "account_id": None,
        "settings": {"apiKey": api_key} if api_key else {},
    }


def _model(provider_id: str, model_id: str, **overrides) -> dict:
    model = {
        "row_id": f"row-{model_id}",
        "provider_id": provider_id,
        "model_id": model_id,
        "wire_model": None,
        "display_name": model_id,
        "state_key": f"{provider_id}/{model_id}",
        "max_prompt_tokens": None,
        "max_output_tokens": None,
        "supported_reasoning_efforts": None,
        "supports_vision": None,
    }
    model.update(overrides)
    return model


class BuildPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = [
            _provider("or", "OpenRouter", "https://openrouter.ai/api/v1"),
            _provider("local", "oMLX", "http://127.0.0.1:8001/v1"),
        ]
        self.catalogs = {
            "openrouter": {
                "vendor/model": {
                    "model_id": "vendor/model",
                    "max_context_window_tokens": 1000000,
                    "max_output_tokens": 128000,
                    "supported_reasoning_efforts": ["high", "low"],
                    "default_reasoning_effort": "high",
                }
            }
        }

    def test_update_status_when_metadata_missing(self) -> None:
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("or", "vendor/model")],
            catalogs=self.catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "update")
        self.assertEqual(
            entry["changes"],
            ["max_prompt_tokens", "max_output_tokens", "supported_reasoning_efforts"],
        )
        self.assertEqual(entry["proposed"]["max_prompt_tokens"], 872000)
        self.assertEqual(plan["summary"]["update"], 1)

    def test_unchanged_when_metadata_already_matches(self) -> None:
        plan = server.build_plan(
            providers=self.providers,
            models=[
                _model(
                    "or",
                    "vendor/model",
                    max_prompt_tokens=872000,
                    max_output_tokens=128000,
                    supported_reasoning_efforts=["low", "high"],
                )
            ],
            catalogs=self.catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "unchanged")
        self.assertEqual(entry["changes"], [])
        self.assertEqual(plan["summary"]["unchanged"], 1)

    def test_unresolved_when_model_absent_from_catalog(self) -> None:
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("or", "vendor/unknown")],
            catalogs=self.catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "unresolved")
        self.assertIn("not present", entry["reason"])

    def test_unsupported_provider_when_no_registry_match(self) -> None:
        providers = [
            _provider("unknown", "Local Llama", "http://127.0.0.1:9099/v1"),
        ]
        plan = server.build_plan(
            providers=providers,
            models=[_model("unknown", "some-local-model")],
            catalogs=self.catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "unsupported_provider")
        self.assertIn("openrouter", entry["reason"])
        self.assertIn("omlx", entry["reason"])

    def test_omlx_model_is_planned_from_its_own_catalog(self) -> None:
        catalogs = {
            "local": {
                "flint-oQ4e": {
                    "model_id": "flint-oQ4e",
                    "max_context_window_tokens": 262144,
                    "max_output_tokens": 32768,
                    "supported_reasoning_efforts": None,
                    "default_reasoning_effort": None,
                }
            }
        }
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("local", "flint-oQ4e")],
            catalogs=catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "update")
        self.assertEqual(entry["changes"], ["max_prompt_tokens", "max_output_tokens"])
        self.assertEqual(entry["proposed"]["max_prompt_tokens"], 229376)
        self.assertEqual(entry["proposed"]["max_output_tokens"], 32768)
        self.assertIsNone(entry["proposed"]["supported_reasoning_efforts"])

    def test_omlx_model_missing_from_its_server_catalog_is_unresolved(self) -> None:
        catalogs = {
            "openrouter": self.catalogs["openrouter"],
            "local": {"other-model": {"model_id": "other-model"}},
        }
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("local", "flint-oQ4e")],
            catalogs=catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "unresolved")
        self.assertEqual(entry["provider_kind"], "omlx")

    def test_model_ids_filter_accepts_model_id_and_state_key(self) -> None:
        models = [_model("or", "vendor/model"), _model("or", "vendor/other")]
        by_model_id = server.build_plan(
            providers=self.providers,
            models=models,
            catalogs=self.catalogs,
            model_ids=["vendor/model"],
        )
        self.assertEqual([e["model_id"] for e in by_model_id["entries"]], ["vendor/model"])

        by_state_key = server.build_plan(
            providers=self.providers,
            models=models,
            catalogs=self.catalogs,
            model_ids=["or/vendor/other"],
        )
        self.assertEqual([e["model_id"] for e in by_state_key["entries"]], ["vendor/other"])

    def test_vision_flag_is_planned_only_from_definitive_evidence(self) -> None:
        catalogs = {
            "openrouter": {
                "vendor/vlm": {
                    "model_id": "vendor/vlm",
                    "max_context_window_tokens": 1000,
                    "supports_vision": True,
                    "vision_media_types": ["image/png"],
                    "vision_source": "openrouter.architecture",
                }
            }
        }
        plan = server.build_plan(
            providers=self.providers,
            models=[
                _model(
                    "or",
                    "vendor/vlm",
                    max_prompt_tokens=1000,
                    supports_vision=0,
                )
            ],
            catalogs=catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["changes"], ["supports_vision"])
        self.assertEqual(entry["current"]["supports_vision"], 0)
        self.assertEqual(entry["status"], "update")

        already = server.build_plan(
            providers=self.providers,
            models=[
                _model(
                    "or",
                    "vendor/vlm",
                    max_prompt_tokens=1000,
                    supports_vision=1,
                )
            ],
            catalogs=catalogs,
        )
        self.assertEqual(already["entries"][0]["changes"], [])
        self.assertEqual(already["entries"][0]["status"], "unchanged")

    def test_text_only_catalog_entry_never_plans_a_vision_write(self) -> None:
        catalogs = {
            "openrouter": {
                "vendor/model": {
                    "model_id": "vendor/model",
                    "max_context_window_tokens": 1000,
                    "supports_vision": False,
                    "vision_media_types": [],
                }
            }
        }
        plan = server.build_plan(
            providers=self.providers,
            models=[
                _model(
                    "or",
                    "vendor/model",
                    max_prompt_tokens=1000,
                    supports_vision=1,
                )
            ],
            catalogs=catalogs,
        )
        entry = plan["entries"][0]
        self.assertNotIn("supports_vision", entry["changes"])
        self.assertEqual(entry["current"]["supports_vision"], 1)
        self.assertIs(entry["proposed"]["supports_vision"], False)

    def test_set_reasoning_defaults_only_fills_missing_choices(self) -> None:
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("or", "vendor/model")],
            catalogs=self.catalogs,
            reasoning_state={"or/vendor/model": "low"},
            set_reasoning_defaults=True,
        )
        self.assertEqual(plan["entries"][0]["app_state_changes"], [])

        plan = server.build_plan(
            providers=self.providers,
            models=[_model("or", "vendor/model")],
            catalogs=self.catalogs,
            reasoning_state={},
            set_reasoning_defaults=True,
        )
        changes = plan["entries"][0]["app_state_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["key"], server.APP_STATE_REASONING_KEY)
        self.assertEqual(changes[0]["to"], "high")

    def test_set_context_tier_plans_change(self) -> None:
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("or", "vendor/model")],
            catalogs=self.catalogs,
            context_tier_state={},
            set_context_tier="long_context",
        )
        changes = plan["entries"][0]["app_state_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["key"], server.APP_STATE_CONTEXT_TIER_KEY)
        self.assertEqual(changes[0]["to"], "long_context")

    def test_set_context_tier_rejects_invalid_value(self) -> None:
        with self.assertRaises(ValueError):
            server.build_plan(
                providers=self.providers,
                models=[_model("or", "vendor/model")],
                catalogs=self.catalogs,
                set_context_tier="huge",
            )

    def test_providers_section_reports_detected_kind(self) -> None:
        plan = server.build_plan(
            providers=self.providers, models=[], catalogs=self.catalogs
        )
        kinds = {p["name"]: p["provider_kind"] for p in plan["providers"]}
        self.assertEqual(kinds["OpenRouter"], "openrouter")
        self.assertEqual(kinds["oMLX"], "omlx")
        by_name = {p["name"]: p for p in plan["providers"]}
        self.assertEqual(
            by_name["OpenRouter"]["catalog_url"],
            "https://openrouter.ai/api/v1/models",
        )
        self.assertEqual(
            by_name["oMLX"]["catalog_url"], "http://127.0.0.1:8001/v1/models"
        )


class UnsupportedModelTypePlanTest(unittest.TestCase):
    """Non-chat oMLX models are reported but never planned for a write."""

    #: The three configured models that cannot serve chat completions.
    UNSUPPORTED = (
        ("jina-reranker-v3.5-mlx", "reranker"),
        ("Ming-3.0-tts-tiny-4bit", "audio_tts"),
        ("MarkItDown", "markitdown"),
    )

    def setUp(self) -> None:
        self.provider = _provider("local", "oMLX", "http://127.0.0.1:8001/v1")

    def _catalog(self, include_chat: bool = True) -> dict:
        catalog = {
            model_id: server.normalize_omlx_model(
                {"id": model_id, "max_model_len": 8192},
                {
                    "id": model_id,
                    "model_type": model_type,
                    "engine_type": model_type,
                    "max_tokens": 2048,
                },
                {
                    "id": model_id,
                    "model_type": model_type,
                    "reasoning_effort_options": ["low", "medium", "high", "max"],
                    "reasoning_effort_default": "low",
                },
            )
            for model_id, model_type in self.UNSUPPORTED
        }
        if include_chat:
            catalog["flint-oQ4e"] = server.normalize_omlx_model(
                {"id": "flint-oQ4e", "max_model_len": 262144},
                {"id": "flint-oQ4e", "model_type": "vlm", "max_tokens": 32768},
            )
        return catalog

    def _plan(self, include_chat: bool = True, **kwargs):
        catalog = self._catalog(include_chat=include_chat)
        models = [_model(self.provider["id"], model_id) for model_id in catalog]
        return server.build_plan(
            providers=[self.provider],
            models=models,
            catalogs={self.provider["id"]: catalog},
            **kwargs,
        )

    def test_each_non_chat_type_reports_unsupported_with_its_type(self) -> None:
        plan = self._plan()
        by_id = {entry["model_id"]: entry for entry in plan["entries"]}
        for model_id, model_type in self.UNSUPPORTED:
            with self.subTest(model_id=model_id):
                entry = by_id[model_id]
                self.assertEqual(entry["status"], "unsupported_model_type")
                self.assertIn(model_type, entry["reason"])
                self.assertEqual(
                    entry["provider_metadata"]["unsupported_model_type"], model_type
                )
                self.assertEqual(entry["provider_metadata"]["model_type"], model_type)
                self.assertIsNone(entry["proposed"])
                self.assertEqual(entry["changes"], [])
                self.assertEqual(entry["app_state_changes"], [])

    def test_no_reasoning_or_vision_write_is_planned_for_them(self) -> None:
        """#3's gate and #2's vision path must both stay shut."""
        catalog = self._catalog()
        for model_id, _ in self.UNSUPPORTED:
            metadata = catalog[model_id]
            self.assertIsNone(metadata["supported_reasoning_efforts"])
            self.assertFalse(metadata["supports_reasoning"])
            self.assertIs(metadata["supports_vision"], False)
        plan = self._plan()
        for entry in plan["entries"]:
            if entry["status"] != "unsupported_model_type":
                continue
            self.assertEqual(entry["changes"], [])
            self.assertIsNone(entry["proposed"])

    def test_flag_lets_them_plan_normally_for_debugging(self) -> None:
        plan = self._plan(include_unsupported_model_types=True)
        by_id = {entry["model_id"]: entry for entry in plan["entries"]}
        for model_id, _ in self.UNSUPPORTED:
            with self.subTest(model_id=model_id):
                entry = by_id[model_id]
                self.assertEqual(entry["status"], "update")
                self.assertIn("max_prompt_tokens", entry["changes"])
                self.assertEqual(entry["proposed"]["max_prompt_tokens"], 6144)
                self.assertNotIn(
                    "supported_reasoning_efforts", entry["changes"]
                )
        self.assertEqual(plan["summary"]["unsupported_model_type"], 0)
        self.assertEqual(plan["summary"]["update"], 4)

    def test_summary_has_five_buckets_that_sum_to_total(self) -> None:
        plan = self._plan()
        summary = plan["summary"]
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["unsupported_model_type"], 3)
        self.assertEqual(summary["update"], 1)
        # ``update_row`` is a subset of ``update``, never a sixth bucket, so it
        # must not appear in the sum below.
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

    def test_existing_buckets_are_identical_on_a_fixture_without_them(self) -> None:
        """The four original counts must not shift when nothing is excluded."""
        plan = self._plan(include_chat=True)
        chat_only = {
            "total": 1,
            "update": 1,
            "unchanged": 0,
            "unresolved": 0,
            "unsupported_provider": 0,
            "unsupported_model_type": 0,
            # Not a bucket: a subset of ``update`` (see build_plan).
            "update_row": 1,
        }
        catalog = {"flint-oQ4e": self._catalog()["flint-oQ4e"]}
        chat_plan = server.build_plan(
            providers=[self.provider],
            models=[_model(self.provider["id"], "flint-oQ4e")],
            catalogs={self.provider["id"]: catalog},
        )
        self.assertEqual(chat_plan["summary"], chat_only)
        self.assertEqual(
            sum(
                chat_plan["summary"][key]
                for key in (
                    "update",
                    "unchanged",
                    "unresolved",
                    "unsupported_provider",
                )
            ),
            chat_plan["summary"]["total"],
        )
        # Same fixture plus the excluded models: the four original buckets keep
        # their numbers and total grows by exactly the new bucket.
        self.assertEqual(
            {
                key: plan["summary"][key]
                for key in (
                    "update",
                    "unchanged",
                    "unresolved",
                    "unsupported_provider",
                )
            },
            {
                key: chat_only[key]
                for key in (
                    "update",
                    "unchanged",
                    "unresolved",
                    "unsupported_provider",
                )
            },
        )
        self.assertEqual(plan["summary"]["total"], chat_only["total"] + 3)

    def test_openrouter_entries_are_always_chat(self) -> None:
        provider = _provider("or", "OpenRouter", "https://openrouter.ai/api/v1")
        catalog = {
            "vendor/model": server.normalize_openrouter_model(
                {"id": "vendor/model", "context_length": 1000}
            )
        }
        plan = server.build_plan(
            providers=[provider],
            models=[_model(provider["id"], "vendor/model")],
            catalogs={"openrouter": catalog},
        )
        self.assertEqual(plan["entries"][0]["status"], "update")
        self.assertEqual(plan["summary"]["unsupported_model_type"], 0)

    def test_predicate_is_the_single_gate(self) -> None:
        for model_type in server.OMLX_UNSUPPORTED_MODEL_TYPES:
            with self.subTest(model_type=model_type):
                self.assertTrue(
                    server.is_unsupported_omlx_model_type(model_type),
                    "is_unsupported_omlx_model_type must cover every excluded type",
                )
                self.assertFalse(
                    server.is_omlx_reasoning_model_type(model_type),
                    "reasoning trust must reuse the same exclusion predicate",
                )
        for model_type in ("vlm", "llm", "text"):
            self.assertFalse(server.is_unsupported_omlx_model_type(model_type))
            self.assertTrue(server.is_omlx_reasoning_model_type(model_type))
        self.assertFalse(server.is_unsupported_omlx_model_type(None))
        self.assertTrue(server.is_unsupported_omlx_model_type(None, " ReRanker "))


class CollectCatalogsTest(unittest.TestCase):
    def test_unreachable_local_server_warns_instead_of_raising(self) -> None:
        providers = [
            _provider("or", "OpenRouter", "https://openrouter.ai/api/v1"),
            _provider("local", "oMLX", "http://127.0.0.1:9/v1"),
        ]
        original = server.fetch_catalog

        def fake_fetch(kind, catalog_url=None, provider=None):
            if kind == "omlx":
                raise OSError("connection refused")
            return {"vendor/model": {"model_id": "vendor/model"}}

        server.fetch_catalog = fake_fetch
        try:
            catalogs, warnings = server.collect_catalogs(
                providers, ["openrouter", "omlx"]
            )
        finally:
            server.fetch_catalog = original

        self.assertIn("openrouter", catalogs)
        self.assertEqual(catalogs["local"], {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("oMLX", warnings[0])
        self.assertIn("http://127.0.0.1:9/v1", warnings[0])

    def test_local_catalogs_are_keyed_per_provider(self) -> None:
        providers = [
            _provider("a", "oMLX", "http://127.0.0.1:8001/v1"),
            _provider("b", "oMLX", "http://127.0.0.1:8002/v1"),
        ]
        original = server.fetch_catalog
        seen: list[str] = []

        def fake_fetch(kind, catalog_url=None, provider=None):
            seen.append(provider["base_url"])
            return {"m": {"model_id": "m"}}

        server.fetch_catalog = fake_fetch
        try:
            catalogs, warnings = server.collect_catalogs(providers, ["omlx"])
        finally:
            server.fetch_catalog = original

        self.assertEqual(sorted(seen), ["http://127.0.0.1:8001/v1", "http://127.0.0.1:8002/v1"])
        self.assertEqual(set(catalogs), {"a", "b"})
        self.assertEqual(warnings, [])


class NormalizeOmlxModelTest(unittest.TestCase):
    def test_status_wins_over_listing_for_effective_context(self) -> None:
        entry = {"id": "Qwen3.8-27B-4bit", "max_model_len": 262144}
        status = {
            "id": "Qwen3.8-27B-4bit",
            "model_context_length": 262144,
            "max_context_window": 128000,
            "max_tokens": 32768,
            "model_type": "llm",
            "loaded": True,
        }
        result = server.normalize_omlx_model(entry, status)
        self.assertEqual(result["max_context_window_tokens"], 128000)
        self.assertEqual(result["max_output_tokens"], 32768)
        self.assertEqual(result["context_source"], "status.max_context_window")
        self.assertEqual(result["native_context_window_tokens"], 262144)
        self.assertTrue(result["loaded"])
        self.assertEqual(result["model_type"], "llm")

    def test_falls_back_to_listing_max_model_len(self) -> None:
        result = server.normalize_omlx_model({"id": "flint-oQ4e", "max_model_len": 262144})
        self.assertEqual(result["max_context_window_tokens"], 262144)
        self.assertEqual(result["context_source"], "models.max_model_len")
        self.assertIsNone(result["max_output_tokens"])

    def test_reports_unresolved_when_no_context_is_advertised(self) -> None:
        result = server.normalize_omlx_model({"id": "MarkItDown"})
        self.assertIsNone(result["max_context_window_tokens"])
        self.assertEqual(result["context_source"], "unresolved")

    def test_vlm_type_flags_vision(self) -> None:
        for field in ("model_type", "engine_type"):
            with self.subTest(field=field):
                result = server.normalize_omlx_model(
                    {"id": "flint-oQ4e"}, {"id": "flint-oQ4e", field: "vlm"}
                )
                self.assertTrue(result["supports_vision"])
                self.assertEqual(result["vision_source"], "omlx.model_type")
                self.assertIn("image/png", result["vision_media_types"])

    def test_non_vision_types_are_not_flagged(self) -> None:
        for model_type in ("llm", "reranker", "audio_tts", "markitdown"):
            with self.subTest(model_type=model_type):
                result = server.normalize_omlx_model(
                    {"id": "m"}, {"id": "m", "model_type": model_type}
                )
                self.assertIs(result["supports_vision"], False)
                self.assertEqual(result["vision_media_types"], [])

    def test_missing_type_leaves_vision_unknown(self) -> None:
        result = server.normalize_omlx_model({"id": "m"})
        self.assertIsNone(result["supports_vision"])
        self.assertIsNone(result["vision_source"])

    def test_never_derives_reasoning_efforts_without_admin_data(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "Qwen3.8-27B-4bit", "max_model_len": 262144},
            {"reasoning_efforts": ["low", "high"], "max_tokens": 4096},
        )
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertIsNone(result["default_reasoning_effort"])
        self.assertFalse(result["supports_reasoning"])

    def test_supported_for_chat_and_reported_type(self) -> None:
        for model_type in ("reranker", "audio_tts", "markitdown"):
            with self.subTest(model_type=model_type):
                result = server.normalize_omlx_model(
                    {"id": "m"}, {"id": "m", "model_type": model_type}
                )
                self.assertIs(result["supported_for_chat"], False)
                self.assertEqual(result["unsupported_model_type"], model_type)
                self.assertEqual(result["model_type"], model_type)
        for model_type in ("vlm", "llm", "text"):
            with self.subTest(model_type=model_type):
                result = server.normalize_omlx_model(
                    {"id": "m"}, {"id": "m", "model_type": model_type}
                )
                self.assertIs(result["supported_for_chat"], True)
                self.assertIsNone(result["unsupported_model_type"])

    def test_engine_type_is_the_fallback_when_model_type_is_absent(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "m"}, {"id": "m", "engine_type": "audio_tts"}
        )
        self.assertIs(result["supported_for_chat"], False)
        self.assertEqual(result["unsupported_model_type"], "audio_tts")
        self.assertEqual(result["model_type"], "audio_tts")

    def test_non_chat_type_short_circuits_vision_even_when_vlm_is_also_claimed(
        self,
    ) -> None:
        result = server.normalize_omlx_model(
            {"id": "m", "model_type": "vlm"},
            {"id": "m", "model_type": "reranker"},
        )
        self.assertIs(result["supports_vision"], False)
        self.assertIsNone(result["vision_source"])

    def test_unknown_type_is_neither_excluded_nor_trusted(self) -> None:
        result = server.normalize_omlx_model({"id": "m"})
        self.assertIs(result["supported_for_chat"], True)
        self.assertIsNone(result["unsupported_model_type"])
        self.assertIsNone(result["model_type"])

    def test_admin_options_are_intersected_with_copilot_vocabulary(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "flint-oQ4e"},
            {"id": "flint-oQ4e", "model_type": "vlm"},
            {
                "id": "flint-oQ4e",
                "model_type": "vlm",
                "reasoning_effort_options": ["minimal", "low", "medium", "high"],
                "reasoning_effort_default": "low",
            },
        )
        self.assertEqual(
            result["supported_reasoning_efforts"], ["low", "medium", "high"]
        )
        self.assertEqual(result["default_reasoning_effort"], "low")
        self.assertTrue(result["supports_reasoning"])

    def test_admin_options_default_is_dropped_when_not_offered(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "m"},
            {"model_type": "llm"},
            {
                "reasoning_effort_options": ["low", "high"],
                "reasoning_effort_default": "max",
            },
        )
        self.assertEqual(result["supported_reasoning_efforts"], ["low", "high"])
        self.assertIsNone(result["default_reasoning_effort"])

    def test_status_levels_alone_are_reported_but_write_no_efforts(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "m"},
            {
                "model_type": "vlm",
                "reasoning_levels": ["off", "on"],
                "thinking_default": True,
            },
        )
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertIsNone(result["default_reasoning_effort"])
        self.assertFalse(result["supports_reasoning"])
        self.assertEqual(result["reasoning_levels"], ["off", "on"])
        self.assertTrue(result["thinking_default"])

    def test_admin_options_ignored_for_untrusted_model_types(self) -> None:
        admin_entry = {
            "reasoning_effort_options": ["low", "medium", "high", "xhigh", "max"],
            "reasoning_effort_default": "low",
        }
        for model_type in ("reranker", "audio_tts", "markitdown"):
            with self.subTest(model_type=model_type):
                result = server.normalize_omlx_model(
                    {"id": "m"},
                    {"model_type": model_type},
                    {**admin_entry, "model_type": model_type},
                )
                self.assertIsNone(result["supported_reasoning_efforts"])
                self.assertIsNone(result["default_reasoning_effort"])
                self.assertFalse(result["supports_reasoning"])

    def test_admin_options_ignored_when_type_is_absent(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "m"},
            None,
            {"reasoning_effort_options": ["low", "high"]},
        )
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertFalse(result["supports_reasoning"])

    def test_admin_type_is_used_when_status_omits_it(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "m"},
            {"max_tokens": 4096},
            {"model_type": "text", "reasoning_effort_options": ["low", "max"]},
        )
        self.assertEqual(result["supported_reasoning_efforts"], ["low", "max"])


class FetchOmlxAdminModelsTest(unittest.TestCase):
    def test_indexes_models_and_sends_the_key_on_the_admin_root(self) -> None:
        provider = _provider(
            "local", "oMLX", "http://127.0.0.1:8001/v1", api_key="main-key"
        )
        original = server.http_get_json
        seen: list[tuple[str, dict]] = []

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            seen.append((url, headers or {}))
            return {
                "models": [
                    {
                        "id": "flint-oQ4e",
                        "model_type": "vlm",
                        "reasoning_effort_options": ["low", "high"],
                    }
                ]
            }

        server.http_get_json = fake_get
        try:
            admin = server.fetch_omlx_admin_models("http://127.0.0.1:8001/v1", "main-key")
        finally:
            server.http_get_json = original

        self.assertEqual(admin["flint-oQ4e"]["model_type"], "vlm")
        self.assertEqual(seen[0][0], "http://127.0.0.1:8001/admin/api/models")
        self.assertEqual(seen[0][1].get("Authorization"), "Bearer main-key")

    def test_failures_are_silent_and_yield_empty_mapping(self) -> None:
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        server.http_get_json = fake_get
        try:
            self.assertEqual(
                server.fetch_omlx_admin_models("http://127.0.0.1:8001", "sub-key"), {}
            )
        finally:
            server.http_get_json = original

    def test_missing_base_url_does_not_issue_a_request(self) -> None:
        original = server.http_get_json
        calls: list[str] = []

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            calls.append(url)
            return {"models": []}

        server.http_get_json = fake_get
        try:
            self.assertEqual(server.fetch_omlx_admin_models("", "key"), {})
        finally:
            server.http_get_json = original
        self.assertEqual(calls, [])


class OmlxAdminCatalogTest(unittest.TestCase):
    def test_admin_options_reach_the_catalog_for_a_vlm(self) -> None:
        provider = _provider(
            "local", "oMLX", "http://127.0.0.1:8001/v1", api_key="main-key"
        )
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                return {
                    "models": [
                        {
                            "id": "flint-oQ4e",
                            "model_type": "vlm",
                            "reasoning_effort_options": [
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                            ],
                            "reasoning_effort_default": "low",
                            "reasoning_levels": ["off", "on"],
                        }
                    ]
                }
            if url.endswith("/models/status"):
                return {"models": [{"id": "flint-oQ4e", "model_type": "vlm"}]}
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        entry = catalog["flint-oQ4e"]
        self.assertEqual(
            entry["supported_reasoning_efforts"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(entry["default_reasoning_effort"], "low")
        self.assertTrue(entry["supports_reasoning"])
        self.assertEqual(entry["reasoning_levels"], ["off", "on"])

    def test_admin_401_falls_back_to_status_levels(self) -> None:
        provider = _provider(
            "local", "oMLX", "http://127.0.0.1:8001/v1", api_key="sub-key"
        )
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            if url.endswith("/models/status"):
                return {
                    "models": [
                        {
                            "id": "flint-oQ4e",
                            "model_type": "vlm",
                            "reasoning_levels": ["off", "on"],
                        }
                    ]
                }
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        entry = catalog["flint-oQ4e"]
        self.assertIsNone(entry["supported_reasoning_efforts"])
        self.assertFalse(entry["supports_reasoning"])
        self.assertEqual(entry["reasoning_levels"], ["off", "on"])

    def test_admin_404_leaves_no_reasoning_metadata(self) -> None:
        provider = _provider("local", "oMLX", "http://127.0.0.1:8001/v1")
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return {"data": [{"id": "m", "max_model_len": 65536}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        entry = catalog["m"]
        self.assertIsNone(entry["supported_reasoning_efforts"])
        self.assertFalse(entry["supports_reasoning"])
        self.assertIsNone(entry["reasoning_levels"])
        self.assertIsNone(entry["thinking_default"])

    def test_reranker_is_excluded_by_the_trust_gate(self) -> None:
        provider = _provider(
            "local", "oMLX", "http://127.0.0.1:8001/v1", api_key="main-key"
        )
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                return {
                    "models": [
                        {
                            "id": "jina-reranker-v3.5-mlx",
                            "model_type": "reranker",
                            "engine_type": "reranker",
                            "reasoning_effort_options": [
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                            ],
                            "reasoning_effort_default": "low",
                        }
                    ]
                }
            if url.endswith("/models/status"):
                return {
                    "models": [
                        {
                            "id": "jina-reranker-v3.5-mlx",
                            "model_type": "reranker",
                            "reasoning_levels": ["off", "on"],
                        }
                    ]
                }
            return {"data": [{"id": "jina-reranker-v3.5-mlx", "max_model_len": 8192}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        entry = catalog["jina-reranker-v3.5-mlx"]
        self.assertIsNone(entry["supported_reasoning_efforts"])
        self.assertIsNone(entry["default_reasoning_effort"])
        self.assertFalse(entry["supports_reasoning"])
        self.assertIsNone(entry["reasoning_levels"])

    def test_plan_reports_reasoning_levels_in_provider_metadata(self) -> None:
        provider = _provider("local", "oMLX", "http://127.0.0.1:8001/v1")
        metadata = server.normalize_omlx_model(
            {"id": "m"},
            {"model_type": "vlm", "reasoning_levels": ["off", "on"]},
        )
        plan = server.build_plan(
            providers=[provider],
            models=[
                {
                    "model_id": "m",
                    "provider_id": provider["id"],
                    "row_id": "row",
                    "state_key": f"{provider['id']}/row",
                    "max_prompt_tokens": 1000,
                    "max_output_tokens": 1000,
                    "supported_reasoning_efforts": None,
                    "supports_vision": None,
                }
            ],
            catalogs={provider["id"]: {"m": metadata}},
        )
        entry = plan["entries"][0]
        self.assertIsNone(entry["proposed"]["supported_reasoning_efforts"])
        self.assertEqual(
            entry["provider_metadata"]["reasoning_levels"], ["off", "on"]
        )
        self.assertNotIn("supported_reasoning_efforts", entry["changes"])

    def test_existing_reasoning_selection_is_never_overwritten(self) -> None:
        provider = _provider("local", "oMLX", "http://127.0.0.1:8001/v1")
        metadata = server.normalize_omlx_model(
            {"id": "m"},
            {"model_type": "llm"},
            {
                "model_type": "llm",
                "reasoning_effort_options": ["low", "medium", "high"],
                "reasoning_effort_default": "low",
            },
        )
        model = _model(
            provider["id"],
            "m",
            max_prompt_tokens=1000,
            max_output_tokens=1000,
            supported_reasoning_efforts=["low", "medium", "high"],
        )
        plan = server.build_plan(
            providers=[provider],
            models=[model],
            catalogs={provider["id"]: {"m": metadata}},
            reasoning_state={model["state_key"]: "high"},
            set_reasoning_defaults=True,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["app_state_changes"], [])
        self.assertEqual(entry["status"], "unchanged")


class ProviderUrlTest(unittest.TestCase):
    def test_api_root_adds_v1_once(self) -> None:
        self.assertEqual(
            server.provider_api_root("http://127.0.0.1:8001"), "http://127.0.0.1:8001/v1"
        )
        self.assertEqual(
            server.provider_api_root("http://127.0.0.1:8001/v1/"),
            "http://127.0.0.1:8001/v1",
        )

    def test_omlx_catalog_url_comes_from_the_provider_base_url(self) -> None:
        provider = {"name": "oMLX", "base_url": "http://127.0.0.1:8001/v1"}
        self.assertEqual(
            server.provider_catalog_url("omlx", provider=provider),
            "http://127.0.0.1:8001/v1/models",
        )

    def test_omlx_catalog_url_is_none_without_a_provider(self) -> None:
        self.assertIsNone(server.provider_catalog_url("omlx"))

    def test_public_provider_uses_the_registry_url(self) -> None:
        self.assertEqual(
            server.provider_catalog_url("openrouter"),
            "https://openrouter.ai/api/v1/models",
        )


class FetchOmlxCatalogTest(unittest.TestCase):
    def test_merges_status_into_the_listing(self) -> None:
        provider = _provider("local", "oMLX", "http://127.0.0.1:8001/v1")
        original = server.http_get_json
        requested: list[str] = []

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            requested.append(url)
            if url.endswith("/models/status"):
                return {
                    "models": [
                        {
                            "id": "flint-oQ4e",
                            "model_context_length": 262144,
                            "max_context_window": 131072,
                            "max_tokens": 32768,
                            "model_type": "vlm",
                        }
                    ]
                }
            return {"object": "list", "data": [{"id": "flint-oQ4e", "max_model_len": 262144}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        self.assertEqual(
            requested,
            [
                "http://127.0.0.1:8001/v1/models",
                "http://127.0.0.1:8001/v1/models/status",
                "http://127.0.0.1:8001/admin/api/models",
            ],
        )
        self.assertEqual(catalog["flint-oQ4e"]["max_context_window_tokens"], 131072)
        self.assertEqual(catalog["flint-oQ4e"]["max_output_tokens"], 32768)

    def test_status_failure_falls_back_to_the_listing(self) -> None:
        provider = _provider("local", "oMLX", "http://127.0.0.1:8001")
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/models/status"):
                raise OSError("no status endpoint")
            return {"data": [{"id": "m", "max_model_len": 65536}]}

        server.http_get_json = fake_get
        try:
            catalog = server.fetch_catalog("omlx", provider=provider)
        finally:
            server.http_get_json = original

        self.assertEqual(catalog["m"]["max_context_window_tokens"], 65536)
        self.assertEqual(catalog["m"]["context_source"], "models.max_model_len")


class ParseEffortsTest(unittest.TestCase):
    def test_parses_json_array(self) -> None:
        self.assertEqual(server.parse_efforts('["low","high"]'), ["low", "high"])

    def test_none_stays_none(self) -> None:
        self.assertIsNone(server.parse_efforts(None))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(server.parse_efforts("not json"))

    def test_non_list_returns_none(self) -> None:
        self.assertIsNone(server.parse_efforts('{"low": true}'))


#: Minimal schema matching what the server reads. Deliberately seeded with a
#: real ``settings_json.apiKey``: the fabricated ``_provider(api_key=...)``
#: dicts used by the pure-function tests cannot be produced by any real code
#: path, so they can never prove the key reaches the wire.
TEMP_DB_SCHEMA = """
CREATE TABLE model_providers (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
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


class _TempDbTestCase(unittest.TestCase):
    """Base class that seeds a real oMLX provider row into a temp database."""

    API_KEY = "REALKEY"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "data.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(TEMP_DB_SCHEMA)
        conn.execute(
            "INSERT INTO model_providers (id, name, type, settings_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "local",
                "oMLX",
                "ollama",
                json.dumps(
                    {
                        "baseUrl": "http://127.0.0.1:8001/v1",
                        "authKind": "api_key",
                        "apiKey": self.API_KEY,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO provider_models (id, provider_id, model_id, display_name) "
            "VALUES (?, ?, ?, ?)",
            ("row-flint", "local", "flint-oQ4e", "flint-oQ4e"),
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _admin_payload(self) -> dict:
        return {
            "models": [
                {
                    "id": "flint-oQ4e",
                    "model_type": "vlm",
                    "reasoning_effort_options": [
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max",
                    ],
                    "reasoning_effort_default": "low",
                }
            ]
        }


class ProviderKeyOnTheWireTest(_TempDbTestCase):
    """The provider key must reach the admin request from a real DB row."""

    def _fake_get(self, seen: list[tuple[str, dict]]):
        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            seen.append((url, dict(headers or {})))
            if url.endswith("/admin/api/models"):
                return self._admin_payload()
            if url.endswith("/models/status"):
                return {"models": [{"id": "flint-oQ4e", "model_type": "vlm"}]}
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        return fake_get

    def _assert_admin_key_was_sent(self, seen: list[tuple[str, dict]]) -> None:
        admin_headers = [
            headers for url, headers in seen if url.endswith("/admin/api/models")
        ]
        self.assertEqual(len(admin_headers), 1, seen)
        # Compare against the seeded key, never a redacted placeholder: this
        # must fail when the provider settings reach the request already masked.
        self.assertEqual(
            admin_headers[0].get("Authorization"), "Bearer " + self.API_KEY
        )

    def test_fetch_provider_metadata_sends_the_key_end_to_end(self) -> None:
        """The public read path must authenticate the admin fetch itself.

        Calling ``load_providers(conn, redact=False)`` directly proves only
        that the loader *can* expose the key. This goes through
        ``fetch_provider_metadata`` so reverting its internal ``redact=False``
        makes the test fail.
        """
        seen: list[tuple[str, dict]] = []
        original = server.http_get_json
        server.http_get_json = self._fake_get(seen)
        try:
            result = server.fetch_provider_metadata(
                provider="omlx", db_path=self.db_path
            )
        finally:
            server.http_get_json = original

        self._assert_admin_key_was_sent(seen)
        resolved = {entry["model_id"]: entry for entry in result["resolved"]}
        metadata = resolved["flint-oQ4e"]["metadata"]
        self.assertEqual(
            metadata["supported_reasoning_efforts"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(metadata["default_reasoning_effort"], "low")
        # The key itself must never appear in the tool result.
        self.assertNotIn("REALKEY", json.dumps(result))

    def test_plan_metadata_updates_sends_the_key_end_to_end(self) -> None:
        seen: list[tuple[str, dict]] = []
        original = server.http_get_json
        server.http_get_json = self._fake_get(seen)
        try:
            server.plan_metadata_updates(db_path=self.db_path)
        finally:
            server.http_get_json = original

        self._assert_admin_key_was_sent(seen)

    def test_apply_metadata_updates_sends_the_key_end_to_end(self) -> None:
        seen: list[tuple[str, dict]] = []
        original = server.http_get_json
        server.http_get_json = self._fake_get(seen)
        try:
            server.apply_metadata_updates(db_path=self.db_path, backup=False)
        finally:
            server.http_get_json = original

        self._assert_admin_key_was_sent(seen)

    def test_real_db_key_is_sent_as_a_bearer_header(self) -> None:
        conn = server.connect(self.db_path, readonly=True)
        try:
            providers = server.load_providers(conn, redact=False)
        finally:
            conn.close()
        self.assertEqual(
            providers[0]["settings"]["apiKey"], "REALKEY",
            "the internal loader must expose the usable key",
        )

        seen: list[tuple[str, dict]] = []
        original = server.http_get_json

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            seen.append((url, dict(headers or {})))
            if url.endswith("/admin/api/models"):
                return self._admin_payload()
            if url.endswith("/models/status"):
                return {"models": [{"id": "flint-oQ4e", "model_type": "vlm"}]}
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        server.http_get_json = fake_get
        try:
            catalog = server.collect_catalogs(providers, ["omlx"])[0]["local"]
        finally:
            server.http_get_json = original

        admin_headers = [
            headers
            for url, headers in seen
            if url.endswith("/admin/api/models")
        ]
        self.assertEqual(len(admin_headers), 1)
        self.assertEqual(
            admin_headers[0].get("Authorization"), "Bearer REALKEY"
        )
        self.assertEqual(
            catalog["flint-oQ4e"]["supported_reasoning_efforts"],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(catalog["flint-oQ4e"]["default_reasoning_effort"], "low")

    def test_default_loader_redacts_the_key(self) -> None:
        conn = server.connect(self.db_path, readonly=True)
        try:
            providers = server.load_providers(conn)
        finally:
            conn.close()
        self.assertEqual(providers[0]["settings"]["apiKey"], "<redacted>")

        original = server.http_get_json
        seen: list[dict] = []

        def fake_get(url, timeout=server.HTTP_TIMEOUT_SECONDS, headers=None):
            if url.endswith("/admin/api/models"):
                seen.append(dict(headers or {}))
            if url.endswith("/models/status"):
                return {"models": [{"id": "flint-oQ4e", "model_type": "vlm"}]}
            return {"data": [{"id": "flint-oQ4e", "max_model_len": 131072}]}

        server.http_get_json = fake_get
        try:
            server.collect_catalogs(providers, ["omlx"])
        finally:
            server.http_get_json = original

        # A redacted value must never be sent: it would be a fake credential.
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0].get("Authorization"))
        self.assertNotIn("<redacted>", json.dumps(seen))


if __name__ == "__main__":
    unittest.main()
