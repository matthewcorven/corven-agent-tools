#!/usr/bin/env python3
"""Worktree-safe visual & E2E artifact manager and warn-only budget checker.

Canonical source: ``playwright-report-retrieval`` in ``matthewcorven/corven-agent-tools``.
Consuming repositories vendor this file into their own ``scripts/`` directory and
stamp it with ``python3 scripts/artifacts.py stamp``; ``--check-parity`` then fails
CI when the vendored body drifts from its declared ``VENDORED_SHA256``.

Stores generated local artifacts outside the git tree in a sibling directory
(<main-repo>-artifacts/) shared across all worktrees of the same clone:

    <repo>-artifacts/
      runs.jsonl
      latest -> wt-<basename>-<sha8>/runs/<utc>-<rand6>
      refs/issue-<N>/<utc>-<rand6> -> ../../wt-<basename>-<sha8>/runs/<utc>-<rand6>
      wt-<basename>-<sha8>/
        manifest.json
        latest -> runs/<utc>-<rand6>
        runs/<utc>-<rand6>/
          planning/  mockups/  dev/  debug/  qa/

Override the artifact root with ``E2E_ARTIFACTS_ROOT`` (``VDS_ARTIFACTS_ROOT`` is
accepted as a legacy alias for repositories that predate this file).

Zero third-party dependencies (Python 3.10+ stdlib only).
Budget checks are strictly warn-only (always exit 0).
"""
# vendored-from: playwright-report-retrieval@corven-agent-tools v0.3.0 sha=8fb253c45f2d
# VENDORED_SHA256: d0d8f3af34e585dc9e16e4abf822165ce5bc215b834ee7606c11e496efcd0ae8


from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PHASES = ("planning", "mockups", "dev", "debug", "qa")

# Provenance stamp for copies vendored into consuming repositories. CI runners and
# .githooks/ have no ~/.copilot/installed-plugins/, so those repositories carry a
# parity-checked copy of this file instead of calling the installed one.
PLUGIN_NAME = "playwright-report-retrieval"
PLUGIN_MARKETPLACE = "corven-agent-tools"
PLUGIN_VERSION = "0.3.0"
PROVENANCE_SOURCE = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE}"
PROVENANCE_HASH_KEY = "VENDORED_SHA256"
PROVENANCE_FROM_KEY = "vendored-from"
# The canonical copy is not a vendored copy, so it records this sentinel instead of
# a commit sha. Consuming repositories must record a real sha.
CANONICAL_COMMIT_SENTINEL = "canonical"
_COMMIT_RE = re.compile(r"^(?:canonical|[0-9a-f]{7,40})$")
_PROVENANCE_LINE_RE = re.compile(r"^# (?:vendored-from|VENDORED_SHA256):")
_PROVENANCE_VALUE_RE = re.compile(
    rf"^{re.escape(PROVENANCE_SOURCE)}\s+v(?P<version>\S+)\s+sha=(?P<commit>\S+)\s*$"
)

DEFAULT_BUDGET = {
    "schemaVersion": 1,
    "local": {
        "maxGb": 2.0,
        "warnAtPercent": 80.0,
        "hookThrottleSeconds": 300,
    },
    "ci": {
        "maxMbPerRun": 500.0,
        "warnAtPercent": 80.0,
        "captureKinds": ["start", "fail", "checkpoint"],
    },
}
ISSUE_BRANCH_RE = re.compile(
    r"(?:^|/)(?:squad|copilot|issue|fix|feat|feature|chore|docs)/(\d+)(?:[-_/]|$)",
    re.IGNORECASE,
)
RUN_ID_UTC_RE = re.compile(r"^(\d{8}T\d{6}Z)-[0-9a-f]+$")
DURATION_RE = re.compile(r"^(\d+)([dhm])$", re.IGNORECASE)


def _git(repo_path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_path}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _git_optional(repo_path: Path, *args: str, default: str = "") -> str:
    try:
        return _git(repo_path, *args)
    except RuntimeError:
        return default


def _sanitize_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return cleaned or "repo"


