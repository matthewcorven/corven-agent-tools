---
name: change-learn
description: >-
  AI-visualizes and AI-summarizes the changes between two source-control
  states — git commits, staged files, untracked files, or any combination —
  as Mermaid/PlantUML diagrams and Marp slide decks, narrated high-level
  first and then progressively lower, with optional PDF export.
  Token-efficient lineage analysis is driven by a repo-root
  .change-learn.json config. USE FOR: explain what changed in this branch,
  visualize my diff as slides, summarize changes between two commits,
  present staged plus untracked work as a deck, export a change review to
  PDF, teach me this changeset. DO NOT USE FOR: performing the code review
  itself (use code-review or branch-diff), authoring general-purpose
  presentations unrelated to source control, or modifying the changes being
  described.
---

# Change-Learn — visualize and narrate a change window

Turn any source-control change window into a narrated, diagram-rich Marp
deck. Always describe the change **top-down** — executive intent first,
then per-area design, then per-file detail — never the reverse.

## Precedence rules

1. **Helper scripts first.** Never run raw `git diff`/`git log` for analysis
   this skill covers. The bundled scripts emit compact JSON designed to be
   read directly — do not post-process their output with jq/python/grep.
2. **Config is authoritative.** If the target repo has a root
   `.change-learn.json`, its `focus`, `ignore`, `areas`, `highlight`, and
   `context_files` settings shape every analysis. Read each listed
   `context_files` document before writing any narrative.
3. **Use the resilient local path by default.** Run the bundled scripts for
   analysis, config, diffs, and deck construction. The `change-learn` MCP
   host is an optional enhancement for healthy sessions — use its tools
   (`analyze_changes`, `generate_deck`, `summarize_changes`,
   `export_deck_pdf`) only when they have completed successfully in the
   current session. Do not retry a generic MCP invocation failure; continue
   with the local scripts and report that the fallback was used.
4. **Mermaid by default, PlantUML on request.** Both are plain-text; emit
   PlantUML only when the user asks for it or the repo config sets
   `"deck": {"diagrams": "plantuml"}`.

## Workflow

### Step 1 — analyze the change window (token-efficient)

```sh
python3 scripts/lineage.py --repo <repo-root> summary \
  [--from <ref>] [--to <ref>] [--staged] [--working] [--untracked] [--no-commits]
```

- Defaults compare the merge-base with the default branch to `HEAD`.
- Combine flags freely — e.g. commits + staged + untracked in one window.
- `--no-commits --staged --working --untracked` analyzes only uncommitted state.
- Output is compact JSON — totals, commits, per-area churn, ranked files
  (config highlights first), untracked files, and flagged added lines
  (TODO/debug/`any`). Save it for Step 3:

```sh
python3 scripts/lineage.py --repo <root> summary ... > /tmp/cl-analysis.json
```

When you need actual patch content for a handful of files, use diff mode
(writes temp `.patch` files, prints a manifest — read the patch files
directly with your file reader):

```sh
python3 scripts/lineage.py --repo <root> diff --from <ref> <path1> <path2>
```

### Step 2 — read context

- `python3 scripts/lineage.py --repo <root> config` shows the resolved config.
- Read every existing file in the analysis JSON's `config.context_files`.
- Read the top 3–6 patches only for files you will narrate in depth.

### Step 3 — scaffold the deck

```sh
python3 scripts/deck.py scaffold --analysis /tmp/cl-analysis.json \
  -o change-deck.md --title "<deck title>" [--theme <marp theme>]
```

The scaffold embeds Mermaid `gitGraph` and `pie` diagrams plus
`<!-- ai-fill: ... -->` markers. **You must replace every ai-fill marker**
with real narrative, following the marker's instructions. Keep the
high-to-low ordering intact. See
[references/deck-authoring.md](references/deck-authoring.md) for slide
style, extra diagram patterns (flowchart, sequence, PlantUML), and the
softaworks/agent-toolkit `marp-slide` conventions this skill follows.

### Step 4 — export (optional)

```sh
python3 scripts/deck.py export --deck change-deck.md -o change-deck.pdf
```

PDF export pre-renders Mermaid blocks to SVG via `npx @mermaid-js/mermaid-cli`,
then runs `npx @marp-team/marp-cli --pdf`. HTML export (`--format html`)
keeps Mermaid blocks as-is. Requires Node.js; report a clear message if
`npx` is unavailable instead of hand-rolling an exporter.

## Repo-root config — `.change-learn.json`

```json
{
  "focus": ["src/**", "hosts/**"],
  "ignore": ["**/package-lock.json", "**/*.min.js", "dist/**"],
  "areas": {"Host": "hosts/**", "Plugin": "plugins/**", "Docs": "docs/**"},
  "highlight": ["**/Program.cs", "**/plugin.json"],
  "context_files": ["README.md", "docs/ARCHITECTURE.md"],
  "deck": {"theme": "gradient", "diagrams": "mermaid"},
  "max_files": 200,
  "max_commits": 30
}
```

All keys optional. `ignore` removes files from analysis entirely; `focus`
demotes non-matching files; `highlight` surfaces matches first; `areas`
names the groupings used for pie charts and per-area slides;
`context_files` lists docs the agent must read before narrating.

## Consumer-project guidance

When installed in a consumer project, suggest committing a
`.change-learn.json` tuned to that repo's layout, and point reviewers at
the generated deck rather than the raw diff for orientation. The deck is a
learning artifact — optimize for a reader seeing the change for the first
time.

## Canvas range selection

The Change Learn canvas includes **From commit** and **To commit** selectors.
Each option includes the full commit identity behind the selected value,
short SHA, subject, local branch/ref decorations, and GitHub pull-request
metadata when `gh pr list` can retrieve it. Select an ancestor **From**
commit and later **To** commit, then choose **Analyze span**. This executes
the bundled `lineage.py` locally through the canvas, so it remains available
when the App cannot dispatch MCP tool results.
