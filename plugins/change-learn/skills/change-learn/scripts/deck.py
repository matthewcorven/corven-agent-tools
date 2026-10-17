#!/usr/bin/env python3
"""change-learn Marp deck tooling.

Commands:
  scaffold   Generate a Marp deck skeleton from a lineage.py summary JSON.
             The skeleton embeds Mermaid diagrams (gitGraph, pie) and
             `<!-- ai-fill: ... -->` markers telling the agent exactly which
             narrative sections to write, high level first, then lower.
  export     Export a finished deck to PDF (or HTML) via npx Marp CLI,
             pre-rendering Mermaid blocks to SVG via npx mermaid-cli first.

Examples:
  deck.py scaffold --analysis analysis.json -o deck.md --title "Sprint 12 changes"
  deck.py export --deck deck.md -o deck.pdf
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

MARP_HEADER = """---
marp: true
theme: {theme}
paginate: true
_paginate: false
---

"""

MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def esc(text):
    return str(text).replace("|", "\\|")


def git_graph(commits):
    lines = ["gitGraph"]
    for c in reversed(commits[:12]):
        subject = re.sub(r'[\'"\\]', "", c["s"])[:48]
        lines.append(f'  commit id: "{c["sha"]} {subject}"')
    return "\n".join(lines)


def area_pie(areas):
    lines = ["pie showData", '  title Churn by area (adds + dels)']
    for a in areas[:8]:
        churn = a["adds"] + a["dels"]
        if churn > 0:
            name = re.sub(r'"', "", a["area"])
            lines.append(f'  "{name}" : {churn}')
    return "\n".join(lines)


def file_table(files, limit=12):
    rows = ["| File | Status | + | - |", "|---|---|---:|---:|"]
    for f in files[:limit]:
        rows.append(f"| `{esc(f['p'])}` | {f.get('st','?')} | {f.get('a') or 0} | {f.get('d') or 0} |")
    return "\n".join(rows)


def scaffold(analysis, title, theme, repo_name):
    spec = analysis.get("spec", {})
    totals = analysis.get("totals", {})
    commits = analysis.get("commits", [])
    areas = analysis.get("areas", [])
    files = analysis.get("files", [])
    untracked = analysis.get("untracked", [])
    flags = analysis.get("flags", [])
    window = []
    if spec.get("from"):
        window.append(f"`{spec['from']}..{spec.get('to', 'HEAD')}`")
    if spec.get("staged"):
        window.append("staged")
    if spec.get("working"):
        window.append("working tree")
    if spec.get("untracked"):
        window.append("untracked")
    window_label = " + ".join(window) or "current change window"

    slides = [MARP_HEADER.format(theme=theme)]

    # 1. Title
    slides.append(f"""# {title}

**{repo_name}** — {window_label}

{totals.get('commits', 0)} commits · {totals.get('files', 0)} files · +{totals.get('adds', 0)} / -{totals.get('dels', 0)}

<!-- ai-fill: one-sentence subtitle capturing the overall intent of this change window -->
""")

    # 2. Executive summary (highest level)
    slides.append("""---

## The story at a glance

<!-- ai-fill: 3-5 bullet executive summary. Audience-neutral. What changed,
     why it matters, and what a reviewer should look at first. No file paths
     on this slide — keep it at the level of capabilities and intent. -->
""")

    # 3. Churn by area
    slides.append(f"""---

## Where the change landed

```mermaid
{area_pie(areas)}
```

<!-- ai-fill: one sentence per major area explaining its role in this change -->
""")

    # 4. Commit timeline
    if commits:
        slides.append(f"""---

## Commit timeline

```mermaid
{git_graph(commits)}
```

<!-- ai-fill: 1-2 sentences describing the arc of the work across these commits -->
""")

    # 5. Per-area slides (middle level)
    for a in areas[:6]:
        area_files = [f for f in files if f["p"].startswith(a["area"]) or a["area"] == "(root)"][:8]
        slides.append(f"""---

## Area — {a['area']}

{a['files']} files · +{a['adds']} / -{a['dels']}

{file_table(area_files, 8)}

<!-- ai-fill: what this area's change accomplishes, notable design decisions,
     and anything a reviewer must verify. Reference specific files. -->
