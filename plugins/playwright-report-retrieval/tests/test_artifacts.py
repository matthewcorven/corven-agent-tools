#!/usr/bin/env python3
"""Hermetic tests for the canonical ``scripts/artifacts.py`` CLI.

Every test builds its own throwaway git repository (and linked worktree where the
layout matters) under a temp directory, so the suite never touches the developer's
checkout, the network, or ``~/.copilot``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_PY = PLUGIN_ROOT / "scripts" / "artifacts.py"

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import artifacts as artifacts_mod  # noqa: E402


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # A developer's own override must not redirect the test's artifact root.
    env.pop("E2E_ARTIFACTS_ROOT", None)
    env.pop("VDS_ARTIFACTS_ROOT", None)
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = _run(["git", *args], cwd=cwd)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


def _init_repo_with_worktree(base: Path, branch: str = "copilot/349-visual-test") -> tuple[Path, Path]:
    """Create ``base/sample-repo`` plus a linked worktree ``base/sample-repo-349``."""
    main_repo = base / "sample-repo"
    main_repo.mkdir()
    _git(main_repo, "init", "-b", "develop")
    _git(main_repo, "config", "user.email", "test@example.local")
    _git(main_repo, "config", "user.name", "Test User")
    (main_repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(main_repo, "add", "README.md")
    _git(main_repo, "commit", "-m", "init")

    worktree = base / "sample-repo-349"
    _git(main_repo, "worktree", "add", "-b", branch, str(worktree), "develop")
    return (Path(os.path.realpath(main_repo)), Path(os.path.realpath(worktree)))


def _artifacts(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(ARTIFACTS_PY), "--repo-path", str(repo), *args])


class ArtifactRootTests(unittest.TestCase):
    def test_sibling_root_is_shared_by_main_repo_and_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            main_repo, worktree = _init_repo_with_worktree(base)

            main_ctx = json.loads(_artifacts(main_repo, "root", "--json").stdout)
            wt_ctx = json.loads(_artifacts(worktree, "root", "--json").stdout)

            expected_root = str(base / "sample-repo-artifacts")
            self.assertEqual(main_ctx["artifactRoot"], expected_root)
            self.assertEqual(wt_ctx["artifactRoot"], expected_root)
            self.assertNotEqual(main_ctx["worktreeId"], wt_ctx["worktreeId"])
            self.assertRegex(wt_ctx["worktreeId"], r"^wt-sample-repo-349-[0-9a-f]{8}$")

    def test_root_never_lands_inside_the_git_tree(self) -> None:
        """Artifacts outside the tree are what keeps worktree removal from deleting them."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            main_repo, _ = _init_repo_with_worktree(base)

            ctx = json.loads(_artifacts(main_repo, "root", "--json").stdout)
            artifact_root = Path(ctx["artifactRoot"]).resolve()
            self.assertFalse(
                artifact_root.is_relative_to(Path(ctx["mainRepoPath"]).resolve()),
                "artifact root must be a sibling of the repository, not inside it",
            )
            self.assertEqual(artifact_root.name, "sample-repo-artifacts")


