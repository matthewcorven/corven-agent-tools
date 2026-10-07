---
name: issue-work-queue
description: Dependency-driven GitHub issue queue. Use when choosing the next issue to work on, reporting queue or board status, answering "what's ready", "what's next", "what's blocked", or when issue bodies contain "## Depends On" or "## Blocked By" sections. Runs the bundled issue_queue.py helper, whose output MUST take precedence over raw `gh issue list`. DO NOT USE FOR: creating issues, triage/label assignment, PR creation, or release planning.
---

# Issue Work Queue

Compute which GitHub issues are **ready to work on** versus **blocked**, from the dependency sections in issue bodies.

## Precedence rule (MUST / NEVER)

- **The issue-queue helper's output MUST take precedence over raw `gh issue list` output.** Before choosing work or reporting queue/board status, run the helper first (commands below).
- Agents **MUST NEVER** infer readiness, ordering, or blockers from raw `gh issue list` output or ad hoc label scanning.
- Raw GitHub queries (`gh issue view`, `gh pr view`, `gh pr list`, `gh issue list`) are **follow-up only** — permitted after the helper has identified the specific issue or PR to inspect.
- Issue bodies' `## Depends On` and `## Blocked By` sections are the source of truth for readiness and ordering; issue titles are never used for readiness or prioritization.

## Requirements

- `python3` (standard library only — no pip packages)
- Authenticated [`gh` CLI](https://cli.github.com/)

## Usage

`<skill-dir>` is the directory containing this `SKILL.md`; the helper is bundled at `scripts/issue_queue.py` relative to it.

```bash
# Ready-vs-blocked queue (choose next work)
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text

# Board status (counts, untriaged/assigned, PRs) — for "status" / "what's on the board?" requests
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text --mode status

# Member-scoped queue
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format text --label squad:{member}
```

| Flag | Values | Default | Notes |
| --- | --- | --- | --- |
| `--mode` | `queue`, `status` | `queue` | `status` adds board counts, untriaged/assigned breakdown, open/draft PR counts |
| `--repo` | `owner/repo` | current repo (via `gh repo view`) | Errors if the repo cannot be detected — always pass `--repo` in scripts |
| `--state` | `all`, `open` | `all` | CLOSED issues resolve references as satisfied but never appear in output lists |
| `--label` | repeatable | `squad` | `squad` matches the base label plus `squad:{member}` labels; repeat for multiple (AND) |
| `--format` | `json`, `text` | `json` | |
| `--limit` | int | `1000` | Max issues fetched from GitHub |

## Readiness semantics

- An issue is **available** when it is OPEN, has no unresolved `## Depends On` / `## Blocked By` references, and has no linked (closing) open PR.
- A reference is **unsatisfied only when its referent is OPEN**. A CLOSED referent is satisfied — completed work cannot block.
- A reference whose issue does not exist in any state is flagged `(missing)` in text output so broken references cannot hide.
- References are issue numbers (`#123`, `123`) or task ids (`7.4`, parsed from `[Task X.Y]` titles). `none` / `n/a` sections mean no dependencies.
- Output sections: `Next available`, `Available`, `Blocked` (with `depends on` / `blocked by` reasons).

## Authoring issues

```markdown
## Depends On
#123
7.4

## Blocked By
456
```

## Non-goals

Board-state transitions, triage assignment, label sync, and PR lifecycle belong to repo-local tooling (e.g. squad workflows). This skill only answers: *what is ready, what is blocked, and why.*
