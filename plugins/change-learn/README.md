# change-learn

AI-visualize and AI-summarize the changes between two source-control states — git
commits, staged files, the working tree, untracked files, or any combination — as
**Mermaid diagrams** and **Marp slide decks** (plain-text syntax throughout), with
optional **PDF export**. Decks narrate the change at a high level first, then
progressively lower: title → executive summary → area breakdown → commit story →
per-area detail → risk flags.

This is an [Agent Plugins 1.0](https://agent-plugins.org/) plugin with two layers:

1. **A portable skill** (`skills/change-learn/`) — works in any Agent Skills
   client with just `python3` and `git`. Token-efficient scripts do the heavy
   lifting; the agent reads compact JSON and fills in the narrative.
2. **An MCP host** (`hosts/ChangeLearn.Host/` at the repo root) — a Blazor Server
   app exposing the same capabilities as 8 MCP tools, with a Squad agent team on
   the GitHub Copilot SDK + Microsoft Agent Framework for AI-written summaries
   and an append-only decision ledger.

## The skill (portable layer)

| Script | Purpose |
| --- | --- |
| `scripts/lineage.py` | Token-efficient change analysis: `summary` (compact JSON: totals, commits, areas, per-file stats, risk flags), `diff` (per-file patches written to temp files with a manifest), `config` (resolved `.change-learn.json`) |
| `scripts/deck.py` | `scaffold` builds a Marp deck from an analysis JSON — frontmatter, Mermaid `pie`/`gitGraph` blocks, and `<!-- ai-fill -->` markers the agent replaces with narrative; `export` renders PDF/HTML via `mermaid-cli` + `marp-cli` |

Change window selection composes freely:

```shell
python3 scripts/lineage.py --repo . summary --from main --to HEAD     # commit range
python3 scripts/lineage.py --repo . summary --staged                  # staged only
python3 scripts/lineage.py --repo . summary --working --untracked --no-commits
```

### Repo-root config: `.change-learn.json`

Drop this at the analyzed repo's root to steer every analysis:

```json
{
  "focus": ["src/**"],
  "ignore": ["**/*.lock"],
  "areas": { "API": ["src/api/**"], "UI": ["src/ui/**"] },
  "context_files": ["ARCHITECTURE.md"],
  "highlight": ["TODO", "HACK"],
  "deck": { "theme": "gaia", "title": "Sprint review" },
  "max_files": 200,
  "max_commits": 30
}
```

`context_files` are surfaced in the analysis so the agent can ground its
narrative in your architecture notes without re-discovering them.

### PDF export

`deck.py export --deck deck.md --format pdf` pre-renders Mermaid blocks with
`@mermaid-js/mermaid-cli`, then runs `@marp-team/marp-cli --pdf` (both fetched
via `npx` on first use; requires Node.js).

## The MCP host (full architecture)

`hosts/ChangeLearn.Host` is a .NET 10 app with two transports:

- **stdio (default for harnesses)** — `mcp.json` points at
  `scripts/mcp-stdio.sh`, so Copilot CLI / Copilot app start the server
  **on demand** per session and stop it when the session ends. The launcher
  builds the host incrementally (output to stderr) and execs
  `ChangeLearn.Host.dll --stdio`; tools analyze the repo the harness was
  launched in.
- **streamable HTTP + Blazor UI (manual)** — `dotnet run` serves the status
  page and an MCP endpoint at `http://localhost:5190/mcp`.

```text
Blazor Server (status page, :5190)
└── MCP endpoint (/mcp, ModelContextProtocol.AspNetCore)
    ├── GitLineageService  ──wraps──▶ lineage.py
    ├── DeckService        ──wraps──▶ deck.py
    ├── ChangeSummarizer   ──▶ Squad team (GitHub Copilot SDK + Agent Framework)
    │                          └─ deterministic fallback when the CLI is unavailable
    └── DecisionLedger     ──▶ team/.squad/decisions.md (append-only)
```

| MCP tool | Purpose |
| --- | --- |
| `analyze_changes` | Compact lineage JSON for any change window |
| `get_file_diffs` | Per-file patches as temp files + manifest (token-efficient) |
| `get_change_config` | Resolved `.change-learn.json` |
| `summarize_changes` | AI summary via the Squad team, deterministic fallback otherwise |
| `generate_deck` | Scaffold a Marp deck (requires `outputPath`) |
| `export_deck_pdf` | Render the deck to PDF/HTML |
| `record_decision` / `list_decisions` | Squad append-only decision ledger |

Run it manually (UI + HTTP mode):

```shell
cd hosts/ChangeLearn.Host
dotnet run          # listens on http://localhost:5190
```

Stdio mode is normally launched by the harness, but can be probed directly:

```shell
plugins/change-learn/scripts/mcp-stdio.sh   # speaks MCP on stdin/stdout
```

Configuration (`appsettings.json`, all optional — blank values fall back to
defaults): `ChangeLearn:RepoPath` (default: discovered git root),
`ChangeLearn:SkillScriptsPath`, `ChangeLearn:PythonPath`,
`ChangeLearn:SquadFolderPath` (the `.squad` directory itself).

### Squad agent team

`team/.squad/` defines the team per [Brady Gaster's Squad pattern](https://devblogs.microsoft.com/agent-framework/building-agent-teams-with-agent-framework-github-copilot-cli-and-squad/):
a charter, per-agent instruction files (narrator, cartographer, historian), and
`decisions.md` — the declarative, deterministic harness layer that manages the
team's charter, decisions, and history. `ChangeSummarizer` drives the team
through the GitHub Copilot SDK via Microsoft Agent Framework.

> **Known caveat:** the Copilot SDK launches the locally installed Copilot CLI;
> if that CLI predates the SDK's protocol (symptom:
> `copilot-runtime: unsupported argument '--allow-all'`), `summarize_changes`
> logs a warning and returns the deterministic summary instead. Update the
> Copilot CLI to restore AI summaries — every tool keeps working either way.

## Install

```shell
copilot plugin marketplace add matthewcorven/corven-agent-tools
copilot plugin install change-learn@corven-agent-tools
```

The skill works immediately. The MCP server starts **on demand**: the harness
runs `scripts/mcp-stdio.sh`, which needs the .NET 10 SDK plus a local clone of
this repo to supply the host. The launcher finds the host via (in order):

1. `CHANGE_LEARN_HOST_DIR` — `export CHANGE_LEARN_HOST_DIR=<clone>/hosts/ChangeLearn.Host`
2. the source checkout itself, when the plugin runs from this repo
3. `~/.config/change-learn/host-dir` — a file containing the host path:
   `mkdir -p ~/.config/change-learn && echo <clone>/hosts/ChangeLearn.Host > ~/.config/change-learn/host-dir`

Set `CHANGE_LEARN_REPO` to analyze a repo other than the harness's working
directory.

## Tests

```shell
python3 -m unittest discover -s plugins/change-learn/skills/change-learn/tests -v
```

## License

MIT
