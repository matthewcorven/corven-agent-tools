# corven-copilot-tools

A GitHub Copilot **Agent Plugins 1.0** marketplace (the v1 open plugin format — not the legacy Copilot format) shipping dependency-driven GitHub issue-tracking tooling.

## What's inside

| Component | Location | Purpose |
| --- | --- | --- |
| `issue-work-queue` skill | `plugins/github-issues/skills/issue-work-queue/SKILL.md` | Portable rules for a dependency-driven work queue: issue bodies' `## Depends On` / `## Blocked By` sections are the source of truth, and **the queue helper takes precedence over raw `gh issue list`** |
| Queue helper | `plugins/github-issues/skills/issue-work-queue/scripts/issue_queue.py` | Computes the ready-vs-blocked queue (`queue` mode) and board counts (`status` mode) via the `gh` CLI |
| Unit tests | `plugins/github-issues/skills/issue-work-queue/tests/test_issue_queue.py` | Stdlib `unittest` suite, run in CI |

Requirements: `python3` (stdlib only) and an authenticated [`gh` CLI](https://cli.github.com/).

## Install

Register the marketplace once, then install the plugin:

```shell
copilot plugin marketplace add matthewcorven/corven-copilot-tools
copilot plugin install github-issues@corven-copilot-tools
```

The install source must be the qualified `plugin@marketplace` form — a bare `github-issues` is rejected with `Invalid plugin spec`.

You can also enable it declaratively in `.github/copilot/settings.json` (repository-level) or `~/.copilot/settings.json` (user-level). The `enabledPlugins` key must be the qualified `github-issues@corven-copilot-tools`, and a non-default marketplace must also be registered under `extraKnownMarketplaces`:

```json
{
  "enabledPlugins": {
    "github-issues@corven-copilot-tools": true
  },
  "extraKnownMarketplaces": {
    "corven-copilot-tools": {
      "source": {
        "source": "github",
        "repo": "matthewcorven/corven-copilot-tools"
      }
    }
  }
}
```

## Usage

The skill is discovered automatically. Ask for queue status, "what's ready next", or "what's blocking X" — or invoke the helper directly. `<skill-dir>` is the directory containing the skill's `SKILL.md`:

```shell
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text --mode status
```

Flags: `--mode {queue,status}` · `--repo owner/repo` · `--state {all,open}` · `--label` (repeatable, default `squad`) · `--format {text,json}` · `--limit N` · `--project N` (optional board-drift diagnostic) · `--project-owner login`.

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

## Repository layout

```text
corven-copilot-tools/                      # marketplace: corven-copilot-tools
├── .github/plugin/marketplace.json
└── plugins/
    └── github-issues/                    # plugin: github-issues@corven-copilot-tools
        ├── plugin.json                   # Agent Plugins 1.0 manifest ($schema → v1, not legacy)
        └── skills/issue-work-queue/
            ├── SKILL.md
            ├── scripts/issue_queue.py
            └── tests/test_issue_queue.py
```

## Development

```shell
python3 -m unittest discover -s plugins/github-issues/skills/issue-work-queue/tests -v
```

## License

MIT
