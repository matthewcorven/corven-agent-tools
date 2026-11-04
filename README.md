# corven-agent-tools

Portable **Agent Plugins 1.0** tooling for AI coding agents. This repository distributes a dependency-driven GitHub issue-tracking skill, a decision-model skill, a source-control change-visualization plugin, a deterministic Squad model-resolver plugin, a Playwright report-retrieval plugin, and a GitHub Copilot BYOK model-metadata plugin, and is published as a plugin marketplace.

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
| `playwright-report-retrieval` plugin | `plugins/playwright-report-retrieval/` | Canonical Playwright reporting + AI-driven E2E standards skill, plus a retrieval CLI and MCP server (`fetch_report`, `list_failed_tests`) that downloads a CI HTML report by run ID or commit and deep-links to a test |
| `copilot-byok-metadata` plugin | `plugins/copilot-byok-metadata/` | MCP server + thin skill that reads the GitHub Copilot app's BYOK models, fetches context-window and reasoning-effort metadata from provider sources of truth (OpenRouter and local oMLX servers), and applies it to the app config interactively or automatically |

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
copilot plugin install playwright-report-retrieval@corven-agent-tools
copilot plugin install copilot-byok-metadata@corven-agent-tools
```

The install source must be the qualified `plugin@marketplace` form — a bare `github-issues` is rejected with `Invalid plugin spec`.

### Upgrading a plugin that ships an MCP server

Several plugins in this marketplace (`copilot-byok-metadata`, `squad-model-resolver`, `playwright-report-retrieval`, `use-decision-model`, and the `change-learn` MCP host) start a long-lived MCP server process. **After an upgrade, that server process from the previous version may still be running.** An AI harness that already has it open keeps calling the old tools and will not see new tools, new tool arguments, or changed behavior.

Before concluding an upgrade did not take effect:

1. Shut down the existing MCP server instance — usually by restarting the AI harness session that owns it.
2. If the harness does not stop it (an orphaned `server.py`, or a `change-learn` host that outlived its session), force-close that process.
3. Reopen the harness and confirm the new version's tools are listed.

A newly launched harness session picks up the installed version automatically; only already-open sessions hold the stale process.

You can also enable them declaratively in `.github/copilot/settings.json` (repository-level) or `~/.copilot/settings.json` (user-level). The `enabledPlugins` keys must be the qualified `plugin@corven-agent-tools` forms, and a non-default marketplace must also be registered under `extraKnownMarketplaces`:

```json
{
  "enabledPlugins": {
    "github-issues@corven-agent-tools": true,
    "use-decision-model@corven-agent-tools": true,
    "squad-model-resolver@corven-agent-tools": true,
    "playwright-report-retrieval@corven-agent-tools": true,
    "copilot-byok-metadata@corven-agent-tools": true
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

## Playwright report retrieval reference

The `playwright-report-retrieval` plugin carries the canonical Playwright reporting and AI-driven E2E standards, plus the tooling to pull a CI HTML report back to a workstation.

### Standards (skill)

`skills/playwright-report-retrieval/SKILL.md` is the source of truth for: Playwright-only E2E automation, Red-Green-Refactor, the coverage gate (no UI change without happy/alternate/edge coverage), deterministic data-driven journeys with ephemeral state, `storageState()` forking for parallel alternate/edge paths, ephemeral local artifacts, and 90-day CI artifact retention with no commit-back.

### CLI

```shell
python3 plugins/playwright-report-retrieval/scripts/fetch_report.py --run-id 123456789
python3 plugins/playwright-report-retrieval/scripts/fetch_report.py --commit abc1234 --test "Login Flow"
python3 plugins/playwright-report-retrieval/scripts/fetch_report.py --run-id 123456789 --test-id 3f2a1b --no-open --json
```

The script resolves the run, locates the `playwright-report` artifact, prunes namespace entries older than 7 days, extracts to `./playwright-reports/<run-id>/`, appends the `#?q=` / `#?testId=` deep link, and opens the default browser. Cached reports are reused unless `--force` is passed.

### MCP tools

- `fetch_report`: Takes `run_id` or `commit`, plus optional `test`, `test_id`, `artifact_name`, `namespace`, `provider`, `repo`, `workflow`, `max_age_days`, `force`, `open_browser`. Returns the report directory, `index.html` path, and deep link.
- `list_failed_tests`: Takes `report_dir` (or the same run/commit selectors) and returns every test with its status, plus a `failed` subset.

### Provider seam

`GitHubActionsProvider` (backed by the `gh` CLI) is the only implementation today. Subclass `CIProvider`, implement `resolve_run_id`, `find_artifact`, and `download_artifact`, then register it in `PROVIDERS`.

## Copilot BYOK metadata reference

The `copilot-byok-metadata` plugin fills a gap in the GitHub Copilot desktop app: BYOK (bring-your-own-key) models are stored with `max_prompt_tokens`, `max_output_tokens`, and `supported_reasoning_efforts` all `NULL`, so the app cannot compact at the right point or offer reasoning levels for them. This plugin reads those models, fetches the real metadata from the provider's source of truth, and writes it into the app config.

Scope today: **GitHub Copilot desktop app only**, with **OpenRouter** and local **oMLX** servers as metadata providers. GitHub Copilot CLI support is planned for a future release if its configuration turns out to be separate.

### MCP tools

- `read_byok_config`: Returns a complete snapshot in one call — every provider (credentials redacted), every model with its stored metadata, the user's selected reasoning effort and context tier, the cached catalog entries, and which metadata fields are still missing. Takes an optional `workspace_path` to identify the calling project and workspace; BYOK configuration itself is app-global. `catalog_cache.byok_entries` surfaces each entry's `max_context_window_tokens`, `max_prompt_tokens`, `max_output_tokens`, and `has_limits`, and `catalog_cache.caches` reports both catalog caches with a `byok_entries_with_limits` count — so an agent can tell at a glance whether the cache patch actually landed.
- `list_byok_models`: Reads `~/.copilot/data.db` and returns every configured provider and BYOK model with its current metadata, plus counts of models with and without metadata.
- `fetch_provider_metadata`: Fetches each provider's source of truth — OpenRouter's public catalog, or a local oMLX server's `/v1/models` enriched by `/v1/models/status` — and returns normalized metadata per model. Read `derived` for the values that get written; `metadata` is the raw provider payload whose `max_output_tokens` is an unclamped aggregate maximum. Unreachable local servers are returned in `warnings` instead of failing the call.
- `plan_metadata_updates`: Computes a dry-run plan — per model, the current values, the proposed values, the changed fields, and a status of `update`, `unchanged`, `unresolved`, or `unsupported_provider`. Never writes.
- `apply_metadata_updates`: Applies the plan. Defaults to `dry_run: true`; a real write takes a database backup first and runs in a single transaction.
- `get_provider_registry`: Returns the provider registry (source-of-truth URLs, host/name match rules) and the supported targets.

### Metadata derivation

OpenRouter's `top_provider.max_completion_tokens` is an aggregate maximum, not a sane per-request output budget — several 1M-token models report `943718`, which would leave only ~100k of prompt budget and force compaction almost immediately. The server therefore clamps `max_output_tokens` to `min(provider value, 131072, context_window × 0.5)` and derives `max_prompt_tokens = context_window − max_output_tokens`, matching the app's own built-in catalog convention (output budgets never exceed 128000).

Reasoning efforts are intersected with the Copilot vocabulary (`low`, `medium`, `high`, `xhigh`, `max`), so OpenRouter's `minimal` and `none` are dropped. Models with no `reasoning` object get `NULL`, which the app reads as "no reasoning effort".

### oMLX provider

oMLX is a local inference server, so there is no shared catalog: each configured provider is queried at its own base URL.

- `GET <baseUrl>/v1/models` returns `id` and `max_model_len` — the effective context window, exposed as a vLLM-style extension.
- `GET <baseUrl>/v1/models/status` returns the same value as `max_context_window`, plus `model_context_length` (the discovered native length) and `max_tokens` (the per-model generation ceiling). The status value wins when both are available; if the endpoint is missing or fails, the `/v1/models` value is used instead.
- oMLX resolves the effective window as: per-model override → discovered native context (optionally clamped by the operator's `max_context_window_policy`) → the oMLX app default. `provider_metadata.context_source` records which tier supplied the value.
- oMLX accepts reasoning-effort strings but does not advertise a per-model level set, so **no reasoning efforts are derived for oMLX models** and an existing user selection is left untouched. `derives_reasoning_efforts` in `read_byok_config` reports this per provider.
- A provider is detected as oMLX when its name contains `omlx` and it has a base URL; the URL may be configured with or without the trailing `/v1`. Models on a server that is not running come back `unresolved`, and `plan_metadata_updates`/`apply_metadata_updates` report the failure in `warnings`. Start oMLX and re-run.
- oMLX's model list also contains non-chat models (OCR, rerankers, MarkItDown). They are planned like any other entry; entries that advertise no context window stay `unchanged` with a `context_source` of `unresolved`.

### Optional behaviors

- `set_reasoning_defaults`: Seeds `copilot-reasoning-effort` with the provider's default effort, but only for models with no existing user pick — an explicit choice is never overwritten.
- `set_context_tier`: Writes `copilot-context-tier` (`default` or `long_context`) for the affected models. Off by default.
- `refresh_catalog_cache`: Also patches the cached catalog entries so the model picker reflects the new limits without waiting for a refresh. **On by default; leave it on.** `provider_models` has no context-window column, so the cached catalogs are the only path by which the app learns a BYOK model's real window. Without it the model stays on the app's fallback window and compaction still fires early. Both `copilot-available-models-v2` (an object with a `models` list) and `copilot-cloud-available-models-v2` (a bare list) are patched; `applied.catalog_caches_patched` reports each one and `applied.catalog_cache_entries_patched` is the total. The server emits a `WARNING` note when a refresh was requested but matched no entries in either cache.

### Safety

Writes go to `~/.copilot/data.db` while the app may be running. The server uses `busy_timeout`, a single transaction, and a `data.db.byok-metadata-backup-<epochms>` backup before any write. `dry_run` defaults to `true`; the skill requires presenting the plan and getting confirmation unless the user is in autopilot mode.

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
    ├── playwright-report-retrieval/         # playwright-report-retrieval@corven-agent-tools
    │   ├── plugin.json, mcp.json
    │   ├── server.py                        # MCP server: fetch_report, list_failed_tests
    │   ├── scripts/fetch_report.py          # Retrieval CLI + CI provider seam
    │   ├── skills/playwright-report-retrieval/  # Canonical E2E + reporting standards
    │   └── tests/                           # Unit tests + MCP smoke tests
    ├── copilot-byok-metadata/               # copilot-byok-metadata@corven-agent-tools
    │   ├── plugin.json, mcp.json
    │   ├── server.py                        # MCP server: list, fetch, plan, apply, registry
    │   ├── skills/copilot-byok-metadata/    # Thin skill: read → plan → confirm → apply
    │   └── tests/                           # Unit tests, apply tests, MCP smoke tests
    └── use-decision-model/                  # use-decision-model@corven-agent-tools
        ├── plugin.json
        └── skills/use-decision-model/       # Skill, sender, example payloads
```

## Development

```shell
python3 -m unittest discover -s plugins/github-issues/skills/issue-work-queue/tests -v
python3 -m unittest discover -s plugins/squad-model-resolver/tests -v
python3 -m unittest discover -s plugins/playwright-report-retrieval/tests -v
python3 -m unittest discover -s plugins/copilot-byok-metadata/tests -v
python3 -m unittest discover -s plugins/change-learn/skills/change-learn/tests -v
```

The change-learn MCP host builds with `dotnet build hosts/ChangeLearn.Host`.

## License

MIT
