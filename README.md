# corven-agent-tools

Portable **Agent Plugins 1.0** tooling for AI coding agents. This repository distributes a dependency-driven GitHub issue-tracking skill, a decision-model skill, a source-control change-visualization plugin, and a deterministic Squad model-resolver plugin, and is published as a plugin marketplace.

## What's inside

| Component | Location | Purpose |
| --- | --- | --- |
| `issue-work-queue` skill | `plugins/github-issues/skills/issue-work-queue/SKILL.md` | Portable rules for a dependency-driven work queue: issue bodies' `## Depends On` / `## Blocked By` sections are the source of truth, and **the queue helper takes precedence over raw `gh issue list`** |
| Queue helper | `plugins/github-issues/skills/issue-work-queue/scripts/issue_queue.py` | Computes the ready-vs-blocked queue (`queue` mode) and board counts (`status` mode) via the `gh` CLI |
| Unit tests | `plugins/github-issues/skills/issue-work-queue/tests/test_issue_queue.py` | Stdlib `unittest` suite, run in CI |
| `use-decision-model` skill | `plugins/use-decision-model/skills/use-decision-model/SKILL.md` | Guidance for using a **decision model as a tool**: when it is useful, how to craft the `state` and typed `noul`/`choice`/`score` questions, how to send them, and the response signature to expect |
| Sender | `plugins/use-decision-model/skills/use-decision-model/scripts/decision.py` | POSTs a `state` + `questions` payload to the decisions endpoint and prints the raw JSON response |
| Example payloads | `plugins/use-decision-model/skills/use-decision-model/examples/` | Ready-to-send `noul.json`, `choice.json`, `score.json` payloads, plus `borderline.json` (hedged `noul` case) and `multi.json` (all three types on one JSON-object state) |
| `change-learn` skill | `plugins/change-learn/skills/change-learn/SKILL.md` | AI-visualize and AI-summarize changes between two source-control states as Mermaid diagrams and Marp slide decks (PDF export), narrated high-level first then lower, driven by a repo-root `.change-learn.json` |
| Lineage + deck scripts | `plugins/change-learn/skills/change-learn/scripts/` | `lineage.py` (token-efficient change analysis as compact JSON) and `deck.py` (Marp deck scaffold with Mermaid blocks + `npx` PDF export) |
| Change-learn MCP host | `hosts/ChangeLearn.Host/` | Dual-transport MCP host (on-demand stdio from a configured source checkout; Blazor Server UI + `:5190/mcp` HTTP) exposing 8 tools, with a Squad agent team on the GitHub Copilot SDK + Microsoft Agent Framework — see [`plugins/change-learn/README.md`](plugins/change-learn/README.md) |
| `squad-model-resolver` plugin | `plugins/squad-model-resolver/` | Deterministic model ID resolution MCP server (`resolve_model`, `resolve_for_agent`, `verify_spawn`) + thin aggressive skill for Squad agent spawns |