def _normalize_issue(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lstrip("#")
    if not cleaned:
        return None
    return _sanitize_slug(cleaned)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_symlink(link_path: Path, target: str) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = link_path.parent / f".{link_path.name}.{os.getpid()}.{secrets.token_hex(4)}.lnk"
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link_path)
    except OSError:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink(missing_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        os.symlink(target, link_path)


def resolve_repo_context(repo_path: str | Path | None = None) -> dict[str, Any]:
    start = Path(repo_path or os.getcwd()).resolve()
    toplevel_raw = _git(start, "rev-parse", "--show-toplevel")
    worktree_root = Path(os.path.realpath(toplevel_raw))

    try:
        common_raw = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except RuntimeError:
        rel_common = _git(start, "rev-parse", "--git-common-dir")
        common_raw = str((worktree_root / rel_common).resolve())
    common_dir = Path(os.path.realpath(common_raw))

    if common_dir.name == ".git":
        main_repo_root = common_dir.parent
    else:
        main_repo_root = common_dir

    override_root = os.environ.get("E2E_ARTIFACTS_ROOT") or os.environ.get(
        "VDS_ARTIFACTS_ROOT"
    )
    if override_root:
        artifact_root = Path(os.path.realpath(override_root))
    else:
        artifact_root = main_repo_root.parent / f"{main_repo_root.name}-artifacts"

    sha8 = hashlib.sha256(str(worktree_root).encode("utf-8")).hexdigest()[:8]
    wt_id = f"wt-{_sanitize_slug(worktree_root.name)}-{sha8}"

    branch = _git_optional(worktree_root, "rev-parse", "--abbrev-ref", "HEAD", default="HEAD")
    commit = _git_optional(worktree_root, "rev-parse", "--short=12", "HEAD", default="unknown")
    status = _git_optional(worktree_root, "status", "--porcelain", default="")
    dirty = bool(status.strip())

    return {
        "repo": main_repo_root.name,
        "mainRepoPath": str(main_repo_root),
        "worktreePath": str(worktree_root),
        "worktreeId": wt_id,
        "artifactRoot": str(artifact_root),
        "branch": branch,
        "commit": commit,
        "dirty": dirty,
    }


def load_budget(budget_file: str | Path | None, worktree_root: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    if budget_file:
        candidates.append(Path(budget_file))
    candidates.append(worktree_root / "artifacts.budget.json")

    merged = json.loads(json.dumps(DEFAULT_BUDGET))
    for path in candidates:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("local"), dict):
                merged["local"].update(data["local"])
            if isinstance(data.get("ci"), dict):
                merged["ci"].update(data["ci"])
            merged["_source"] = str(path)
            return merged
    merged["_source"] = "defaults"
    return merged


def _infer_issue_from_branch(branch: str) -> str | None:
    match = ISSUE_BRANCH_RE.search(branch or "")
    return match.group(1) if match else None


def _dir_stats(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) without following symlinks."""
    if not path.exists():
        return (0, 0)
    if path.is_symlink():
        return (0, 0)
    if path.is_file():
        try:
            return (1, path.lstat().st_size)
        except OSError:
            return (0, 0)
    files = 0
    total = 0
    for root, dirs, filenames in os.walk(path, followlinks=False):
        # Prune symlinked directories so symlinks are never traversed or double-counted.
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in filenames:
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
                if not os.path.islink(fp):
                    files += 1
                    total += st.st_size
            except OSError:
                continue
    return (files, total)


def _read_runs_jsonl(index_path: Path) -> list[dict[str, Any]]:
    if not index_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in index_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            continue
    return records


def _write_runs_jsonl(index_path: Path, records: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    _atomic_write_text(index_path, payload)


def _append_runs_jsonl(index_path: Path, record: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def create_run(
    repo_path: str | Path | None = None,
    phase: str = "qa",
    issue: str | None = None,
    spec: str | None = None,
    label: str | None = None,
    budget_file: str | Path | None = None,
    check_budget: bool = True,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"Invalid phase '{phase}'; expected one of {', '.join(PHASES)}")

    ctx = resolve_repo_context(repo_path)
    artifact_root = Path(ctx["artifactRoot"])
    wt_dir = artifact_root / ctx["worktreeId"]
    runs_dir = wt_dir / "runs"

    now = _utc_now()
    now_iso = _utc_iso(now)
    utc_stamp = now.strftime("%Y%m%dT%H%M%SZ")

    # Ensure uniqueness even if two calls happen within the same second.
    run_id = ""
    run_dir = Path()
    for _ in range(10):
        candidate_id = f"{utc_stamp}-{secrets.token_hex(3)}"
        candidate_dir = runs_dir / candidate_id
        try:
            candidate_dir.mkdir(parents=True, exist_ok=False)
            run_id = candidate_id
            run_dir = candidate_dir
            break
        except FileExistsError:
            continue
    if not run_id:
        raise RuntimeError("Failed to allocate unique artifact run directory")

    for sub in PHASES:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    resolved_issue = _normalize_issue(issue) or _infer_issue_from_branch(ctx["branch"])

    manifest_path = wt_dir / "manifest.json"
    first_seen = now_iso
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            first_seen = existing.get("firstSeen") or now_iso
        except json.JSONDecodeError:
            pass

    manifest = {
        "repo": ctx["repo"],
        "mainRepoPath": ctx["mainRepoPath"],
        "worktreeId": ctx["worktreeId"],
        "worktreePath": ctx["worktreePath"],
        "branch": ctx["branch"],
        "commit": ctx["commit"],
        "firstSeen": first_seen,
        "lastUsed": now_iso,
    }
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")

    # Layer 3: latest pointers (worktree + root)
    _atomic_symlink(wt_dir / "latest", f"runs/{run_id}")
    _atomic_symlink(artifact_root / "latest", f"{ctx['worktreeId']}/runs/{run_id}")

    # Layer 4: refs/issue-<N>/<run_id> symlink
    ref_link: str | None = None
    if resolved_issue:
        link_path = artifact_root / "refs" / f"issue-{resolved_issue}" / run_id
        _atomic_symlink(link_path, f"../../{ctx['worktreeId']}/runs/{run_id}")
        ref_link = str(link_path)

    phase_dir = run_dir / phase
    record = {
        "run": run_id,
        "utc": now_iso,
        "issue": resolved_issue,
        "worktreeId": ctx["worktreeId"],
        "worktreePath": ctx["worktreePath"],
        "branch": ctx["branch"],
        "commit": ctx["commit"],
        "dirty": ctx["dirty"],
        "phase": phase,
        "spec": spec,
        "label": label,
        "status": "active",
        "files": 0,
        "bytes": 0,
        "path": str(run_dir),
        "phasePath": str(phase_dir),
        "refLink": ref_link,
    }

    # Layer 2: append-only index
    _append_runs_jsonl(artifact_root / "runs.jsonl", record)

    budget_report: dict[str, Any] | None = None
    if check_budget:
        budget_report = compute_usage(
            repo_path=ctx["worktreePath"],
            budget_file=budget_file,
            throttle=False,
        )
        record["budgetWarning"] = budget_report.get("warningMessage")

    return record


def record_meta(
    repo_path: str | Path | None = None,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
    status: str | None = None,
    spec: str | None = None,
    files: int | None = None,
    total_bytes: int | None = None,
) -> dict[str, Any]:
    ctx = resolve_repo_context(repo_path)
    artifact_root = Path(ctx["artifactRoot"])
    index_path = artifact_root / "runs.jsonl"

    target_path = Path(os.path.realpath(run_dir)) if run_dir else None
    target_id = run_id or (target_path.name if target_path else None)
    if not target_id:
        raise ValueError("Either --run-dir or --run must be provided")

    records = _read_runs_jsonl(index_path)
    matched: dict[str, Any] | None = None
    for rec in reversed(records):
        if rec.get("run") == target_id or (
            target_path and rec.get("path") == str(target_path)
        ):
            matched = rec
            break

    if matched is None:
        if target_path is None:
            raise FileNotFoundError(f"Run '{target_id}' not found in {index_path}")
        matched = {
            "run": target_id,
            "utc": _utc_iso(),
            "issue": _infer_issue_from_branch(ctx["branch"]),
            "worktreeId": ctx["worktreeId"],
            "worktreePath": ctx["worktreePath"],
            "branch": ctx["branch"],
            "commit": ctx["commit"],
            "dirty": ctx["dirty"],
            "phase": "qa",
            "spec": spec,
            "label": None,
            "status": status or "completed",
            "files": 0,
            "bytes": 0,
            "path": str(target_path),
            "phasePath": str(target_path / "qa"),
            "refLink": None,
        }
        records.append(matched)

    resolved_path = Path(matched["path"])
    if files is None or total_bytes is None:
        counted_files, counted_bytes = _dir_stats(resolved_path)
        matched["files"] = counted_files if files is None else files
        matched["bytes"] = counted_bytes if total_bytes is None else total_bytes
    else:
        matched["files"] = files
        matched["bytes"] = total_bytes

    if status:
        matched["status"] = status
    if spec:
        matched["spec"] = spec
    matched["updatedUtc"] = _utc_iso()

    _write_runs_jsonl(index_path, records)
    return matched


def find_runs(
    repo_path: str | Path | None = None,
    issue: str | None = None,
    commit: str | None = None,
    branch: str | None = None,
    spec: str | None = None,
    phase: str | None = None,
    worktree: str | None = None,
    status: str | None = None,
    latest: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    ctx = resolve_repo_context(repo_path)
    artifact_root = Path(ctx["artifactRoot"])
    records = _read_runs_jsonl(artifact_root / "runs.jsonl")

    norm_issue = _normalize_issue(issue)
    results: list[dict[str, Any]] = []
    for rec in records:
        if norm_issue and str(rec.get("issue") or "") != norm_issue:
            continue
        if commit and not str(rec.get("commit") or "").startswith(commit):
            continue
        if branch and branch.lower() not in str(rec.get("branch") or "").lower():
            continue
        if spec and spec.lower() not in str(rec.get("spec") or "").lower():
            continue
        if phase and str(rec.get("phase") or "") != phase:
            continue
        if worktree and worktree.lower() not in str(rec.get("worktreeId") or "").lower():
            continue
        if status and str(rec.get("status") or "") != status:
            continue
        results.append(rec)

    if latest:
        return results[-1:] if results else []
    if limit is not None and limit > 0:
        return results[-limit:]
    return results


def compute_usage(
    repo_path: str | Path | None = None,
    budget_file: str | Path | None = None,
    throttle: bool = False,
) -> dict[str, Any]:
    ctx = resolve_repo_context(repo_path)
    artifact_root = Path(ctx["artifactRoot"])
    worktree_root = Path(ctx["worktreePath"])
    budget = load_budget(budget_file, worktree_root)

    local_cfg = budget["local"]
    max_gb = float(local_cfg.get("maxGb", 2.0))
    warn_pct = float(local_cfg.get("warnAtPercent", 80.0))
    throttle_sec = int(local_cfg.get("hookThrottleSeconds", 300))
    max_bytes = int(max_gb * (1024**3))

    stamp_path = artifact_root / ".budget-check.stamp"
    if throttle and stamp_path.is_file():
        age = time.time() - stamp_path.stat().st_mtime
        if age < throttle_sec:
            return {
                "skippedByThrottle": True,
                "artifactRoot": str(artifact_root),
                "totalBytes": 0,
                "maxBytes": max_bytes,
                "percentUsed": 0.0,
                "warn": False,
                "overLimit": False,
                "offenders": [],
                "warningMessage": None,
            }

    if not artifact_root.exists():
        return {
            "skippedByThrottle": False,
            "artifactRoot": str(artifact_root),
            "totalBytes": 0,
            "maxBytes": max_bytes,
            "percentUsed": 0.0,
            "warn": False,
            "overLimit": False,
            "offenders": [],
            "warningMessage": None,
        }

    total_files, total_bytes = _dir_stats(artifact_root)
    offenders: list[dict[str, Any]] = []
    for child in sorted(artifact_root.iterdir()):
        if not child.is_dir() or child.is_symlink() or not child.name.startswith("wt-"):
            continue
        wt_files, wt_bytes = _dir_stats(child)
        manifest_path = child / "manifest.json"
        last_used = ""
        branch = "unknown"
        wt_path = ""
        if manifest_path.is_file():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                last_used = str(m.get("lastUsed") or "")
                branch = str(m.get("branch") or "unknown")
                wt_path = str(m.get("worktreePath") or "")
            except json.JSONDecodeError:
                pass
        offenders.append(
            {
                "worktreeId": child.name,
                "branch": branch,
                "worktreePath": wt_path,
                "lastUsed": last_used,
                "files": wt_files,
                "bytes": wt_bytes,
                "mb": round(wt_bytes / (1024**2), 2),
            }
        )

    # Sort offenders: oldest lastUsed first (best pruning candidates), tie-break by largest size.
    offenders.sort(key=lambda o: (o["lastUsed"] or "9999", -o["bytes"]))

    if throttle:
        try:
            stamp_path.write_text(_utc_iso() + "\n", encoding="utf-8")
        except OSError:
            pass

    percent_used = (total_bytes / max_bytes * 100.0) if max_bytes > 0 else 0.0
    over_limit = total_bytes > max_bytes
    warn = percent_used >= warn_pct

    warning_msg: str | None = None
    if warn:
        used_gb = total_bytes / (1024**3)
        status_label = "EXCEEDED" if over_limit else "APPROACHING"
        top_lines = [
            f"  - {o['worktreeId']} (branch={o['branch']}, lastUsed={o['lastUsed'] or 'unknown'}, {o['mb']:.1f} MB)"
            for o in offenders[:5]
        ]
        offender_block = "\n".join(top_lines) if top_lines else "  (no wt-* directories)"
        warning_msg = (
            f"WARNING: Local artifact budget {status_label}: "
            f"{used_gb:.2f} GB / {max_gb:.2f} GB ({percent_used:.1f}%) in {artifact_root}\n"
            f"Oldest worktree artifact directories (sorted by manifest.json lastUsed):\n"
            f"{offender_block}\n"
            f"Action: review and reclaim space with:\n"
            f"  python3 scripts/artifacts.py prune --older-than 7d --dry-run"
        )

    return {
        "skippedByThrottle": False,
        "artifactRoot": str(artifact_root),
        "budgetSource": budget.get("_source"),
        "totalFiles": total_files,
        "totalBytes": total_bytes,
        "maxBytes": max_bytes,
        "maxGb": max_gb,
        "warnAtPercent": warn_pct,
        "percentUsed": round(percent_used, 2),
        "warn": warn,
        "overLimit": over_limit,
        "offenders": offenders,
        "warningMessage": warning_msg,
    }


def _parse_duration(spec: str) -> timedelta:
    match = DURATION_RE.match(spec.strip())
    if not match:
        raise ValueError(f"Invalid duration '{spec}'; expected e.g. 7d, 24h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _run_timestamp(run_dir: Path) -> datetime:
    match = RUN_ID_UTC_RE.match(run_dir.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)


def _clean_dangling_symlinks(artifact_root: Path) -> int:
    removed = 0
    for root, dirs, filenames in os.walk(artifact_root, followlinks=False):
        for entry in list(dirs) + list(filenames):
            fp = Path(root) / entry
            if fp.is_symlink() and not fp.exists():
                fp.unlink(missing_ok=True)
                removed += 1
    refs_dir = artifact_root / "refs"
    if refs_dir.is_dir():
        for child in list(refs_dir.iterdir()):
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()
    return removed


def prune_runs(
    repo_path: str | Path | None = None,
    older_than: str = "14d",
    confirm: bool = False,
) -> dict[str, Any]:
    ctx = resolve_repo_context(repo_path)
    artifact_root = Path(ctx["artifactRoot"])
    delta = _parse_duration(older_than)
    cutoff = _utc_now() - delta

    candidates: list[dict[str, Any]] = []
    if artifact_root.is_dir():
        for wt_dir in sorted(artifact_root.iterdir()):
            if not wt_dir.is_dir() or wt_dir.is_symlink() or not wt_dir.name.startswith("wt-"):
                continue
            runs_dir = wt_dir / "runs"
            if not runs_dir.is_dir():
                continue
            for run_dir in sorted(runs_dir.iterdir()):
                if not run_dir.is_dir() or run_dir.is_symlink():
                    continue
                ts = _run_timestamp(run_dir)
                if ts <= cutoff:
                    files, nbytes = _dir_stats(run_dir)
                    candidates.append(
                        {
                            "run": run_dir.name,
                            "worktreeId": wt_dir.name,
                            "utc": _utc_iso(ts),
                            "files": files,
                            "bytes": nbytes,
                            "path": str(run_dir),
                        }
                    )

    reclaim_bytes = sum(c["bytes"] for c in candidates)
    deleted = False
    dangling_removed = 0

    if confirm and candidates:
        pruned_ids = set()
        for c in candidates:
            shutil.rmtree(c["path"], ignore_errors=True)
            pruned_ids.add(c["run"])
        index_path = artifact_root / "runs.jsonl"
        if index_path.is_file():
            kept = [r for r in _read_runs_jsonl(index_path) if r.get("run") not in pruned_ids]
            _write_runs_jsonl(index_path, kept)
        dangling_removed = _clean_dangling_symlinks(artifact_root)
        deleted = True

    return {
        "artifactRoot": str(artifact_root),
        "olderThan": older_than,
        "cutoffUtc": _utc_iso(cutoff),
        "dryRun": not confirm,
        "deleted": deleted,
        "candidateCount": len(candidates),
        "reclaimBytes": reclaim_bytes,
        "danglingSymlinksRemoved": dangling_removed,
        "candidates": candidates,
    }


def check_ci_budget(
    paths: list[str],
    budget_file: str | Path | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    worktree_root = Path(repo_path or os.getcwd()).resolve()
    try:
        worktree_root = Path(os.path.realpath(_git(worktree_root, "rev-parse", "--show-toplevel")))
    except RuntimeError:
        pass

    budget = load_budget(budget_file, worktree_root)
    ci_cfg = budget["ci"]
    max_mb = float(ci_cfg.get("maxMbPerRun", 500.0))
    warn_pct = float(ci_cfg.get("warnAtPercent", 80.0))
    max_bytes = int(max_mb * (1024**2))

    breakdown: list[dict[str, Any]] = []
    total_bytes = 0
    total_files = 0
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            breakdown.append({"path": raw, "exists": False, "files": 0, "bytes": 0})
            continue
        files, nbytes = _dir_stats(p)
        total_files += files
        total_bytes += nbytes
        breakdown.append({"path": raw, "exists": True, "files": files, "bytes": nbytes})

    total_mb = total_bytes / (1024**2)
    percent_used = (total_bytes / max_bytes * 100.0) if max_bytes > 0 else 0.0
    over_limit = total_bytes > max_bytes
    warn = percent_used >= warn_pct

    annotation: str | None = None
    if warn:
        label = "exceeded" if over_limit else "approaching"
        annotation = (
            f"::warning title=CI Artifact Budget::"
            f"CI artifacts {label} per-run budget: {total_mb:.2f} MB / {max_mb:.0f} MB "
            f"({percent_used:.1f}%). Verify only start/fail/checkpoint visuals are attached in CI."
        )

    return {
        "budgetSource": budget.get("_source"),
        "totalFiles": total_files,
        "totalBytes": total_bytes,
        "totalMb": round(total_mb, 2),
        "maxMbPerRun": max_mb,
        "warnAtPercent": warn_pct,
        "percentUsed": round(percent_used, 2),
        "warn": warn,
        "overLimit": over_limit,
        "breakdown": breakdown,
        "annotation": annotation,
    }


def _is_provenance_line(line: str) -> bool:
    return bool(_PROVENANCE_LINE_RE.match(line))


def _body_lines(text: str) -> list[str]:
    """Return ``text``'s lines with the provenance stamp block removed."""
    return [line for line in text.splitlines() if not _is_provenance_line(line)]


def body_hash(text: str) -> str:
    """Return the SHA-256 of ``text`` with provenance stamp lines removed.

    The body hash is what makes vendoring verifiable. A comment stamp alone can
    never fail a build, so a consuming repo's copy could drift from the canonical
    file silently. Stripping the stamp lines first means re-stamping (or
    refreshing the recorded commit) never changes the hash, while any edit to
    real code does.
    """
    return hashlib.sha256("\n".join(_body_lines(text)).encode("utf-8")).hexdigest()


def parse_provenance(text: str) -> dict[str, str | None]:
    """Return the declared ``version``/``commit``/``sha256`` stamp of ``text``."""
    found: dict[str, str | None] = {"version": None, "commit": None, "sha256": None}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"# {PROVENANCE_HASH_KEY}:"):
            found["sha256"] = stripped.split(":", 1)[1].strip() or None
        elif stripped.startswith(f"# {PROVENANCE_FROM_KEY}:"):
            value = stripped.split(":", 1)[1].strip()
            match = _PROVENANCE_VALUE_RE.match(value)
            if match:
                found["version"] = match.group("version")
                found["commit"] = match.group("commit")
            else:
                # A bare ``vX.Y.Z`` cannot distinguish pre-merge from post-merge
                # during a version bump, so surface it as an unusable stamp.
                bare = re.match(rf"^{re.escape(PROVENANCE_SOURCE)}\s+v(?P<version>\S+)\s*$", value)
                if bare:
                    found["version"] = bare.group("version")
    return found