class RunLayoutTests(unittest.TestCase):
    def test_run_creates_layout_and_all_four_discovery_layers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)
            artifact_root = base / "sample-repo-artifacts"

            first = _artifacts(worktree, "run", "--phase", "qa", "--spec", "app-shell")
            self.assertEqual(first.returncode, 0, first.stderr)

            # Layer 1: machine-readable stdout line, issue inferred from the branch.
            self.assertRegex(
                first.stdout.strip(),
                r"^ARTIFACT run=\d{8}T\d{6}Z-[0-9a-f]{6} issue=349 phase=qa "
                r"spec=app-shell path=.+$",
            )

            second = _artifacts(
                worktree, "run", "--phase", "planning", "--issue", "#229",
                "--spec", "mockup-audit", "--json",
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            record = json.loads(second.stdout)

            first_id = re.search(r"run=(\S+)", first.stdout).group(1)
            self.assertNotEqual(first_id, record["run"], "back-to-back runs need unique ids")

            run_path = Path(record["path"])
            self.assertRegex(run_path.name, r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
            for phase in ("planning", "mockups", "dev", "debug", "qa"):
                self.assertTrue((run_path / phase).is_dir(), f"missing {phase}/")

            manifest = json.loads(
                (artifact_root / record["worktreeId"] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["branch"], "copilot/349-visual-test")
            self.assertEqual(manifest["worktreePath"], str(worktree))
            self.assertTrue(manifest["firstSeen"])
            self.assertTrue(manifest["lastUsed"])

            # Layer 3: both latest pointers resolve to the newest run.
            self.assertEqual((artifact_root / "latest").resolve(), run_path.resolve())
            self.assertEqual(
                (artifact_root / record["worktreeId"] / "latest").resolve(), run_path.resolve()
            )

            # Layer 4: refs/issue-<N>/<run> symlinks.
            self.assertTrue((artifact_root / "refs" / "issue-349" / first_id).is_symlink())
            self.assertEqual(
                (artifact_root / "refs" / "issue-229" / record["run"]).resolve(), run_path.resolve()
            )

    def test_record_meta_and_find_read_the_append_only_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)

            record = json.loads(
                _artifacts(
                    worktree, "run", "--phase", "qa", "--issue", "12",
                    "--spec", "e2e-artifacts", "--json",
                ).stdout
            )
            run_path = Path(record["path"])
            (run_path / "qa" / "01-capture.png").write_bytes(b"x" * 1024)

            meta = json.loads(
                _artifacts(
                    worktree, "record-meta", "--run-dir", str(run_path),
                    "--status", "passed", "--json",
                ).stdout
            )
            self.assertEqual(meta["files"], 1)
            self.assertEqual(meta["bytes"], 1024)
            self.assertEqual(meta["status"], "passed")

            # Layer 2: the append-only index is queryable.
            found = json.loads(_artifacts(worktree, "find", "--issue", "12", "--json").stdout)
            self.assertEqual([r["run"] for r in found], [record["run"]])
            self.assertEqual(found[0]["status"], "passed")

            latest = json.loads(_artifacts(worktree, "find", "--latest", "--json").stdout)
            self.assertEqual(latest[0]["run"], record["run"])

            # The index stays one JSON object per line (append-only contract).
            index = base / "sample-repo-artifacts" / "runs.jsonl"
            for line in index.read_text(encoding="utf-8").splitlines():
                self.assertIsInstance(json.loads(line), dict)

    def test_invalid_phase_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)
            proc = _artifacts(worktree, "run", "--phase", "nope")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("invalid choice", proc.stderr)

    def test_create_run_rejects_an_unknown_phase_at_the_api_boundary(self) -> None:
        """The MCP layer calls ``create_run`` directly, so the guard cannot live only in argparse."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)
            with self.assertRaises(ValueError) as ctx:
                artifacts_mod.create_run(repo_path=worktree, phase="screenshots")
            self.assertIn("Invalid phase", str(ctx.exception))


class BudgetTests(unittest.TestCase):
    def test_local_usage_is_warn_only_and_ranks_oldest_worktree_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            main_repo, worktree = _init_repo_with_worktree(base)
            artifact_root = base / "sample-repo-artifacts"

            # A 10 KB budget makes a couple of writes enough to cross the threshold.
            budget_path = worktree / "artifacts.budget.json"
            budget_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "local": {
                            "maxGb": 10_000 / (1024**3),
                            "warnAtPercent": 80,
                            "hookThrottleSeconds": 300,
                        },
                        "ci": {"maxMbPerRun": 1, "warnAtPercent": 80},
                    }
                ),
                encoding="utf-8",
            )

            old = json.loads(
                _artifacts(main_repo, "run", "--phase", "qa", "--issue", "10",
                           "--no-budget-check", "--json").stdout
            )
            new = json.loads(
                _artifacts(worktree, "run", "--phase", "qa", "--issue", "349",
                           "--no-budget-check", "--json").stdout
            )

            manifest_path = artifact_root / old["worktreeId"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["lastUsed"] = "2026-01-01T00:00:00Z"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            (Path(old["path"]) / "qa" / "a.bin").write_bytes(b"a" * 5000)
            under = _artifacts(worktree, "usage", "--budget-file", str(budget_path))
            self.assertEqual(under.returncode, 0)
            self.assertEqual(under.stdout.strip(), "", "under budget must stay silent")

            (Path(new["path"]) / "qa" / "b.bin").write_bytes(b"b" * 7000)
            over = _artifacts(worktree, "usage", "--budget-file", str(budget_path), "--json")
            self.assertEqual(over.returncode, 0, "budget checks must never fail a build")
            report = json.loads(over.stdout)
            self.assertTrue(report["warn"])
            self.assertTrue(report["overLimit"])
            # Symlinks (latest, refs/*) must not be counted as payload bytes.
            self.assertLess(report["totalBytes"], 20_000)
            self.assertEqual(report["offenders"][0]["worktreeId"], old["worktreeId"])
            self.assertIn("WARNING:", report["warningMessage"])
            self.assertIn("prune --older-than 7d --dry-run", report["warningMessage"])

    def test_usage_throttle_skips_the_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)
            budget_path = worktree / "artifacts.budget.json"
            budget_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "local": {"maxGb": 10_000 / (1024**3), "warnAtPercent": 80},
                        "ci": {"maxMbPerRun": 1},
                    }
                ),
                encoding="utf-8",
            )
            record = json.loads(
                _artifacts(worktree, "run", "--phase", "qa", "--no-budget-check", "--json").stdout
            )
            (Path(record["path"]) / "qa" / "big.bin").write_bytes(b"z" * 12_000)

            first = _artifacts(worktree, "usage", "--budget-file", str(budget_path), "--throttle")
            self.assertEqual(first.returncode, 0)
            self.assertIn("WARNING:", first.stdout)

            second = _artifacts(worktree, "usage", "--budget-file", str(budget_path), "--throttle")
            self.assertEqual(second.returncode, 0)
            self.assertEqual(second.stdout.strip(), "")

    def test_ci_budget_emits_annotation_but_never_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            budget_path = base / "artifacts.budget.json"
            budget_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "local": {"maxGb": 2, "warnAtPercent": 80},
                        "ci": {"maxMbPerRun": 0.01, "warnAtPercent": 80},  # ~10 KB
                    }
                ),
                encoding="utf-8",
            )
            report_dir = base / "playwright-report"
            report_dir.mkdir()
            (report_dir / "small.bin").write_bytes(b"0" * 2000)

            ok = _run(
                [sys.executable, str(ARTIFACTS_PY), "check-ci-budget",
                 "--budget-file", str(budget_path), str(report_dir)]
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertNotIn("::warning", ok.stdout)

            (report_dir / "big.bin").write_bytes(b"1" * 12_000)
            over = _run(
                [sys.executable, str(ARTIFACTS_PY), "check-ci-budget",
                 "--budget-file", str(budget_path), str(report_dir)]
            )
            self.assertEqual(over.returncode, 0, "warn-only: CI budget must never fail")
            self.assertIn("::warning title=CI Artifact Budget::", over.stdout)


class PruneTests(unittest.TestCase):
    def test_prune_is_dry_run_by_default_and_confirm_cleans_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))
            _, worktree = _init_repo_with_worktree(base)
            artifact_root = base / "sample-repo-artifacts"

            record = json.loads(
                _artifacts(worktree, "run", "--phase", "qa", "--issue", "349",
                           "--no-budget-check", "--json").stdout
            )
            run_path = Path(record["path"])
            stale_id = "20200101T000000Z-abcdef"
            stale_path = run_path.parent / stale_id
            run_path.rename(stale_path)
            (artifact_root / "refs" / "issue-349" / stale_id).symlink_to(
                f"../../{record['worktreeId']}/runs/{stale_id}"
            )
            (stale_path / "qa" / "stale.png").write_bytes(b"z" * 2048)

            dry = json.loads(
                _artifacts(worktree, "prune", "--older-than", "7d", "--json").stdout
            )
            self.assertTrue(dry["dryRun"])
            self.assertFalse(dry["deleted"])
            self.assertEqual(dry["candidateCount"], 1)
            self.assertTrue(stale_path.exists(), "dry-run must not delete")

            confirmed = json.loads(
                _artifacts(worktree, "prune", "--older-than", "7d", "--confirm", "--json").stdout
            )
            self.assertTrue(confirmed["deleted"])
            self.assertFalse(stale_path.exists())
            self.assertFalse((artifact_root / "refs" / "issue-349" / stale_id).is_symlink())
            # The pruned run must leave the append-only index too.
            remaining = [
                json.loads(line)
                for line in (artifact_root / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertNotIn(stale_id, [r.get("run") for r in remaining])


class ProvenanceParityTests(unittest.TestCase):
    """``stamp``/``--check-parity`` is what makes vendoring fail loudly on drift."""

    def test_canonical_file_is_stamped_and_in_parity(self) -> None:
        text = ARTIFACTS_PY.read_text(encoding="utf-8")
        provenance = artifacts_mod.parse_provenance(text)
        self.assertIsNotNone(provenance["sha256"], "canonical file must carry VENDORED_SHA256")
        self.assertEqual(provenance["version"], artifacts_mod.PLUGIN_VERSION)

        report = artifacts_mod.check_parity(ARTIFACTS_PY)
        self.assertTrue(report["parity"], report["problems"])

    def test_stamp_is_idempotent_and_does_not_change_the_body_hash(self) -> None:
        original = ARTIFACTS_PY.read_text(encoding="utf-8")
        once = artifacts_mod.stamp_text(original, version="9.9.9", commit="abcdef123456")
        twice = artifacts_mod.stamp_text(once, version="9.9.9", commit="abcdef123456")
        self.assertEqual(once, twice)
        self.assertEqual(artifacts_mod.body_hash(original), artifacts_mod.body_hash(once))
        self.assertEqual(
            len([line for line in once.splitlines() if line.startswith("# vendored-from:")]), 1
        )

    def test_vendored_copy_matching_the_canonical_file_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            body = artifacts_mod.stamp_text(
                ARTIFACTS_PY.read_text(encoding="utf-8"),
                version=artifacts_mod.PLUGIN_VERSION,
                commit="deadbeef1234",
            )
            vendored.write_text(body, encoding="utf-8")

            report = artifacts_mod.check_parity(vendored)
            self.assertTrue(report["parity"], report["problems"])
            self.assertEqual(report["declaredCommit"], "deadbeef1234")

    def test_edited_vendored_body_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            body = artifacts_mod.stamp_text(
                ARTIFACTS_PY.read_text(encoding="utf-8"),
                version=artifacts_mod.PLUGIN_VERSION,
                commit="deadbeef1234",
            )
            vendored.write_text(body.replace("warn-only", "blocking"), encoding="utf-8")

            report = artifacts_mod.check_parity(vendored)
            self.assertFalse(report["parity"])
            self.assertTrue(any("body hash mismatch" in p for p in report["problems"]))
            self.assertTrue(any("differs from the canonical file" in p for p in report["problems"]))

    def test_unstamped_copy_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            # Simulate a hand-copied file with the stamp lines removed.
            stripped = "\n".join(
                line for line in ARTIFACTS_PY.read_text(encoding="utf-8").splitlines()
                if not artifacts_mod._is_provenance_line(line)
            )
            vendored.write_text(stripped + "\n", encoding="utf-8")

            report = artifacts_mod.check_parity(vendored)
            self.assertFalse(report["parity"])
            self.assertTrue(any("missing '# vendored-from:'" in p for p in report["problems"]))
            self.assertTrue(any("VENDORED_SHA256" in p for p in report["problems"]))

    def test_stale_version_fails_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            vendored.write_text(
                artifacts_mod.stamp_text(
                    ARTIFACTS_PY.read_text(encoding="utf-8"), version="0.1.0", commit="deadbeef1234"
                ),
                encoding="utf-8",
            )
            report = artifacts_mod.check_parity(vendored)
            self.assertFalse(report["parity"])
            self.assertTrue(any("but the canonical plugin is" in p for p in report["problems"]))

    def test_bare_version_without_commit_sha_fails_parity(self) -> None:
        """A bare ``vX.Y.Z`` cannot distinguish pre-merge from post-merge."""
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            text = re.sub(
                r"^# vendored-from:.*$",
                f"# vendored-from: {artifacts_mod.PROVENANCE_SOURCE} v{artifacts_mod.PLUGIN_VERSION}",
                ARTIFACTS_PY.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
            vendored.write_text(text, encoding="utf-8")
            report = artifacts_mod.check_parity(vendored)
            self.assertFalse(report["parity"])
            self.assertTrue(any("no plugin commit sha" in p for p in report["problems"]))

    def test_check_parity_cli_exit_code_tracks_parity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            vendored.write_text(
                artifacts_mod.stamp_text(
                    ARTIFACTS_PY.read_text(encoding="utf-8"),
                    version=artifacts_mod.PLUGIN_VERSION,
                    commit="deadbeef1234",
                ),
                encoding="utf-8",
            )
            ok = _run([sys.executable, str(ARTIFACTS_PY), "check-parity", str(vendored)])
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

            vendored.write_text(vendored.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
            bad = _run([sys.executable, str(ARTIFACTS_PY), "check-parity", str(vendored)])
            self.assertEqual(bad.returncode, 1)
            self.assertIn("PARITY FAIL", bad.stdout)

    def test_stamp_cli_writes_a_verifiable_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vendored = Path(td) / "artifacts.py"
            vendored.write_text(ARTIFACTS_PY.read_text(encoding="utf-8"), encoding="utf-8")

            stamped = _run(
                [sys.executable, str(ARTIFACTS_PY), "stamp", str(vendored),
                 "--commit", "cafebabe1234", "--json"]
            )
            self.assertEqual(stamped.returncode, 0, stamped.stderr)
            payload = json.loads(stamped.stdout)
            self.assertEqual(payload["commit"], "cafebabe1234")
            self.assertEqual(payload["version"], artifacts_mod.PLUGIN_VERSION)

            report = artifacts_mod.check_parity(vendored)
            self.assertTrue(report["parity"], report["problems"])


if __name__ == "__main__":
    unittest.main()