Requirements: `python3` (stdlib only) and an authenticated [`gh` CLI](https://cli.github.com/) for the issue-queue skill. The decision-model sender needs `OPENROUTER_API_KEY` in the environment.

## Format and compatibility

Both plugins use the open Agent Plugins 1.0 format, compatible with multiple AI coding agents. Their `SKILL.md` skills are portable to clients that support Agent Plugins 1.0 and the Agent Skills format.

## Install with GitHub Copilot

Register the marketplace once, then install a plugin:

```shell
copilot plugin marketplace add matthewcorven/corven-agent-tools
copilot plugin install github-issues@corven-agent-tools
copilot plugin install use-decision-model@corven-agent-tools
copilot plugin install change-learn@corven-agent-tools
copilot plugin install squad-model-resolver@corven-agent-tools
```

The install source must be the qualified `plugin@marketplace` form — a bare `github-issues` is rejected with `Invalid plugin spec`.

You can also enable them declaratively in `.github/copilot/settings.json` (repository-level) or `~/.copilot/settings.json` (user-level). The `enabledPlugins` keys must be the qualified `plugin@corven-agent-tools` forms, and a non-default marketplace must also be registered under `extraKnownMarketplaces`:

```json
{
  "enabledPlugins": {
    "github-issues@corven-agent-tools": true,
    "use-decision-model@corven-agent-tools": true,
    "squad-model-resolver@corven-agent-tools": true
  },
  "extraKnownMarketplaces": {
    "corven-agent-tools": {
      "source": {
        "source": "github",
        "repo": "matthewcorven/corven-agent-tools"
      }
    }
  }
}
```

### Migration from `corven-copilot-tools`

After the GitHub repository is renamed, replace existing marketplace registrations and declarative settings with `corven-agent-tools` and `plugin@corven-agent-tools`. The plugin names remain `github-issues` and `use-decision-model`.

## Usage

The skills are discovered automatically.

For the issue queue, ask for queue status, "what's ready next", or "what's blocking X" — or invoke the helper directly. `<skill-dir>` is the directory containing the skill's `SKILL.md`:

```shell
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text --mode status
```

For a decision model, ask for a typed judgment (route a ticket, gate a yes/no fact, score quality) — or send a payload directly:

```shell
python3 <skill-dir>/scripts/decision.py --file examples/choice.json
echo '{"state": "...", "questions": {...}}' | python3 <skill-dir>/scripts/decision.py --stdin
```

## Issue-work-queue reference

### Flags

Flags: `--mode {queue,status}` · `--repo owner/repo` · `--state {all,open}` · `--label` (repeatable, default `squad`; a base label includes `<label>:*` members) · `--format {json,text,ascii,mermaid,markdown-mermaid}` · `--limit N` · `--project N` (optional board-drift diagnostic) · `--project-owner login`.

### Flow diagram formats

`--format ascii`, `--format mermaid`, and `--format markdown-mermaid` render the dependency graph as a flow diagram — tiered plain text for terminals, a bare Mermaid `flowchart` for any renderer, or a Markdown document with an embedded Mermaid block for GitHub/VS Code preview. Same queue computation as `text`/`json`; only the presentation changes.

```shell
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --format ascii
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --format mermaid
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --format markdown-mermaid
```

### The precedence rule

The issue-queue helper's output **must take precedence over raw `gh issue list`**. Agents must never infer readiness, ordering, or blockers from `gh issue list` or ad hoc label scans; raw GitHub queries are follow-up only, after the helper identifies the specific issue or PR to inspect.

### Board drift diagnostic

With `--project <number>` (GitHub Projects V2), the helper additionally **reports drift** between the board's `Status` field and queue-derived expectations — as a diagnostic for the receiving AI model to act on. It classifies each mismatch (`done_but_open`, `closed_not_done`, `work_started_board_stale`, plus informational kinds), flags open queue issues missing from the board, and never feeds back into readiness: the queue stays authoritative, the board is a view that can drift.

**The repo declares its board, the tool reads it.** Drop a `.github/issue-queue.json` at the repo root so agents never have to know the project number:

```json
{ "project": 3 }
```

When `--project` is omitted the helper discovers that file automatically (walking up from the current directory, stopping at the repo root). Precedence: explicit `--project` flag → repo config → no board section. Optional `"owner"` key overrides the default (repository owner); a malformed config surfaces as `board.error` without breaking the queue.

```shell
# equivalent — flag wins over config when both are present
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --project 3 --format text
```

## Authoring issues for the queue

```markdown
## Depends On
#123
456

## Blocked By
7.4
```

References resolve to issue numbers or `[Task X.Y]` ids parsed from issue titles. A reference is unsatisfied only when its referent is OPEN; CLOSED referents are satisfied; missing referents are flagged `(missing)`.

## Squad model-resolver reference

The `squad-model-resolver` plugin provides an MCP server and a thin skill that deterministically resolves model references into exact complete IDs for the harness `task` tool, and verifies spawned sessions.

### MCP tools

- `resolve_model`: Takes `reference`, `catalog` (list of model IDs exposed by the harness), and optional `provider_registry`. Resolves built-in or `provider://model-path` references against exact catalog matches.
- `resolve_for_agent`: Takes `repo_path`, `agent_name`, `catalog`, and optional `session_directive`. Reads `.squad/config.json` and the agent's charter `## Model` section, applying strict layer precedence (`agentModelOverrides` → session directive → charter → `defaultModel` → `auto`).
- `verify_spawn`: Takes `selected_model_id` and `reported_model_id` to verify match status post-spawn, noting provider provenance disclaimers.

### Provider registry file format

To resolve provider-qualified references (such as `openrouter://z-ai/glm-5.3` or `omlx://Qwen3.8-27B-4bit`), the server maps provider schemes to opaque ID prefixes independently using a user-level configuration file at `~/.config/squad-model-resolver/providers.json`:

```json
{
  "openrouter": "b311631e-d285-40e6-b0e3-2e813af887ed",
  "omlx": "6fecf2bb-2bbb-4e4e-bec9-a2ca2c4a9d44"
}
```

Prefix values can be either a string prefix (e.g. UUID) or an array of string prefixes. The user-level registry file is authoritative: calling agents cannot forge or overwrite entries in the user registry, and the resolver will never guess prefixes or infer providers from UUIDs.

## Repository layout

```text
corven-agent-tools/                         # Agent Plugins marketplace
├── .github/
│   ├── plugin/marketplace.json             # Marketplace registry
│   ├── workflows/                          # CI and repository automation
│   ├── agents/                              # Repository-level Copilot agents
│   └── skills/                              # Repository-level Copilot skills
├── hosts/ChangeLearn.Host/                 # .NET MCP host and Blazor UI
│   ├── Components/                         # Blazor UI
│   ├── Services/                            # Lineage, deck, summary, decision services
│   ├── Tools/                               # MCP tool registrations
│   └── team/.squad/                         # Host's runtime agent-team definitions
└── plugins/                                 # Independently installable plugins
    ├── change-learn/                        # change-learn@corven-agent-tools
    │   ├── plugin.json, mcp.json, README.md
    │   ├── com.github.copilot/extensions/   # Copilot App canvas
    │   ├── scripts/                         # MCP bridge and host launcher
    │   └── skills/change-learn/             # Portable skill, scripts, reference, tests
    ├── github-issues/                       # github-issues@corven-agent-tools
    │   ├── plugin.json
    │   └── skills/issue-work-queue/         # Skill, queue helper, tests
    ├── squad-model-resolver/                # squad-model-resolver@corven-agent-tools
    │   ├── plugin.json, mcp.json
    │   ├── server.py
    │   ├── skills/squad-model-resolver/     # Mandatory thin skill
    │   └── tests/                           # Unit tests + MCP smoke tests
    └── use-decision-model/                  # use-decision-model@corven-agent-tools
        ├── plugin.json
        └── skills/use-decision-model/       # Skill, sender, example payloads
```

## Development

```shell
python3 -m unittest discover -s plugins/github-issues/skills/issue-work-queue/tests -v
python3 -m unittest discover -s plugins/squad-model-resolver/tests -v
python3 -m unittest discover -s plugins/change-learn/skills/change-learn/tests -v
```

The change-learn MCP host builds with `dotnet build hosts/ChangeLearn.Host`.

## License

MIT
