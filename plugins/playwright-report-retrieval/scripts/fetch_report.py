#!/usr/bin/env python3
"""Fetch a Playwright HTML report from CI and open it deep-linked to a test.

Usage:
    fetch_report.py --run-id 123456789
    fetch_report.py --run-id 123456789 --test "Login Flow"
    fetch_report.py --commit abc1234 --test-id 3f2a1b
    fetch_report.py --run-id 123456789 --no-open --json

The report is extracted to ``<namespace>/<run-id>/`` and the local ``index.html``
is opened in the default browser with the Playwright SPA hash appended
(``#?q=<urlencoded>`` or ``#?testId=<id>``).

Directories in the namespace older than ``--max-age-days`` (default 7) are
purged before each download so the cache cannot grow without bound.

Provider support is a seam: ``GitHubActionsProvider`` is the only implementation
today, but ``--provider`` selects from ``PROVIDERS`` so other CI systems can be
added without touching the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_ARTIFACT_NAME = "playwright-report"
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_NAMESPACE = "playwright-reports"


class RetrievalError(RuntimeError):
    """Raised when a report cannot be located, downloaded or extracted."""


# --------------------------------------------------------------------------- #
# Provider seam
# --------------------------------------------------------------------------- #


@dataclass
class ArtifactRef:
    """A located CI artifact."""

    run_id: str
    name: str
    download_url: Optional[str] = None
    size_bytes: Optional[int] = None
    expired: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class CIProvider:
    """Interface every CI provider implementation must satisfy."""

    name = "base"

    def resolve_run_id(self, run_id: Optional[str], commit: Optional[str]) -> str:
        raise NotImplementedError

    def find_artifact(self, run_id: str, artifact_name: str) -> ArtifactRef:
        raise NotImplementedError

    def download_artifact(self, ref: ArtifactRef, destination: Path) -> Path:
        raise NotImplementedError


class GitHubActionsProvider(CIProvider):
    """GitHub Actions implementation backed by the ``gh`` CLI.

    ``gh`` is used rather than raw HTTP so authentication, enterprise hosts and
    token refresh are handled by the user's existing login.
    """

    name = "github-actions"

    def __init__(self, repo: Optional[str] = None, workflow: Optional[str] = None) -> None:
        self.repo = repo
        self.workflow = workflow

    # -- helpers ---------------------------------------------------------- #

    def _gh(self, args: list[str], *, binary: bool = False) -> Any:
        cmd = ["gh", *args]
        if self.repo:
            cmd.extend(["--repo", self.repo])
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=not binary,
            )
        except FileNotFoundError as exc:
            raise RetrievalError(
                "The GitHub CLI ('gh') is required but was not found on PATH. "
                "Install it from https://cli.github.com/ and run 'gh auth login'."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise RetrievalError(f"'gh {' '.join(args)}' failed: {stderr.strip()}") from exc
        return result.stdout

    def _api(self, path: str) -> Any:
        return json.loads(self._gh(["api", path]))

    # -- interface -------------------------------------------------------- #

    def resolve_run_id(self, run_id: Optional[str], commit: Optional[str]) -> str:
        if run_id:
            return str(run_id)
        if not commit:
            raise RetrievalError("Either --run-id or --commit is required.")

        query = f"/repos/{{owner}}/{{repo}}/actions/runs?head_sha={commit}&per_page=20"
        if self.workflow:
            query += f"&workflow_id={urllib.parse.quote(self.workflow)}"

        payload = self._api(query)
        runs = payload.get("workflow_runs") or []
        if not runs:
            raise RetrievalError(
                f"No workflow run found for commit '{commit}'. "
                "Check the SHA and that the workflow has run for that commit."
            )

        # Prefer a completed run; fall back to the newest run of any status.
        completed = [r for r in runs if r.get("status") == "completed"]
        chosen = (completed or runs)[0]
        return str(chosen["id"])

    def find_artifact(self, run_id: str, artifact_name: str) -> ArtifactRef:
        payload = self._api(f"/repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts")
        artifacts = payload.get("artifacts") or []

        matches = [a for a in artifacts if a.get("name") == artifact_name]
        if not matches:
            available = ", ".join(sorted({a.get("name", "?") for a in artifacts})) or "none"
            raise RetrievalError(
                f"Artifact '{artifact_name}' not found on run {run_id}. "
                f"Available artifacts: {available}."
            )

        # Newest first: a re-run can leave several artifacts with the same name.
        matches.sort(key=lambda a: a.get("created_at") or "", reverse=True)
        chosen = matches[0]

        return ArtifactRef(
            run_id=str(run_id),
            name=chosen["name"],
            download_url=chosen.get("archive_download_url"),
            size_bytes=chosen.get("size_in_bytes"),
            expired=bool(chosen.get("expired")),
            metadata=chosen,
        )

    def download_artifact(self, ref: ArtifactRef, destination: Path) -> Path:
        if ref.expired:
            raise RetrievalError(
                f"Artifact '{ref.name}' on run {ref.run_id} has expired and can no longer "
                "be downloaded. Re-run the workflow to produce a fresh report."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        # `gh run download` resolves the artifact by name and writes the zip for us.
        self._gh(
            [
                "run",
                "download",
                ref.run_id,
                "--name",
                ref.name,
                "--dir",
                str(destination),
            ]
        )
        return destination


PROVIDERS: dict[str, type[CIProvider]] = {
    GitHubActionsProvider.name: GitHubActionsProvider,
}


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #


def default_namespace() -> Path:
    """Project-local cache directory, overridable via ``PLAYWRIGHT_REPORT_NAMESPACE``."""
    override = os.environ.get("PLAYWRIGHT_REPORT_NAMESPACE")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / DEFAULT_NAMESPACE


def prune_old_reports(namespace: Path, max_age_days: int, *, now: Optional[float] = None) -> list[Path]:
    """Delete entries in ``namespace`` older than ``max_age_days``.

    Returns the removed paths. Missing namespaces are a no-op.
    """
    if max_age_days <= 0 or not namespace.is_dir():
        return []

    cutoff = (now if now is not None else time.time()) - max_age_days * 86400
    removed: list[Path] = []

    for entry in namespace.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed.append(entry)
        except OSError:
            # A concurrent run may have removed it first; pruning is best-effort.
            continue

    return removed


def extract_zip(archive: Path, destination: Path) -> Path:
    """Extract ``archive`` into ``destination`` and return the report root.

    CI uploads the *contents* of ``playwright-report/``, so ``index.html`` is
    usually at the archive root. Some pipelines wrap it in a directory; both
    shapes are handled.
    """
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            # Guard against zip-slip: reject absolute paths and parent traversal.
            target = (destination / member).resolve()
            if not str(target).startswith(str(destination.resolve())):
                raise RetrievalError(f"Refusing to extract unsafe archive member: {member}")
        zf.extractall(destination)

    if (destination / "index.html").is_file():
        return destination

    for candidate in sorted(destination.rglob("index.html")):
        return candidate.parent

    raise RetrievalError(
        f"No index.html found in the extracted artifact at {destination}. "
        "The artifact may not be a Playwright HTML report."
    )


def build_report_url(index_html: Path, *, test: Optional[str] = None, test_id: Optional[str] = None) -> str:
    """Build a ``file://`` URL for the report, deep-linked when a filter is given."""
    url = index_html.resolve().as_uri()

    if test_id:
        return f"{url}#?testId={urllib.parse.quote(test_id, safe='')}"
    if test:
        return f"{url}#?q={urllib.parse.quote(test, safe='')}"
    return url


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    run_id: str
    report_dir: Path
    index_html: Path
    url: str
    artifact_name: str
    pruned: list[str] = field(default_factory=list)
    opened: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "report_dir": str(self.report_dir),
            "index_html": str(self.index_html),
            "url": self.url,
            "artifact_name": self.artifact_name,
            "pruned": self.pruned,
            "opened": self.opened,
        }


