# corven-dev-copilot-plugin

A GitHub Copilot **Agent Plugins 1.0** plugin (the v1 open plugin format — not the legacy Copilot format) that ships dependency-driven GitHub issue-tracking tooling.

## What's inside

| Component | Location | Purpose |
| --- | --- | --- |
| `issue-work-queue` skill | `skills/issue-work-queue/SKILL.md` | Portable rules for a dependency-driven work queue: issue bodies' `## Depends On` / `## Blocked By` sections are the source of truth, and **the queue helper takes precedence over raw `gh issue list`** |
| Queue helper | `skills/issue-work-queue/scripts/issue_queue.py` | Computes the ready-vs-blocked queue (`queue` mode) and board counts (`status` mode) via the `gh` CLI |
| Unit tests | `skills/issue-work-queue/tests/test_issue_queue.py` | Stdlib `unittest` suite, run in CI |

Requirements: `python3` (stdlib only) and an authenticated [`gh` CLI](https://cli.github.com/).

## Install

Direct from GitHub (Copilot CLI):

```shell
copilot plugin install matthewcorven/corven-dev-copilot-plugin
```

Or register this repository as a marketplace (it ships `.github/plugin/marketplace.json`):

```shell
copilot plugin marketplace add matthewcorven/corven-dev-copilot-plugin
copilot plugin install corven-dev
```

You can also enable it declaratively in `.github/copilot/settings.json` via the `enabledPlugins` field.

## Usage

The skill is discovered automatically. Ask for queue status, "what's ready next", or "what's blocking X" — or invoke the helper directly. `<skill-dir>` is the directory containing the skill's `SKILL.md`:

```shell
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text --mode status
```

Flags: `--mode {queue,status}` · `--repo owner/repo` · `--state {all,open}` · `--label` (repeatable, default `squad`) · `--format {text,json}` · `--limit N`.

### The precedence rule

The issue-queue helper's output **must take precedence over raw `gh issue list`**. Agents must never infer readiness, ordering, or blockers from `gh issue list` or ad hoc label scans; raw GitHub queries are follow-up only, after the helper identifies the specific issue or PR to inspect.

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
corven-dev-copilot-plugin/
├── plugin.json                       # Agent Plugins 1.0 manifest ($schema → v1, not legacy)
├── skills/issue-work-queue/
│   ├── SKILL.md
│   ├── scripts/issue_queue.py
│   └── tests/test_issue_queue.py
├── .github/plugin/marketplace.json
└── .github/workflows/test.yml
```

## Development

```shell
python3 -m unittest discover -s skills/issue-work-queue/tests -v
```

## License

MIT
