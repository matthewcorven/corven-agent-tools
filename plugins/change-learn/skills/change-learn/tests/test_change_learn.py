"""Tests for the change-learn skill scripts (lineage.py and deck.py).

Run from the repo root:
  python3 -m unittest discover -s plugins/change-learn/skills/change-learn/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(TESTS_DIR, "..", "scripts")
LINEAGE = os.path.join(SCRIPTS_DIR, "lineage.py")
DECK = os.path.join(SCRIPTS_DIR, "deck.py")

sys.path.insert(0, SCRIPTS_DIR)
import deck as deck_mod  # noqa: E402
import lineage as lineage_mod  # noqa: E402


def sh(cwd, *args, env_extra=None):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"{args} failed: {proc.stderr}")
    return proc.stdout


def run_lineage(repo, *args):
    out = sh(repo, sys.executable, LINEAGE, "--repo", repo, *args)
    return json.loads(out)


class GitRepoFixture(unittest.TestCase):
    """Creates a temp repo with two commits, staged, working, and untracked state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-test-")
        self.repo = self._tmp.name
        sh(self.repo, "git", "init", "-q", "-b", "main")

        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, "docs"))
        self._write("src/app.py", "def main():\n    return 1\n")
        self._write("docs/guide.md", "# Guide\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "initial layout")
        self.base = sh(self.repo, "git", "rev-parse", "HEAD").strip()

        self._write("src/app.py", "def main():\n    # TODO: handle zero\n    return 2\n")
        self._write("src/util.py", "def helper():\n    return 'x'\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "add helper and tweak app")

        self._write("src/staged.py", "STAGED = True\n")
        sh(self.repo, "git", "add", "src/staged.py")
        self._write("docs/guide.md", "# Guide\n\nMore words.\n")  # working change
        self._write("notes.txt", "untracked line one\nline two\n")  # untracked

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, content):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


class TestLineageSummary(GitRepoFixture):
    def test_commit_range_summary(self):
        data = run_lineage(self.repo, "summary", "--from", self.base, "--to", "HEAD")
        self.assertEqual(data["totals"]["commits"], 1)
        paths = {f["p"] for f in data["files"]}
        self.assertEqual(paths, {"src/app.py", "src/util.py"})
        self.assertEqual(data["commits"][0]["s"], "add helper and tweak app")
        self.assertGreater(data["totals"]["adds"], 0)

    def test_areas_grouping(self):
        data = run_lineage(self.repo, "summary", "--from", self.base)
        areas = {a["area"] for a in data["areas"]}
        self.assertIn("src", areas)

    def test_flags_detect_todo(self):
        data = run_lineage(self.repo, "summary", "--from", self.base)
        kinds = {(fl["p"], fl["kind"]) for fl in data["flags"]}
        self.assertIn(("src/app.py", "todo"), kinds)

    def test_staged_working_untracked_combo(self):
        data = run_lineage(
            self.repo, "summary", "--no-commits",
            "--staged", "--working", "--untracked")
        paths = {f["p"]: f["src"] for f in data["files"]}
        self.assertEqual(paths.get("src/staged.py"), "staged")
        self.assertEqual(paths.get("docs/guide.md"), "working")
        untracked = {u["p"]: u["lines"] for u in data["untracked"]}
        self.assertEqual(untracked.get("notes.txt"), 2)

    def test_untracked_counts_in_totals(self):
        data = run_lineage(self.repo, "summary", "--no-commits", "--untracked")
        self.assertEqual(data["totals"]["files"], len(data["untracked"]))


class TestConfig(GitRepoFixture):
    def _write_config(self, cfg):
        self._write(".change-learn.json", json.dumps(cfg))

    def test_ignore_glob_excludes_files(self):
        self._write_config({"ignore": ["src/util.py"]})
        data = run_lineage(self.repo, "summary", "--from", self.base)
        paths = {f["p"] for f in data["files"]}
        self.assertNotIn("src/util.py", paths)
        self.assertIn("src/app.py", paths)

    def test_highlight_sorts_first(self):
        self._write_config({"highlight": ["src/util.py"]})
        data = run_lineage(self.repo, "summary", "--from", self.base)
        self.assertEqual(data["files"][0]["p"], "src/util.py")
        self.assertTrue(data["files"][0].get("hl"))

    def test_custom_areas(self):
        self._write_config({"areas": {"Code": "src/**"}})
        data = run_lineage(self.repo, "summary", "--from", self.base)
        self.assertIn("Code", {a["area"] for a in data["areas"]})

    def test_context_files_reported_when_present(self):
        self._write("ARCH.md", "# Arch\n")
        self._write_config({"context_files": ["ARCH.md", "missing.md"]})
        data = run_lineage(self.repo, "summary", "--from", self.base)
        self.assertEqual(data["config"]["context_files"], ["ARCH.md"])
        self.assertTrue(data["config"]["present"])

    def test_config_command(self):
        self._write_config({"focus": ["src/**"]})
        out = sh(self.repo, sys.executable, LINEAGE, "--repo", self.repo, "config")
        cfg = json.loads(out)
        self.assertEqual(cfg["focus"], ["src/**"])
        self.assertTrue(cfg["path"].endswith(".change-learn.json"))


class TestDiffMode(GitRepoFixture):
    def test_diff_writes_patch_manifest(self):
        out = sh(self.repo, sys.executable, LINEAGE, "--repo", self.repo,
                 "diff", "--from", self.base, "src/app.py")
        manifest = json.loads(out)
        self.assertEqual(len(manifest["files"]), 1)
        entry = manifest["files"][0]
        self.assertEqual(entry["p"], "src/app.py")
        with open(entry["patch"], encoding="utf-8") as f:
            patch = f.read()
        self.assertIn("+    return 2", patch)
        self.assertTrue(entry["hunks"])


class TestDeckScaffold(GitRepoFixture):
    def _scaffold(self, **kwargs):
        data = run_lineage(
            self.repo, "summary", "--from", self.base, "--untracked")
        analysis = os.path.join(self.repo, "analysis.json")
        with open(analysis, "w", encoding="utf-8") as f:
            json.dump(data, f)
        out = os.path.join(self.repo, "deck.md")
        args = [sys.executable, DECK, "scaffold", "--analysis", analysis, "-o", out,
                "--title", kwargs.get("title", "Test deck")]
        if "theme" in kwargs:
            args += ["--theme", kwargs["theme"]]
        result = json.loads(sh(self.repo, *args))
        with open(out, encoding="utf-8") as f:
            return result, f.read()

    def test_scaffold_structure(self):
        result, deck = self._scaffold()
        self.assertTrue(deck.startswith("---\nmarp: true"))
        self.assertIn("```mermaid", deck)
        self.assertIn("gitGraph", deck)
        self.assertIn("pie showData", deck)
        self.assertIn("<!-- ai-fill:", deck)
        self.assertIn("## The story at a glance", deck)
        self.assertIn("## Appendix — full file inventory", deck)
        self.assertGreaterEqual(result["slides"], 5)

    def test_scaffold_theme_and_untracked_slide(self):
        _, deck = self._scaffold(theme="gradient")
        self.assertIn("theme: gradient", deck)
        self.assertIn("Brand-new files (untracked)", deck)
        self.assertIn("`notes.txt`", deck)

    def test_high_level_precedes_low_level(self):
        _, deck = self._scaffold()
        self.assertLess(deck.index("The story at a glance"),
                        deck.index("Appendix"))


class TestDeckUnits(unittest.TestCase):
    def test_mermaid_block_regex(self):
        md = "x\n```mermaid\npie\n```\ny\n```python\npass\n```\n"
        blocks = deck_mod.MERMAID_BLOCK.findall(md)
        self.assertEqual(blocks, ["pie\n"])

    def test_git_graph_caps_and_sanitizes(self):
        commits = [{"sha": f"s{i}", "s": 'fix "quoted" thing'} for i in range(20)]
        graph = deck_mod.git_graph(commits)
        self.assertEqual(graph.count("commit id:"), 12)
        self.assertNotIn('\\"quoted\\"', graph)

    def test_area_pie_skips_zero_churn(self):
        pie = deck_mod.area_pie([
            {"area": "a", "adds": 5, "dels": 1},
            {"area": "b", "adds": 0, "dels": 0},
        ])
        self.assertIn('"a" : 6', pie)
        self.assertNotIn('"b"', pie)


class TestLineageUnits(unittest.TestCase):
    def test_parse_numstat_rename(self):
        raw = "1\t2\t\0old.py\0new.py\0" + "3\t4\tplain.py\0"
        files = lineage_mod.parse_numstat_z(raw)
        self.assertEqual(files[0], {"a": 1, "d": 2, "from": "old.py", "p": "new.py"})
        self.assertEqual(files[1]["p"], "plain.py")

    def test_matches_any_basename_and_glob(self):
        self.assertTrue(lineage_mod.matches_any("a/b/package-lock.json",
                                                ["package-lock.json"]))
        self.assertTrue(lineage_mod.matches_any("src/x.py", ["src/*.py"]))
        self.assertFalse(lineage_mod.matches_any("src/x.py", ["docs/**"]))


if __name__ == "__main__":
    unittest.main()
