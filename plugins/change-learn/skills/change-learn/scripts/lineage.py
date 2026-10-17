#!/usr/bin/env python3
"""change-learn lineage analyzer.

Token-efficient git change-window analysis. Emits compact JSON designed to be
read directly by an agent (no post-processing needed). Supports commit ranges,
staged changes, unstaged working-tree changes, untracked files, and any
combination of those. Behavior is tuned by an optional repo-root
`.change-learn.json` config file.

Commands:
  summary   Compact overview of a change window (default command).
  diff      Write per-file patches to temp files; print a JSON manifest.
  config    Print the resolved repo config.

State selection flags (summary/diff):
  --from REF      Base ref. Default: merge-base of HEAD with the default branch
                  (origin/HEAD), falling back to HEAD~1.
  --to REF        Head ref. Default: HEAD.
  --staged        Include staged (index vs HEAD) changes.
  --working       Include unstaged working-tree changes.
  --untracked     Include untracked files.
  --no-commits    Skip the commit-range comparison (use with the three flags
                  above to analyze only uncommitted state).

Examples:
  lineage.py --repo . summary
  lineage.py --repo . summary --from main --to HEAD --staged --untracked
  lineage.py --repo . summary --no-commits --staged --working --untracked
  lineage.py --repo . diff --from main src/app.py src/util.py
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

CONFIG_NAME = ".change-learn.json"
MAX_FLAGS = 40
FLAG_PATTERNS = [
    ("todo", re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")),
    ("debug", re.compile(r"(console\.(log|debug)|print\(|Console\.WriteLine|dbg!|binding\.pry|debugger\b)")),
    ("any-type", re.compile(r":\s*any\b")),
]

LANG_MAP = {
    ".cs": "csharp", ".py": "python", ".js": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "tsx", ".jsx": "jsx", ".razor": "razor",
    ".md": "markdown", ".json": "json", ".yml": "yaml", ".yaml": "yaml",
    ".sh": "shell", ".css": "css", ".html": "html", ".csproj": "msbuild",
    ".props": "msbuild", ".targets": "msbuild", ".sql": "sql", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby", ".toml": "toml",
}


def run_git(repo, args, check=True):
    proc = subprocess.run(
        ["git", "-C", repo, "--no-pager"] + args,
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(repo):
    return run_git(repo, ["rev-parse", "--show-toplevel"]).strip()


def load_config(root):
    path = os.path.join(root, CONFIG_NAME)
    cfg = {
        "focus": [],          # globs; when set, non-matching files are demoted to the appendix count
        "ignore": [],         # globs; matching files are excluded entirely
        "areas": {},          # {"Area Name": "glob"} mapping; else top-level dir is used
        "context_files": [],  # repo-root docs the agent should read for narrative context
        "highlight": [],      # globs; matching files are always surfaced first
        "deck": {},           # {"theme": ..., "audiences": [...]} hints for deck.py / the agent
        "max_files": 200,
        "max_commits": 30,
    }
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                for k in cfg:
                    if k in user:
                        cfg[k] = user[k]
        except (json.JSONDecodeError, OSError) as e:
            cfg["_config_error"] = f"{CONFIG_NAME}: {e}"
    cfg["_path"] = path if os.path.isfile(path) else None
    return cfg


def matches_any(path, globs):
    return any(
        fnmatch.fnmatch(path, g) or fnmatch.fnmatch(os.path.basename(path), g)
        for g in globs
    )


def default_base(repo):
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        try:
            name = run_git(repo, ["rev-parse", "--abbrev-ref", ref]).strip() if ref == "origin/HEAD" else ref
            run_git(repo, ["rev-parse", "--verify", "--quiet", name + "^{commit}"])
            base = run_git(repo, ["merge-base", name, "HEAD"]).strip()
            if base:
                return base
        except RuntimeError:
            continue
    try:
        return run_git(repo, ["rev-parse", "HEAD~1"]).strip()
    except RuntimeError:
        return None


def lang_of(path):
    _, ext = os.path.splitext(path)
    return LANG_MAP.get(ext.lower(), ext.lstrip(".").lower() or "none")


def parse_numstat_z(raw):
    """Parse `git diff --numstat -z` output (handles renames)."""
    files = []
    toks = raw.split("\0")
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok:
            i += 1
            continue
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        adds, dels, path = parts[0], parts[1], parts[2]
        entry = {
            "a": None if adds == "-" else int(adds),
            "d": None if dels == "-" else int(dels),
        }
        if path == "":  # rename: old and new paths follow as separate tokens
            entry["from"] = toks[i + 1]
            entry["p"] = toks[i + 2]
            i += 3
        else:
            entry["p"] = path
            i += 1
        files.append(entry)
    return files


def parse_status_z(raw):
    """Parse `git diff --name-status -z` output into {path: status}."""
    status = {}
    toks = raw.split("\0")
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok:
            i += 1
            continue
        code = tok[0]
        if code in ("R", "C"):
            status[toks[i + 2]] = code
            i += 3
        else:
            status[toks[i + 1]] = code
            i += 2
    return status


def collect_diff(repo, diff_args, label, cfg):
    numstat = parse_numstat_z(run_git(repo, ["diff", "--numstat", "-z"] + diff_args))
    statuses = parse_status_z(run_git(repo, ["diff", "--name-status", "-z", "-M"] + diff_args))
    out = []
    for f in numstat:
        if matches_any(f["p"], cfg["ignore"]):
            continue
        f["st"] = statuses.get(f["p"], "M")
        f["lang"] = lang_of(f["p"])
        f["src"] = label
        out.append(f)
    return out


def collect_flags(repo, diff_args, cfg):
    """Scan added lines for TODO/debug/any-type flags. Token-capped."""
    flags = []
    raw = run_git(repo, ["diff", "--unified=0"] + diff_args, check=False)
    current = None
    lineno = 0
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) - 1 if m else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lineno += 1
            if current and not matches_any(current, cfg["ignore"]):
                text = line[1:]
                for kind, pat in FLAG_PATTERNS:
                    if pat.search(text):
                        flags.append({"p": current, "line": lineno, "kind": kind,
                                      "txt": text.strip()[:120]})
                        break
            if len(flags) >= MAX_FLAGS:
                return flags
    return flags


def area_of(path, cfg):
    for name, glob in cfg["areas"].items():
        if matches_any(path, [glob] if isinstance(glob, str) else glob):
            return name
    top = path.split("/", 1)[0]
    return top if "/" in path else "(root)"


def build_summary(repo, args, cfg):
    root = repo_root(repo)
    spec = {"repo": root}
    files = []
    commits = []

    if not args.no_commits:
        base = args.ref_from or default_base(repo)
        head = args.ref_to or "HEAD"
        if base:
            spec["from"] = run_git(repo, ["rev-parse", "--short", base]).strip()
            spec["to"] = run_git(repo, ["rev-parse", "--short", head]).strip()
            rng = [f"{base}..{head}"]
            files += collect_diff(repo, rng, "commits", cfg)
            log = run_git(repo, ["log", "--format=%h%x00%an%x00%s", f"{base}..{head}"], check=False)
            for ln in log.splitlines():
                parts = ln.split("\0")
                if len(parts) == 3:
                    commits.append({"sha": parts[0], "author": parts[1], "s": parts[2][:100]})
            commits = commits[: cfg["max_commits"]]
        else:
            spec["from"] = None

    if args.staged:
        spec["staged"] = True
        files += collect_diff(repo, ["--cached"], "staged", cfg)
    if args.working:
        spec["working"] = True
        files += collect_diff(repo, [], "working", cfg)

    untracked = []
    if args.untracked:
        spec["untracked"] = True
        raw = run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
        for p in raw.split("\0"):
            if not p or matches_any(p, cfg["ignore"]):
                continue
            lines = None
            full = os.path.join(root, p)
            try:
                if os.path.getsize(full) < 1_000_000:
                    with open(full, "rb") as fh:
                        lines = fh.read().count(b"\n")
            except OSError:
                pass
            untracked.append({"p": p, "lines": lines, "lang": lang_of(p)})

    # Dedupe (same path may appear in commits+staged etc.); keep first, merge sources.
    seen = {}
    for f in files:
        key = f["p"]
        if key in seen:
            prev = seen[key]
            prev["src"] = prev["src"] + "+" + f["src"] if f["src"] not in prev["src"] else prev["src"]
            prev["a"] = (prev["a"] or 0) + (f["a"] or 0)
            prev["d"] = (prev["d"] or 0) + (f["d"] or 0)
        else:
            seen[key] = f
    files = list(seen.values())

    # Focus demotion + highlight promotion.
    if cfg["focus"]:
        for f in files:
            f["focus"] = matches_any(f["p"], cfg["focus"])
    for f in files:
        if cfg["highlight"] and matches_any(f["p"], cfg["highlight"]):
            f["hl"] = True

    files.sort(key=lambda f: (-(f.get("hl") and 1 or 0),
                              -(f.get("focus", True) and 1 or 0),
                              -((f["a"] or 0) + (f["d"] or 0))))

    areas = {}
    for f in files:
        a = area_of(f["p"], cfg)
        slot = areas.setdefault(a, {"area": a, "files": 0, "adds": 0, "dels": 0, "sample": []})
        slot["files"] += 1
        slot["adds"] += f["a"] or 0
        slot["dels"] += f["d"] or 0
        if len(slot["sample"]) < 3:
            slot["sample"].append(f["p"])
    for u in untracked:
        a = area_of(u["p"], cfg)
        slot = areas.setdefault(a, {"area": a, "files": 0, "adds": 0, "dels": 0, "sample": []})
        slot["files"] += 1
        slot["adds"] += u["lines"] or 0
        if len(slot["sample"]) < 3:
            slot["sample"].append(u["p"])

    flag_args = []
    if not args.no_commits and spec.get("from"):
        flag_args = [f"{spec['from']}..{spec['to']}"]
    elif args.staged:
        flag_args = ["--cached"]
    flags = collect_flags(repo, flag_args, cfg) if flag_args else []

    truncated = len(files) > cfg["max_files"]
    context_files = [c for c in cfg["context_files"] if os.path.isfile(os.path.join(root, c))]

    return {
        "spec": spec,
        "totals": {
            "files": len(files) + len(untracked),
            "adds": sum(f["a"] or 0 for f in files),
            "dels": sum(f["d"] or 0 for f in files),
            "commits": len(commits),
        },
        "commits": commits,
        "areas": sorted(areas.values(), key=lambda a: -(a["adds"] + a["dels"])),
        "files": files[: cfg["max_files"]],
        "files_truncated": truncated,
        "untracked": untracked,
        "flags": flags,
        "config": {
            "present": cfg["_path"] is not None,
            "context_files": context_files,
            "deck": cfg["deck"],
            **({"error": cfg["_config_error"]} if "_config_error" in cfg else {}),
        },
    }


def cmd_diff(repo, args, cfg):
    """Write per-file patches to temp files; print a manifest with hunk headers."""
    diff_args = []
    if args.staged and args.no_commits:
        diff_args = ["--cached"]
    elif not args.no_commits:
        base = args.ref_from or default_base(repo)
        head = args.ref_to or "HEAD"
        diff_args = [f"{base}..{head}"]
    tmpdir = tempfile.mkdtemp(prefix="change-learn-")
    manifest = []
    for path in args.paths:
        raw = run_git(repo, ["diff"] + diff_args + ["--", path], check=False)
        out = os.path.join(tmpdir, re.sub(r"[^A-Za-z0-9._-]", "_", path) + ".patch")
        with open(out, "w", encoding="utf-8") as f:
            f.write(raw)
        hunks = [ln for ln in raw.splitlines() if ln.startswith("@@")][:20]
        manifest.append({"p": path, "patch": out, "hunks": hunks,
                         "bytes": len(raw.encode())})
    print(json.dumps({"tmpdir": tmpdir, "files": manifest}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="change-learn lineage analyzer")
    ap.add_argument("--repo", default=".", help="path inside the target git repo")
    sub = ap.add_subparsers(dest="cmd")

    def add_state_flags(p):
        p.add_argument("--from", dest="ref_from")
        p.add_argument("--to", dest="ref_to")
        p.add_argument("--staged", action="store_true")
        p.add_argument("--working", action="store_true")
        p.add_argument("--untracked", action="store_true")
        p.add_argument("--no-commits", action="store_true")

    ps = sub.add_parser("summary")
    add_state_flags(ps)
    pd = sub.add_parser("diff")
    add_state_flags(pd)
    pd.add_argument("paths", nargs="+")
    sub.add_parser("config")

    args = ap.parse_args()
    if not args.cmd:
        args.cmd = "summary"
        for a in ("ref_from", "ref_to"):
            setattr(args, a, None)
        for a in ("staged", "working", "untracked", "no_commits"):
            setattr(args, a, False)

    repo = args.repo
    try:
        root = repo_root(repo)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    cfg = load_config(root)

    if args.cmd == "config":
        printable = {k: v for k, v in cfg.items() if not k.startswith("_")}
        printable["path"] = cfg["_path"]
        print(json.dumps(printable, indent=2, ensure_ascii=False))
    elif args.cmd == "diff":
        cmd_diff(repo, args, cfg)
    else:
        print(json.dumps(build_summary(repo, args, cfg),
                         ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
