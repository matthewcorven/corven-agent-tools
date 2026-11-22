"""Portable skill/package contracts; browser behavior lives in the golden example."""

import hashlib
import json
from pathlib import Path
import re
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
SKILL = PLUGIN / "skills" / "visual-proposal"
GOLDEN = SKILL / "examples" / "golden"


class VisualProposalPackageTests(unittest.TestCase):
    def test_marketplace_registers_matching_plugin(self):
        manifest = json.loads((PLUGIN / "plugin.json").read_text())
        marketplace = json.loads((REPO / ".github/plugin/marketplace.json").read_text())
        entries = [item for item in marketplace["plugins"] if item["name"] == manifest["name"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], manifest["version"])
        self.assertEqual((REPO / entries[0]["source"]).resolve(), PLUGIN)

    def test_original_prompt_is_verbatim(self):
        text = (SKILL / "references/original-prompt.md").read_text()
        prompt = re.search(r"```text\n(.*?)\n```", text, re.S).group(1)
        self.assertEqual(len(prompt), 1774)
        self.assertEqual(
            hashlib.sha256(prompt.encode()).hexdigest(),
            "4341a8c6271a4ed974c1e613e053e9cee73f409b9dc393d340617afb55671d57",
            "The authentic prompt must not be copy-edited, rewrapped or normalized.",
        )

    def test_all_relative_document_links_resolve(self):
        documents = [PLUGIN / "README.md", SKILL / "SKILL.md"]
        documents += list((SKILL / "references").glob("*.md"))
        documents += [GOLDEN / "README.md"]
        for document in documents:
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
                if "://" in link or link.startswith("#"):
                    continue
                with self.subTest(document=document.name, link=link):
                    self.assertTrue((document.parent / link.split("#")[0]).is_file())

    def test_golden_slide_and_diagram_contract(self):
        markdown = (GOLDEN / "deck.md").read_text()
        ids = re.findall(r"<!-- slide: ([a-z0-9-]+) -->", markdown)
        headings = re.findall(r"^#{1,2} .+$", markdown, re.M)
        self.assertEqual(len(ids), len(set(ids)), "Slide IDs must be unique")
        self.assertEqual(len(ids), len(headings))
        required = {
            "decision-outcome", "prd", "scope", "context", "services-stores",
            "owner-usecases", "team", "lifecycle", "gates", "human-seams",
            "change-at-rest", "activation", "recovery", "policy", "delivery", "trigger-catalog",
            "decisions-evidence",
        }
        self.assertTrue(required.issubset(ids), required.difference(ids))
        blocks = re.findall(r"```(mermaid|plantuml)\n(.*?)```", markdown, re.S)
        self.assertEqual({language for language, _ in blocks}, {"mermaid", "plantuml"})
        for language, source in blocks:
            self.assertRegex(source, r"(?:%%|') title: .+")
            if language == "plantuml":
                self.assertIn("!pragma layout smetana", source)
        focus = json.loads((GOLDEN / "focus.json").read_text())
        self.assertIn(focus["focus"], ids)
        self.assertTrue(focus["downloadName"].endswith(".md"))

    def test_fixture_sources_are_local_and_explicitly_fictional(self):
        sources = json.loads((GOLDEN / "sources.json").read_text())
        self.assertIn("Fictional", sources["scope"])
        self.assertTrue(sources["documents"])
        for item in sources["documents"]:
            self.assertTrue((GOLDEN / item["path"]).is_file())
        self.assertTrue(all(item["status"] == "unaccepted" for item in sources["decisions"]))
        for path in GOLDEN.iterdir():
            if path.suffix not in (".mjs", ".js", ".css", ".md", ".json"):
                continue
            if path.name == "package-lock.json":
                continue
            text = path.read_text()
            self.assertNotIn("/Users/core/", text, f"Machine-specific path in {path.name}")
            self.assertNotIn("corven-agents", text, f"Source-project coupling in {path.name}")

    def test_renderer_dependencies_and_jar_are_pinned(self):
        package = json.loads((GOLDEN / "package.json").read_text())
        lock = json.loads((GOLDEN / "package-lock.json").read_text())
        for name, version in package["devDependencies"].items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+$")
            self.assertEqual(lock["packages"][f"node_modules/{name}"]["version"], version)
        jar_module = (GOLDEN / "plantuml.mjs").read_text()
        self.assertRegex(jar_module, r"plantumlSha256 = '[0-9a-f]{64}'")
        self.assertIn("verifyJar(bytes)", jar_module)
        self.assertIn("await readVerifiedJar()", (GOLDEN / "build.mjs").read_text())


if __name__ == "__main__":
    unittest.main()
