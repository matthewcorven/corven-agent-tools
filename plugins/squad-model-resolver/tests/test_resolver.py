#!/usr/bin/env python3
"""Unit tests for squad-model-resolver."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add plugin root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (
    get_effective_registry,
    load_user_registry,
    parse_reference,
    read_agent_charter_preferred_model,
    read_squad_config_models,
    resolve_for_agent,
    resolve_model,
    verify_spawn,
)


class SquadModelResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = [
            "claude-sonnet-5.5",
            "claude-haiku-5.5",
            "gpt-5.6-terra",
            "gpt-6-luna",
            "b311631e-d285-40e6-b0e3-2e813af887ed/z-ai/glm-5.3",
            "b311631e-d285-40e6-b0e3-2e813af887ed/deepseek/deepseek-v4.1-flash-1017",
            "6fecf2bb-2bbb-4e4e-bec9-a2ca2c4a9d44/Qwen3.8-27B-4bit",
        ]
        self.registry = {
            "openrouter": "b311631e-d285-40e6-b0e3-2e813af887ed",
            "omlx": "6fecf2bb-2bbb-4e4e-bec9-a2ca2c4a9d44",
        }

    def test_resolved_case_provider_qualified(self) -> None:
        """Provider-qualified reference matching catalog and registry resolves."""
        res = resolve_model(
            reference="openrouter://z-ai/glm-5.3",
            catalog=self.catalog,
            provider_registry=self.registry,
        )
        self.assertEqual(res["status"], "resolved")
        self.assertEqual(res["model_id"], "b311631e-d285-40e6-b0e3-2e813af887ed/z-ai/glm-5.3")
        self.assertEqual(res["provider"], "openrouter")
        self.assertEqual(res["path"], "z-ai/glm-5.3")
        self.assertEqual(res["candidates"], ["b311631e-d285-40e6-b0e3-2e813af887ed/z-ai/glm-5.3"])

    def test_resolved_case_builtin(self) -> None:
        """Built-in unqualified model in catalog resolves directly."""
        res = resolve_model(
            reference="claude-sonnet-5.5",
            catalog=self.catalog,
        )
        self.assertEqual(res["status"], "resolved")
        self.assertEqual(res["model_id"], "claude-sonnet-5.5")
        self.assertIsNone(res["provider"])
        self.assertEqual(res["path"], "claude-sonnet-5.5")

    def test_ambiguous_case_two_exact_path_matches(self) -> None:
        """Two exact-path matches produce status 'ambiguous' and no model_id."""
        ambiguous_catalog = list(self.catalog) + [
            "c411631e-d285-40e6-b0e3-2e813af887ee/z-ai/glm-5.3"
        ]
        res = resolve_model(
            reference="openrouter://z-ai/glm-5.3",
            catalog=ambiguous_catalog,
            provider_registry=self.registry,
        )
        self.assertEqual(res["status"], "ambiguous")
        self.assertIsNone(res["model_id"])
        self.assertEqual(len(res["candidates"]), 2)
        self.assertIn("b311631e-d285-40e6-b0e3-2e813af887ed/z-ai/glm-5.3", res["candidates"])
        self.assertIn("c411631e-d285-40e6-b0e3-2e813af887ee/z-ai/glm-5.3", res["candidates"])

    def test_unresolvable_case(self) -> None:
        """Nonexistent model in catalog is unresolvable."""
        res = resolve_model(
            reference="openrouter://nonexistent/model",
            catalog=self.catalog,
            provider_registry=self.registry,
        )
        self.assertEqual(res["status"], "unresolvable")
        self.assertIsNone(res["model_id"])
        self.assertEqual(res["candidates"], [])

    def test_suffix_match_with_no_registry_metadata_must_not_resolve(self) -> None:
        """Suffix matches in catalog, but provider lacks registry metadata; must NOT resolve."""
        res = resolve_model(
            reference="unknown-provider://z-ai/glm-5.3",
            catalog=self.catalog,
            provider_registry=self.registry,  # does not contain unknown-provider
        )
        self.assertEqual(res["status"], "unresolvable")
        self.assertIsNone(res["model_id"])
        self.assertEqual(res["candidates"], [])
        self.assertIn("no registry metadata", res["reason"])

    def test_uuid_prefix_trap(self) -> None:
        """Never infer a provider from a UUID or guess/fuzzy match paths."""
        # 1. Path differs: deepseek-v4.1-1017 vs deepseek-v4.1-flash-1017
        res = resolve_model(
            reference="openrouter://deepseek/deepseek-v4.1-1017",
            catalog=self.catalog,
            provider_registry=self.registry,
        )
        self.assertEqual(res["status"], "unresolvable")
        self.assertIsNone(res["model_id"])

        # 2. Never construct or guess prefixes without metadata
        res2 = resolve_model(
            reference="omlx://random-prefix/Qwen3.8-27B-4bit",
            catalog=self.catalog,
            provider_registry=self.registry,
        )
        self.assertEqual(res2["status"], "unresolvable")
        self.assertIsNone(res2["model_id"])

    def test_qualified_references_never_fall_back(self) -> None:
        """Provider-qualified references never fall back, even when fallbacks or defaults exist."""
        res = resolve_model(
            reference="openrouter://missing/model",
            catalog=self.catalog,
            provider_registry=self.registry,
            fallback_chain=["claude-sonnet-5.5", "gpt-5.6-terra"],
        )
        self.assertEqual(res["status"], "unresolvable")
        self.assertIsNone(res["model_id"])
        self.assertIn("never fall back", res["reason"])

    def test_builtin_fallback_chain(self) -> None:
        """Built-in references fall back up to max retries then platform default."""
        catalog = ["gpt-5.6-terra"]
        res = resolve_model(
            reference="claude-sonnet-5.5",
            catalog=catalog,
            fallback_chain=["gpt-5.6-sol", "gpt-5.6-terra"],
            max_fallbacks=2,
        )
        self.assertEqual(res["status"], "resolved")
        self.assertEqual(res["model_id"], "gpt-5.6-terra")

        # Exhausted retries -> platform default (model_id is None)
        empty_catalog: list[str] = []
        res2 = resolve_model(
            reference="claude-sonnet-5.5",
            catalog=empty_catalog,
            fallback_chain=["gpt-5.6-terra", "claude-sonnet-5"],
            max_fallbacks=2,
        )
        self.assertEqual(res2["status"], "resolved")
        self.assertIsNone(res2["model_id"])
        self.assertIn("platform default", res2["reason"])

    def test_layer_precedence(self) -> None:
        """Strict layer precedence: config override -> session directive -> charter -> config default -> auto."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            squad_dir = repo_path / ".squad"
            squad_dir.mkdir(parents=True)
            agents_dir = squad_dir / "agents" / "lead"
            agents_dir.mkdir(parents=True)

            # Charter
            charter_text = """# Lead Charter
## Model
**Preferred:** `claude-haiku-5.5`
"""
            (agents_dir / "charter.md").write_text(charter_text, encoding="utf-8")

            # Config
            config_data = {
                "defaultModel": "gpt-6-luna",
                "agentModelOverrides": {
                    "lead": "gpt-5.6-terra"
                }
            }
            (squad_dir / "config.json").write_text(json.dumps(config_data), encoding="utf-8")

            # 1. Config override wins over everything
            r1 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="lead",
                catalog=self.catalog,
                session_directive="claude-sonnet-5.5",
            )
            self.assertEqual(r1["layer_won"], "config override")
            self.assertEqual(r1["model_id"], "gpt-5.6-terra")

            # 2. Session directive wins over charter and defaultModel when no agent override
            r2 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="tester",  # no override for tester
                catalog=self.catalog,
                session_directive="claude-sonnet-5.5",
            )
            self.assertEqual(r2["layer_won"], "session directive")
            self.assertEqual(r2["model_id"], "claude-sonnet-5.5")

            # 3. Charter wins over defaultModel when no session directive and no agent override
            r3 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="lead",
                catalog=self.catalog,
                session_directive=None,
            )
            # wait: lead has agentModelOverride in config, so remove it first
            config_data["agentModelOverrides"] = {}
            (squad_dir / "config.json").write_text(json.dumps(config_data), encoding="utf-8")

            r3 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="lead",
                catalog=self.catalog,
                session_directive=None,
            )
            self.assertEqual(r3["layer_won"], "charter")
            self.assertEqual(r3["model_id"], "claude-haiku-5.5")

            # 4. DefaultModel wins when no charter preference
            r4 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="tester",  # no charter
                catalog=self.catalog,
                session_directive=None,
            )
            self.assertEqual(r4["layer_won"], "config default")
            self.assertEqual(r4["model_id"], "gpt-6-luna")

            # 5. Auto when no config at all
            (squad_dir / "config.json").write_text("{}", encoding="utf-8")
            r5 = resolve_for_agent(
                repo_path=str(repo_path),
                agent_name="tester",
                catalog=self.catalog,
                session_directive=None,
            )
            self.assertEqual(r5["layer_won"], "auto")
            self.assertEqual(r5["model_id"], "claude-sonnet-5.5")

    def test_mismatch_detection_in_verify_spawn(self) -> None:
        """verify_spawn detects match and mismatch, reporting provenance disclaimer."""
        # Match without metadata
        v_match = verify_spawn("claude-sonnet-5.5", "claude-sonnet-5.5", has_provider_metadata=False)
        self.assertTrue(v_match["match"])
        self.assertEqual(v_match["status"], "match")
        self.assertFalse(v_match["provenance_verified"])
        self.assertIn("does not prove provider provenance", v_match["message"])

        # Match with metadata
        v_match_meta = verify_spawn("claude-sonnet-5.5", "claude-sonnet-5.5", has_provider_metadata=True)
        self.assertTrue(v_match_meta["match"])
        self.assertTrue(v_match_meta["provenance_verified"])

        # Mismatch
        v_mismatch = verify_spawn("claude-sonnet-5.5", "gpt-5.6-terra")
        self.assertFalse(v_mismatch["match"])
        self.assertEqual(v_mismatch["status"], "mismatch")
        self.assertIn("Model ID mismatch", v_mismatch["message"])

    def test_user_registry_anti_forgery(self) -> None:
        """Caller registry cannot overwrite authoritative user registry entries."""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json") as f:
            json.dump({"openrouter": "authentic-prefix-1234"}, f)
            f.flush()

            loaded = load_user_registry(f.name)
            self.assertEqual(loaded["openrouter"], "authentic-prefix-1234")

            # Caller attempts to forge/overwrite openrouter
            merged = get_effective_registry(loaded, {"openrouter": "forged-prefix-9999", "newprov": "prefix-new"})
            self.assertEqual(merged["openrouter"], "authentic-prefix-1234")
            self.assertEqual(merged["newprov"], "prefix-new")


if __name__ == "__main__":
    unittest.main()