""")

    # 6. Flags / risk
    if flags:
        rows = ["| File | Line | Kind | Text |", "|---|---:|---|---|"]
        for fl in flags[:10]:
            rows.append(f"| `{esc(fl['p'])}` | {fl['line']} | {fl['kind']} | {esc(fl['txt'][:60])} |")
        slides.append("""---

## Flags worth a look

""" + "\n".join(rows) + """

<!-- ai-fill: triage these flags — which are intentional, which need follow-up -->
""")

    # 7. New files
    if untracked:
        rows = ["| New file | Lines |", "|---|---:|"]
        for u in untracked[:12]:
            rows.append(f"| `{esc(u['p'])}` | {u.get('lines') or '?'} |")
        slides.append("""---

## Brand-new files (untracked)

""" + "\n".join(rows) + """

<!-- ai-fill: why these files exist and where they fit in the architecture -->
""")

    # 8. Appendix (lowest level)
    slides.append(f"""---

## Appendix — full file inventory

{file_table(files, 20)}

<!-- ai-fill: optional per-file one-liners for the files a reviewer will open -->
""")

    return "\n".join(slides)


def npx(cmd_args, cwd=None):
    npx_bin = shutil.which("npx")
    if not npx_bin:
        raise RuntimeError("npx not found — install Node.js to export decks")
    proc = subprocess.run([npx_bin, "-y"] + cmd_args, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"npx {cmd_args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def prerender_mermaid(deck_md, assets_dir):
    """Replace ```mermaid blocks with pre-rendered SVG images (for PDF export)."""
    os.makedirs(assets_dir, exist_ok=True)
    count = 0

    def replace(match):
        nonlocal count
        count += 1
        src = os.path.join(assets_dir, f"diagram-{count}.mmd")
        svg = os.path.join(assets_dir, f"diagram-{count}.svg")
        with open(src, "w", encoding="utf-8") as f:
            f.write(match.group(1))
        npx(["@mermaid-js/mermaid-cli", "-i", src, "-o", svg, "-b", "transparent"])
        rel = os.path.relpath(svg, os.path.dirname(assets_dir) or ".")
        return f"![diagram {count}]({rel})"

    return MERMAID_BLOCK.sub(replace, deck_md), count


def cmd_export(args):
    with open(args.deck, encoding="utf-8") as f:
        deck = f.read()
    deck_path = os.path.abspath(args.deck)
    deck_dir = os.path.dirname(deck_path)
    out = os.path.abspath(
        args.output or os.path.splitext(deck_path)[0] + (".pdf" if args.format == "pdf" else ".html"))

    source = deck_path
    if args.format == "pdf" and MERMAID_BLOCK.search(deck):
        assets = os.path.join(deck_dir, os.path.splitext(os.path.basename(args.deck))[0] + "_assets")
        rendered, n = prerender_mermaid(deck, assets)
        source = os.path.join(deck_dir, ".change-learn-render.md")
        with open(source, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"pre-rendered {n} mermaid diagram(s) -> {assets}", file=sys.stderr)

    marp_args = ["@marp-team/marp-cli", source, "--allow-local-files", "-o", out]
    if args.format == "pdf":
        marp_args.insert(2, "--pdf")
    npx(marp_args, cwd=deck_dir)
    if source != args.deck and not args.keep_intermediate:
        os.unlink(source)
    print(json.dumps({"output": out, "format": args.format}))


def main():
    ap = argparse.ArgumentParser(description="change-learn Marp deck tooling")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scaffold")
    ps.add_argument("--analysis", required=True, help="lineage.py summary JSON file")
    ps.add_argument("-o", "--output", required=True)
    ps.add_argument("--title", default="Change review")
    ps.add_argument("--theme", default="default")
    ps.add_argument("--repo-name", default=None)

    pe = sub.add_parser("export")
    pe.add_argument("--deck", required=True)
    pe.add_argument("-o", "--output", default=None)
    pe.add_argument("--format", choices=["pdf", "html"], default="pdf")
    pe.add_argument("--keep-intermediate", action="store_true")

    args = ap.parse_args()
    if args.cmd == "scaffold":
        with open(args.analysis, encoding="utf-8") as f:
            analysis = json.load(f)
        repo_name = args.repo_name or os.path.basename(
            analysis.get("spec", {}).get("repo", "repository"))
        deck = scaffold(analysis, args.title, args.theme, repo_name)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(deck)
        print(json.dumps({"output": args.output,
                          "slides": deck.count("\n---\n") + 1}))
    else:
        try:
            cmd_export(args)
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
