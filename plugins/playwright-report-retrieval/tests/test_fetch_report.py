#!/usr/bin/env python3
"""Unit tests for the report retrieval script."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import fetch_report as fr  # noqa: E402


def make_zip(path: Path, members: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


class BuildReportUrlTest(unittest.TestCase):
    def test_plain_url_has_no_hash(self) -> None:
        url = fr.build_report_url(Path("/tmp/r/index.html"))
        self.assertTrue(url.startswith("file://"))
        self.assertTrue(url.endswith("/index.html"))
        self.assertNotIn("#", url)

    def test_test_title_is_url_encoded_into_q(self) -> None:
        url = fr.build_report_url(Path("/tmp/r/index.html"), test="Login Flow")
        self.assertTrue(url.endswith("/index.html#?q=Login%20Flow"))

    def test_special_characters_are_encoded(self) -> None:
        url = fr.build_report_url(Path("/tmp/r/index.html"), test="a&b=c/d")
        self.assertTrue(url.endswith("#?q=a%26b%3Dc%2Fd"))

    def test_test_id_takes_precedence_over_title(self) -> None:
        url = fr.build_report_url(Path("/tmp/r/index.html"), test="Login", test_id="3f2a1b")
        self.assertTrue(url.endswith("#?testId=3f2a1b"))


class PruneOldReportsTest(unittest.TestCase):
    def test_removes_entries_older_than_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ns = Path(tmp)
            old = ns / "old-run"
            old.mkdir()
            (old / "index.html").write_text("x")
            fresh = ns / "fresh-run"
            fresh.mkdir()

            now = time.time()
            os.utime(old, (now - 10 * 86400, now - 10 * 86400))

            removed = fr.prune_old_reports(ns, 7, now=now)

            self.assertEqual([p.name for p in removed], ["old-run"])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_missing_namespace_is_a_noop(self) -> None:
        self.assertEqual(fr.prune_old_reports(Path("/nonexistent/xyz"), 7), [])

    def test_zero_days_disables_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ns = Path(tmp)
            old = ns / "old-run"
            old.mkdir()
            os.utime(old, (0, 0))
            self.assertEqual(fr.prune_old_reports(ns, 0), [])
            self.assertTrue(old.exists())


class ExtractZipTest(unittest.TestCase):
    def test_extracts_flat_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {"index.html": "<html></html>"})
            dest = Path(tmp) / "out"
            root = fr.extract_zip(archive, dest)
            self.assertEqual(root, dest)
            self.assertTrue((root / "index.html").is_file())

    def test_finds_index_html_in_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(
                Path(tmp) / "a.zip",
                {"playwright-report/index.html": "<html></html>"},
            )
            root = fr.extract_zip(archive, Path(tmp) / "out")
            self.assertEqual(root.name, "playwright-report")

    def test_rejects_zip_slip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {"../../evil.txt": "pwned"})
            with self.assertRaises(fr.RetrievalError):
                fr.extract_zip(archive, Path(tmp) / "out")

    def test_missing_index_html_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {"readme.txt": "nope"})
            with self.assertRaises(fr.RetrievalError):
                fr.extract_zip(archive, Path(tmp) / "out")


class FakeProvider(fr.CIProvider):
    name = "fake"

    def __init__(self, **_: object) -> None:
        self.downloads = 0

    def resolve_run_id(self, run_id, commit):
        return run_id or "resolved-from-commit"

    def find_artifact(self, run_id, artifact_name):
        return fr.ArtifactRef(run_id=run_id, name=artifact_name)

    def download_artifact(self, ref, destination):
        self.downloads += 1
        make_zip(destination / "report.zip", {"index.html": "<html></html>"})
        return destination


class FetchReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self._orig = dict(fr.PROVIDERS)
        fr.PROVIDERS["fake"] = lambda **_: self.provider

    def tearDown(self) -> None:
        fr.PROVIDERS.clear()
        fr.PROVIDERS.update(self._orig)

    def test_downloads_extracts_and_builds_deep_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = fr.fetch_report(
                run_id="42",
                test="Login Flow",
                namespace=Path(tmp),
                provider_name="fake",
                open_browser=False,
            )
            self.assertEqual(result.run_id, "42")
            self.assertEqual(result.report_dir, Path(tmp) / "42")
            self.assertTrue(result.index_html.is_file())
            self.assertTrue(result.url.endswith("#?q=Login%20Flow"))
            self.assertEqual(self.provider.downloads, 1)

    def test_second_call_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fr.fetch_report(run_id="42", namespace=Path(tmp), provider_name="fake", open_browser=False)
            fr.fetch_report(run_id="42", namespace=Path(tmp), provider_name="fake", open_browser=False)
            self.assertEqual(self.provider.downloads, 1)

    def test_force_redownloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fr.fetch_report(run_id="42", namespace=Path(tmp), provider_name="fake", open_browser=False)
            fr.fetch_report(
                run_id="42", namespace=Path(tmp), provider_name="fake", open_browser=False, force=True
            )
            self.assertEqual(self.provider.downloads, 2)

    def test_cached_report_is_served_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ns = Path(tmp)
            cached = ns / "42"
            cached.mkdir()
            (cached / "index.html").write_text("<html></html>")

            # No provider is registered, so any network path would raise.
            result = fr.fetch_report(
                run_id="42", namespace=ns, provider_name="missing", open_browser=False
            )

            self.assertEqual(result.report_dir, cached)
            self.assertTrue(result.url.endswith("/index.html"))

    def test_commit_resolves_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = fr.fetch_report(
                commit="abc1234", namespace=Path(tmp), provider_name="fake", open_browser=False
            )
            self.assertEqual(result.run_id, "resolved-from-commit")

    def test_requires_a_target(self) -> None:
        with self.assertRaises(fr.RetrievalError):
            fr.fetch_report(provider_name="fake", open_browser=False)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(fr.RetrievalError):
            fr.fetch_report(run_id="1", provider_name="nope", open_browser=False)

    def test_prunes_stale_reports_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ns = Path(tmp)
            stale = ns / "stale-run"
            stale.mkdir()
            os.utime(stale, (0, 0))

            result = fr.fetch_report(
                run_id="42", namespace=ns, provider_name="fake", open_browser=False
            )

            self.assertFalse(stale.exists())
            self.assertEqual([Path(p).name for p in result.pruned], ["stale-run"])


class GitHubActionsProviderTest(unittest.TestCase):
    def test_resolve_run_id_prefers_completed_run(self) -> None:
        provider = fr.GitHubActionsProvider(repo="o/r")
        payload = {
            "workflow_runs": [
                {"id": 1, "status": "in_progress"},
                {"id": 2, "status": "completed"},
            ]
        }
        with mock.patch.object(provider, "_api", return_value=payload):
            self.assertEqual(provider.resolve_run_id(None, "abc"), "2")

    def test_resolve_run_id_raises_when_no_runs(self) -> None:
        provider = fr.GitHubActionsProvider(repo="o/r")
        with mock.patch.object(provider, "_api", return_value={"workflow_runs": []}):
            with self.assertRaises(fr.RetrievalError):
                provider.resolve_run_id(None, "abc")

    def test_find_artifact_picks_newest_match(self) -> None:
        provider = fr.GitHubActionsProvider(repo="o/r")
        payload = {
            "artifacts": [
                {"name": "playwright-report", "created_at": "2024-01-01T00:00:00Z"},
                {"name": "playwright-report", "created_at": "2024-06-01T00:00:00Z"},
                {"name": "other", "created_at": "2024-07-01T00:00:00Z"},
            ]
        }
        with mock.patch.object(provider, "_api", return_value=payload):
            ref = provider.find_artifact("7", "playwright-report")
        self.assertEqual(ref.metadata["created_at"], "2024-06-01T00:00:00Z")

    def test_find_artifact_lists_available_names_on_miss(self) -> None:
        provider = fr.GitHubActionsProvider(repo="o/r")
        payload = {"artifacts": [{"name": "coverage"}]}
        with mock.patch.object(provider, "_api", return_value=payload):
            with self.assertRaises(fr.RetrievalError) as ctx:
                provider.find_artifact("7", "playwright-report")
        self.assertIn("coverage", str(ctx.exception))

    def test_expired_artifact_raises(self) -> None:
        provider = fr.GitHubActionsProvider(repo="o/r")
        ref = fr.ArtifactRef(run_id="7", name="playwright-report", expired=True)
        with self.assertRaises(fr.RetrievalError):
            provider.download_artifact(ref, Path("/tmp/never"))


class CliTest(unittest.TestCase):
    def test_parser_requires_a_target(self) -> None:
        with self.assertRaises(SystemExit):
            fr.build_parser().parse_args([])

    def test_parser_rejects_both_targets(self) -> None:
        with self.assertRaises(SystemExit):
            fr.build_parser().parse_args(["--run-id", "1", "--commit", "abc"])

    def test_main_reports_errors_without_traceback(self) -> None:
        with mock.patch.object(fr, "fetch_report", side_effect=fr.RetrievalError("boom")):
            self.assertEqual(fr.main(["--run-id", "1"]), 1)

    def test_main_emits_json(self) -> None:
        result = fr.FetchResult(
            run_id="1",
            report_dir=Path("/tmp/1"),
            index_html=Path("/tmp/1/index.html"),
            url="file:///tmp/1/index.html",
            artifact_name="playwright-report",
        )
        with mock.patch.object(fr, "fetch_report", return_value=result):
            with mock.patch("sys.stdout") as out:
                self.assertEqual(fr.main(["--run-id", "1", "--json"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["run_id"], "1")


if __name__ == "__main__":
    unittest.main()