def _cached_result(
    namespace: Path,
    run_id: str,
    *,
    artifact_name: str,
    test: Optional[str],
    test_id: Optional[str],
    open_browser: bool,
    pruned: list[Path],
) -> Optional[FetchResult]:
    """Return a result for an already-extracted report, or None on a cache miss."""
    report_dir = namespace / run_id
    index_html = report_dir / "index.html"
    if not index_html.is_file():
        return None
    url = build_report_url(index_html, test=test, test_id=test_id)
    opened = webbrowser.open(url) if open_browser else False
    return FetchResult(
        run_id=run_id,
        report_dir=report_dir,
        index_html=index_html,
        url=url,
        artifact_name=artifact_name,
        pruned=[str(p) for p in pruned],
        opened=opened,
    )


def fetch_report(
    *,
    run_id: Optional[str] = None,
    commit: Optional[str] = None,
    test: Optional[str] = None,
    test_id: Optional[str] = None,
    artifact_name: str = DEFAULT_ARTIFACT_NAME,
    namespace: Optional[Path] = None,
    provider_name: str = GitHubActionsProvider.name,
    repo: Optional[str] = None,
    workflow: Optional[str] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    open_browser: bool = True,
    force: bool = False,
) -> FetchResult:
    """Download, extract and (optionally) open a Playwright report."""
    if not run_id and not commit:
        raise RetrievalError("Either run_id or commit is required.")

    namespace = namespace or default_namespace()
    namespace.mkdir(parents=True, exist_ok=True)
    pruned = prune_old_reports(namespace, max_age_days)

    # A cached report is served without touching the network, so retrieval keeps
    # working offline and after the CI artifact has expired.
    if run_id and not force:
        cached = _cached_result(
            namespace,
            str(run_id),
            artifact_name=artifact_name,
            test=test,
            test_id=test_id,
            open_browser=open_browser,
            pruned=pruned,
        )
        if cached is not None:
            return cached

    try:
        provider_cls = PROVIDERS[provider_name]
    except KeyError:
        raise RetrievalError(
            f"Unknown provider '{provider_name}'. Known providers: {', '.join(sorted(PROVIDERS))}."
        ) from None

    provider = provider_cls(repo=repo, workflow=workflow)  # type: ignore[call-arg]
    resolved_run_id = provider.resolve_run_id(run_id, commit)

    if not force:
        cached = _cached_result(
            namespace,
            resolved_run_id,
            artifact_name=artifact_name,
            test=test,
            test_id=test_id,
            open_browser=open_browser,
            pruned=pruned,
        )
        if cached is not None:
            return cached

    ref = provider.find_artifact(resolved_run_id, artifact_name)

    report_dir = namespace / resolved_run_id
    if report_dir.exists():
        shutil.rmtree(report_dir, ignore_errors=True)

    with tempfile.TemporaryDirectory(prefix="playwright-report-") as tmp:
        staging = Path(tmp) / "artifact"
        provider.download_artifact(ref, staging)

        archives = sorted(staging.rglob("*.zip"))
        if not archives:
            raise RetrievalError(
                f"The downloaded artifact '{artifact_name}' contained no .zip archive."
            )

        report_dir.mkdir(parents=True, exist_ok=True)
        index_html: Optional[Path] = None
        for archive in archives:
            root = extract_zip(archive, report_dir)
            index_html = index_html or (root / "index.html")

    if index_html is None or not index_html.is_file():
        raise RetrievalError(f"Extraction produced no index.html under {report_dir}.")

    url = build_report_url(index_html, test=test, test_id=test_id)
    opened = webbrowser.open(url) if open_browser else False

    return FetchResult(
        run_id=resolved_run_id,
        report_dir=report_dir,
        index_html=index_html,
        url=url,
        artifact_name=artifact_name,
        pruned=[str(p) for p in pruned],
        opened=opened,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_report.py",
        description="Download a Playwright HTML report from CI and open it deep-linked to a test.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id", help="CI run ID to fetch the report for.")
    target.add_argument("--commit", help="Commit SHA; the newest matching run is used.")

    parser.add_argument("--test", help="Test title to deep-link to (appended as #?q=<title>).")
    parser.add_argument("--test-id", help="Test ID to deep-link to (appended as #?testId=<id>).")
    parser.add_argument(
        "--artifact-name",
        default=DEFAULT_ARTIFACT_NAME,
        help=f"CI artifact name (default: {DEFAULT_ARTIFACT_NAME}).",
    )
    parser.add_argument(
        "--namespace",
        type=Path,
        default=None,
        help=f"Extraction root (default: ./{DEFAULT_NAMESPACE}).",
    )
    parser.add_argument(
        "--provider",
        default=GitHubActionsProvider.name,
        choices=sorted(PROVIDERS),
        help="CI provider implementation.",
    )
    parser.add_argument("--repo", help="owner/repo override (default: the current repository).")
    parser.add_argument("--workflow", help="Workflow file or name, used when resolving by commit.")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Purge cached reports older than this (default: {DEFAULT_MAX_AGE_DAYS}, 0 disables).",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not launch the browser.")
    parser.add_argument("--force", action="store_true", help="Re-download even if already cached.")
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = fetch_report(
            run_id=args.run_id,
            commit=args.commit,
            test=args.test,
            test_id=args.test_id,
            artifact_name=args.artifact_name,
            namespace=args.namespace,
            provider_name=args.provider,
            repo=args.repo,
            workflow=args.workflow,
            max_age_days=args.max_age_days,
            open_browser=not args.no_open,
            force=args.force,
        )
    except RetrievalError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"run:      {result.run_id}")
        print(f"report:   {result.report_dir}")
        print(f"url:      {result.url}")
        if result.pruned:
            print(f"pruned:   {len(result.pruned)} stale report(s)")
        if not result.opened and not args.no_open:
            print("note:     the browser could not be launched automatically")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
