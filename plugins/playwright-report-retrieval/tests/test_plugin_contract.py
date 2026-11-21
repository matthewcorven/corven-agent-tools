#!/usr/bin/env python3
"""Contract tests for the plugin's shipped surface.

These guard the things a consuming repository or downstream plugin depends on and
that a refactor could break silently: version agreement across the three
manifests, the CLI's subcommand and run-root contracts, and the two skills that
make up the published standard.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
MARKETPLACE = REPO_ROOT / ".github" / "plugin" / "marketplace.json"

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import artifacts as artifacts_mod  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class VersionAgreementTests(unittest.TestCase):
    """A partial bump leaves a stale MCP server serving old tools to new clients."""

    def test_plugin_marketplace_and_server_versions_agree(self) -> None:
        plugin = json.loads(_read(PLUGIN_ROOT / "plugin.json"))
        marketplace = json.loads(_read(MARKETPLACE))
        entry = next(
            (p for p in marketplace["plugins"] if p["name"] == "playwright-report-retrieval"),
            None,
        )
        self.assertIsNotNone(entry, "playwright-report-retrieval missing from marketplace.json")

        server_version = re.search(
            r'^SERVER_VERSION = "([^"]+)"$', _read(PLUGIN_ROOT / "server.py"), re.MULTILINE
        ).group(1)

        versions = {
            "plugin.json": plugin["version"],
            "marketplace.json": entry["version"],
            "server.py": server_version,
            "artifacts.py": artifacts_mod.PLUGIN_VERSION,
        }
        self.assertEqual(
            set(versions.values()), {"0.3.0"}, f"version mismatch across manifests: {versions}"
        )

    def test_plugin_name_is_unchanged(self) -> None:
        """The name keys consuming repos' settings.json and the installed-plugin path."""
        plugin = json.loads(_read(PLUGIN_ROOT / "plugin.json"))
        self.assertEqual(plugin["name"], "playwright-report-retrieval")


class CliContractTests(unittest.TestCase):
    """Downstream consumers call these subcommands; renaming one is a breaking change."""

    EXPECTED_SUBCOMMANDS = {
        "root",
        "run",
        "record-meta",
        "find",
        "usage",
        "prune",
        "check-ci-budget",
        "stamp",
        "check-parity",
    }

    def test_subcommands_are_stable(self) -> None:
        parser = artifacts_mod._build_parser()
        action = next(a for a in parser._actions if getattr(a, "choices", None))
        self.assertEqual(set(action.choices), self.EXPECTED_SUBCOMMANDS)

    def test_run_root_layout_is_stable(self) -> None:
        self.assertEqual(artifacts_mod.PHASES, ("planning", "mockups", "dev", "debug", "qa"))

    def test_artifact_script_is_executable_and_stdlib_only(self) -> None:
        script = PLUGIN_ROOT / "scripts" / "artifacts.py"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111, "artifacts.py must be executable")

        source = _read(script)
        imports = set(re.findall(r"^(?:import|from) ([A-Za-z_][\w.]*)", source, re.MULTILINE))
        third_party = {
            name for name in imports
            if name.split(".")[0] not in sys.stdlib_module_names
            and name.split(".")[0] not in {"__future__", "artifacts"}
        }
        self.assertEqual(third_party, set(), f"non-stdlib imports: {third_party}")


class SkillContractTests(unittest.TestCase):
    def test_both_skills_exist_with_required_frontmatter(self) -> None:
        for name in ("playwright-report-retrieval", "e2e-artifacts"):
            skill = PLUGIN_ROOT / "skills" / name / "SKILL.md"
            self.assertTrue(skill.is_file(), f"missing skills/{name}/SKILL.md")
            text = _read(skill)
            frontmatter = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL).group(1)
            self.assertIn(f"name: {name}", frontmatter)
            self.assertRegex(frontmatter, r"(?m)^description: >-\s*$")

    def test_skills_cross_link_each_other(self) -> None:
        e2e = _read(PLUGIN_ROOT / "skills" / "e2e-artifacts" / "SKILL.md")
        report = _read(PLUGIN_ROOT / "skills" / "playwright-report-retrieval" / "SKILL.md")
        self.assertIn("../playwright-report-retrieval/SKILL.md", e2e)
        self.assertIn("../e2e-artifacts/SKILL.md", report)

    def test_e2e_artifacts_skill_documents_the_required_contracts(self) -> None:
        text = _read(PLUGIN_ROOT / "skills" / "e2e-artifacts" / "SKILL.md")
        required = [
            "wt-<basename>-<sha8>",
            "runs/<utc>-<rand6>",
            "planning/  mockups/  dev/  debug/  qa/",
            "runs.jsonl",
            "ARTIFACT run=",
            "refs/issue-<N>/",
            "artifacts.budget.json",
            "core.hooksPath .githooks",
            "checkpoint",
            "VENDORED_SHA256",
        ]
        for token in required:
            self.assertIn(token, text, f"e2e-artifacts SKILL.md must document {token!r}")

    def test_report_skill_documents_declared_profiles_and_commit_boundary(self) -> None:
        text = _read(PLUGIN_ROOT / "skills" / "playwright-report-retrieval" / "SKILL.md")
        self.assertIn("## 4. Declared profiles / overrides", text)
        for profile in ("Serial", "Loopback-only", "No-capture"):
            self.assertIn(profile, text, f"§4 must cover the {profile} profile")
        self.assertIn("Governed by the consuming repository's own contract", text)
        # The folded authoring content must be present, not merely referenced.
        for token in ("getByRole", "getByTestId", "waitForTimeout", "show-trace"):
            self.assertIn(token, text, f"folded authoring content missing {token!r}")


if __name__ == "__main__":
    unittest.main()
