#!/usr/bin/env python3
"""Unit tests for the pure metadata-mapping functions in the server."""

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import server  # noqa: E402


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


def _provider(provider_id: str, name: str, base_url: str) -> dict:
    return {
        "id": provider_id,
        "name": name,
        "type": "custom",
        "base_url": base_url,
        "wire_api": "completions",
        "auth_kind": "api_key",
        "account_id": None,
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

    def test_never_derives_reasoning_efforts(self) -> None:
        result = server.normalize_omlx_model(
            {"id": "Qwen3.8-27B-4bit", "max_model_len": 262144},
            {"reasoning_efforts": ["low", "high"], "max_tokens": 4096},
        )
        self.assertIsNone(result["supported_reasoning_efforts"])
        self.assertIsNone(result["default_reasoning_effort"])
        self.assertFalse(result["supports_reasoning"])


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
            ["http://127.0.0.1:8001/v1/models", "http://127.0.0.1:8001/v1/models/status"],
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


if __name__ == "__main__":
    unittest.main()