def stamp_text(text: str, *, version: str = PLUGIN_VERSION, commit: str = "unknown") -> str:
    """Return ``text`` with a refreshed provenance stamp block.

    Any previous stamp lines are dropped so the file can be re-stamped after a
    plugin version bump without accumulating stale lines.
    """
    body_lines = _body_lines(text)
    digest = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()
    stamp = [
        f"# {PROVENANCE_FROM_KEY}: {PROVENANCE_SOURCE} v{version} sha={commit}",
        f"# {PROVENANCE_HASH_KEY}: {digest}",
    ]
    # Keep the shebang (and any encoding cookie) first; insert the stamp after the
    # module docstring so the docstring stays the module docstring.
    insert_at = 0
    if body_lines and body_lines[0].startswith("#!"):
        insert_at = 1

    try:
        tree = ast.parse("\n".join(body_lines))
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
            getattr(tree.body[0], "value", None), ast.Constant
        ):
            insert_at = max(insert_at, int(tree.body[0].end_lineno or insert_at))
    except SyntaxError:
        pass

    stamped = [*body_lines[:insert_at], *stamp, *body_lines[insert_at:]]
    return "\n".join(stamped) + "\n"


def check_parity(
    path: str | Path | None = None,
    *,
    text: str | None = None,
    canonical: str | Path | None = None,
) -> dict[str, Any]:
    """Compare a vendored copy's body hash against its declared ``VENDORED_SHA256``.

    ``path`` is the copy under test; ``canonical`` is the authoritative file it
    claims to be vendored from (defaults to this file). Comparing against the
    canonical body, not just against the copy's own recorded hash, is what makes
    drift detectable in either direction: editing the vendored body and editing
    the canonical file both fail.

    Returns ``{"parity": True|False, ...}``. A missing or unparseable stamp is a
    parity failure — a vendored copy with no verifiable provenance is exactly the
    silent-drift case this check exists to catch.
    """
    source = Path(path) if path else Path(__file__).resolve()
    if text is None:
        text = source.read_text(encoding="utf-8")

    actual = body_hash(text)
    provenance = parse_provenance(text)
    declared = provenance["sha256"]
    canonical_version = PLUGIN_VERSION

    problems: list[str] = []
    if not provenance["version"]:
        problems.append("missing '# vendored-from:' provenance stamp")
    elif provenance["version"] != canonical_version:
        problems.append(
            f"vendored from v{provenance['version']} but the canonical plugin is v{canonical_version}"
        )
    if not provenance["commit"]:
        problems.append("provenance stamp has no plugin commit sha (bare vX.Y.Z cannot distinguish pre/post-merge)")
    elif not _COMMIT_RE.match(provenance["commit"]):
        problems.append(
            f"provenance commit '{provenance['commit']}' is neither 'canonical' nor a 7-40 char hex sha"
        )
    if not declared:
        problems.append("missing '# VENDORED_SHA256:' body hash")
    elif declared != actual:
        problems.append(f"body hash mismatch (declared {declared}, actual {actual})")

    canonical_body: str | None = None
    if canonical is not None or source.resolve() != Path(__file__).resolve():
        canonical_path = Path(canonical) if canonical else Path(__file__).resolve()
        if canonical_path.is_file():
            canonical_body = body_hash(canonical_path.read_text(encoding="utf-8"))
            if actual != canonical_body:
                problems.append(
                    "vendored body differs from the canonical file "
                    f"(canonical {canonical_body[:12]}, vendored {actual[:12]})"
                )

    return {
        "path": str(source),
        "parity": not problems,
        "canonicalVersion": canonical_version,
        "declaredVersion": provenance["version"],
        "declaredCommit": provenance["commit"],
        "declaredSha256": declared,
        "actualSha256": actual,
        "canonicalSha256": canonical_body,
        "problems": problems,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artifacts.py",
        description="Worktree-safe visual/E2E artifact manager and warn-only budget checker.",
    )
    parser.add_argument("--repo-path", default=None, help="Path inside target git repo/worktree")
    sub = parser.add_subparsers(dest="command", required=True)

    p_root = sub.add_parser("root", help="Print sibling artifact root path")
    p_root.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="Create a new collision-free artifact run directory")
    p_run.add_argument("--phase", choices=PHASES, default="qa")
    p_run.add_argument("--issue", default=None, help="Issue or work-item identifier (e.g. 349)")
    p_run.add_argument("--spec", default=None, help="Spec or workflow name")
    p_run.add_argument("--label", default=None, help="Optional freeform tag")
    p_run.add_argument("--budget-file", default=None)
    p_run.add_argument("--no-budget-check", action="store_true")
    p_run.add_argument("--json", action="store_true")

    p_meta = sub.add_parser("record-meta", help="Update run metadata (status/files/bytes) in runs.jsonl")
    p_meta.add_argument("--run-dir", default=None)
    p_meta.add_argument("--run", dest="run_id", default=None)
    p_meta.add_argument("--status", default=None)
    p_meta.add_argument("--spec", default=None)
    p_meta.add_argument("--files", type=int, default=None)
    p_meta.add_argument("--bytes", dest="total_bytes", type=int, default=None)
    p_meta.add_argument("--json", action="store_true")

    p_find = sub.add_parser("find", help="Search runs.jsonl by issue, commit, branch, spec, or latest")
    p_find.add_argument("--issue", default=None)
    p_find.add_argument("--commit", default=None)
    p_find.add_argument("--branch", default=None)
    p_find.add_argument("--spec", default=None)
    p_find.add_argument("--phase", choices=PHASES, default=None)
    p_find.add_argument("--worktree", default=None)
    p_find.add_argument("--status", default=None)
    p_find.add_argument("--latest", action="store_true")
    p_find.add_argument("--limit", type=int, default=None)
    p_find.add_argument("--json", action="store_true")

    p_usage = sub.add_parser("usage", help="Check local artifact disk usage against budget (warn-only)")
    p_usage.add_argument("--budget-file", default=None)
    p_usage.add_argument("--throttle", action="store_true")
    p_usage.add_argument("--verbose", action="store_true")
    p_usage.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune", help="List or remove runs older than a duration (dry-run by default)")
    p_prune.add_argument("--older-than", default="14d", help="Age threshold, e.g. 7d, 24h, 30m")
    p_prune.add_argument("--dry-run", action="store_true", help="Explicitly force dry-run (default)")
    p_prune.add_argument("--confirm", action="store_true", help="Actually delete matching runs")
    p_prune.add_argument("--json", action="store_true")

    p_ci = sub.add_parser("check-ci-budget", help="Check per-run CI artifact bytes (warn-only)")
    p_ci.add_argument("--budget-file", default=None)
    p_ci.add_argument("--json", action="store_true")
    p_ci.add_argument("paths", nargs="+", help="Artifact directories or files to measure")

    p_stamp = sub.add_parser(
        "stamp",
        help="Write the vendored-from + VENDORED_SHA256 provenance stamp into a copy of this file",
    )
    p_stamp.add_argument(
        "path",
        nargs="?",
        default=None,
        help="File to stamp in place (default: this file; consuming repos pass scripts/artifacts.py)",
    )
    p_stamp.add_argument("--version", default=PLUGIN_VERSION, help="Plugin version to record")
    p_stamp.add_argument(
        "--commit",
        default=None,
        help="Canonical plugin commit sha (default: git HEAD of this file, else 'unknown')",
    )
    p_stamp.add_argument("--json", action="store_true")

    p_parity = sub.add_parser(
        "check-parity",
        help="Fail when a vendored copy's body hash or stamp disagrees with VENDORED_SHA256",
    )
    p_parity.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Vendored copy to check (default: this file)",
    )
    p_parity.add_argument("--json", action="store_true")
    p_parity.add_argument(
        "--canonical",
        default=None,
        help="Canonical artifacts.py to compare against (default: this file)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "root":
        ctx = resolve_repo_context(args.repo_path)
        if args.json:
            print(json.dumps(ctx, indent=2))
        else:
            print(ctx["artifactRoot"])
        return 0

    if args.command == "run":
        rec = create_run(
            repo_path=args.repo_path,
            phase=args.phase,
            issue=args.issue,
            spec=args.spec,
            label=args.label,
            budget_file=args.budget_file,
            check_budget=not args.no_budget_check,
        )
        if args.json:
            print(json.dumps(rec, indent=2))
        else:
            print(
                f"ARTIFACT run={rec['run']} issue={rec['issue'] or 'none'} "
                f"phase={rec['phase']} spec={rec['spec'] or 'none'} path={rec['path']}"
            )
            if rec.get("budgetWarning"):
                print(rec["budgetWarning"])
        return 0

    if args.command == "record-meta":
        rec = record_meta(
            repo_path=args.repo_path,
            run_dir=args.run_dir,
            run_id=args.run_id,
            status=args.status,
            spec=args.spec,
            files=args.files,
            total_bytes=args.total_bytes,
        )
        if args.json:
            print(json.dumps(rec, indent=2))
        else:
            print(
                f"ARTIFACT_META run={rec['run']} status={rec['status']} "
                f"files={rec['files']} bytes={rec['bytes']} path={rec['path']}"
            )
        return 0

    if args.command == "find":
        rows = find_runs(
            repo_path=args.repo_path,
            issue=args.issue,
            commit=args.commit,
            branch=args.branch,
            spec=args.spec,
            phase=args.phase,
            worktree=args.worktree,
            status=args.status,
            latest=args.latest,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(
                    f"ARTIFACT run={r.get('run')} issue={r.get('issue') or 'none'} "
                    f"branch={r.get('branch') or 'unknown'} commit={r.get('commit') or 'unknown'} "
                    f"phase={r.get('phase') or 'qa'} spec={r.get('spec') or 'none'} "
                    f"status={r.get('status') or 'unknown'} path={r.get('path')}"
                )
        return 0

    if args.command == "usage":
        report = compute_usage(
            repo_path=args.repo_path,
            budget_file=args.budget_file,
            throttle=args.throttle,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        elif report.get("warningMessage"):
            print(report["warningMessage"])
        elif args.verbose and not report.get("skippedByThrottle"):
            used_mb = report["totalBytes"] / (1024**2)
            print(
                f"Local artifact usage: {used_mb:.2f} MB / {report['maxGb']:.2f} GB "
                f"({report['percentUsed']:.1f}%) in {report['artifactRoot']}"
            )
        return 0

    if args.command == "prune":
        do_confirm = bool(args.confirm and not args.dry_run)
        report = prune_runs(
            repo_path=args.repo_path,
            older_than=args.older_than,
            confirm=do_confirm,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            mode = "PRUNED" if report["deleted"] else "DRY-RUN"
            mb = report["reclaimBytes"] / (1024**2)
            print(
                f"[{mode}] {report['candidateCount']} run(s) older than {report['olderThan']} "
                f"({mb:.2f} MB) in {report['artifactRoot']}"
            )
            for c in report["candidates"]:
                print(f"  - {c['worktreeId']}/runs/{c['run']} ({c['utc']}, {c['bytes']} bytes)")
            if not report["deleted"] and report["candidateCount"] > 0:
                print(
                    f"Re-run with --confirm to delete: "
                    f"python3 scripts/artifacts.py prune --older-than {report['olderThan']} --confirm"
                )
        return 0

    if args.command == "check-ci-budget":
        report = check_ci_budget(
            paths=args.paths,
            budget_file=args.budget_file,
            repo_path=args.repo_path,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"CI artifact usage: {report['totalMb']:.2f} MB / {report['maxMbPerRun']:.0f} MB "
                f"({report['percentUsed']:.1f}%, {report['totalFiles']} files)"
            )
            if report.get("annotation"):
                print(report["annotation"])
        return 0

    if args.command == "stamp":
        target = Path(args.path) if args.path else Path(__file__).resolve()
        commit = args.commit
        if not commit:
            commit = _git_optional(target.parent, "rev-parse", "--short=12", "HEAD", default="")
        if not commit:
            # This file is the canonical copy when it lives in the plugin tree; a
            # vendored copy outside a git checkout must record a real sha instead.
            canonical_here = Path(__file__).resolve() == target.resolve()
            commit = CANONICAL_COMMIT_SENTINEL if canonical_here else "unknown"
        stamped = stamp_text(
            target.read_text(encoding="utf-8"),
            version=args.version,
            commit=commit,
        )
        target.write_text(stamped, encoding="utf-8")
        payload = {
            "path": str(target),
            "version": args.version,
            "commit": commit,
            "sha256": body_hash(stamped),
        }
        print(json.dumps(payload, indent=2) if args.json else f"STAMPED {target} v{args.version} sha={commit}")
        return 0

    if args.command == "check-parity":
        report = check_parity(args.path, canonical=args.canonical)
        if args.json:
            print(json.dumps(report, indent=2))
        elif report["parity"]:
            print(f"PARITY OK {report['path']} v{report['declaredVersion']} sha256={report['actualSha256'][:12]}")
        else:
            print(f"PARITY FAIL {report['path']}")
            for problem in report["problems"]:
                print(f"  - {problem}")
        return 0 if report["parity"] else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
