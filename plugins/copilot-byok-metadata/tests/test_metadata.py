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

    def test_returns_none_for_unknown_provider(self) -> None:
        provider = {"name": "oMLX", "base_url": "http://127.0.0.1:8001/v1"}
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
        plan = server.build_plan(
            providers=self.providers,
            models=[_model("local", "some-local-model")],
            catalogs=self.catalogs,
        )
        entry = plan["entries"][0]
        self.assertEqual(entry["status"], "unsupported_provider")
        self.assertIn("openrouter", entry["reason"])

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
        self.assertIsNone(kinds["oMLX"])


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
