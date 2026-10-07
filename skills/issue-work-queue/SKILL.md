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
| `--project` | number | repo's `.github/issue-queue.json` | GitHub Projects V2 number; enables the board-drift diagnostic |
| `--project-owner` | login | repository owner | Owner of the project |

## Readiness semantics

- An issue is **available** when it is OPEN, has no unresolved `## Depends On` / `## Blocked By` references, and has no linked (closing) open PR.
- A reference is **unsatisfied only when its referent is OPEN**. A CLOSED referent is satisfied — completed work cannot block.
- A reference whose issue does not exist in any state is flagged `(missing)` in text output so broken references cannot hide.
- References are issue numbers (`#123`, `123`) or task ids (`7.4`, parsed from `[Task X.Y]` titles). `none` / `n/a` sections mean no dependencies.
- Output sections: `Next available`, `Available`, `Blocked` (with `depends on` / `blocked by` reasons).

## Board drift diagnostic (opt-in `--project`)

Pass `--project <number>` (GitHub Projects V2) to additionally compare the board's `Status` field against queue-derived expectations.

**You don't need to know the project number.** A repo declares it once in `.github/issue-queue.json`:

```json
{ "project": 3 }
```

When `--project` is omitted, the helper reads that file from the current working directory upward (stopping at the repo root — it never escapes the repository). Precedence: **explicit `--project` flag → repo config → no board section**. The repo owns its own board context, so agents get the diagnostic automatically without having to be told the number. An optional `"owner"` key overrides the default owner (the repository owner). A malformed config degrades to `board.error` — visible in the output, but never fatal to the queue.

**This is a diagnostic only.** It never changes readiness, and per the precedence rule the queue output remains authoritative — the board is a view that can drift, and these entries exist so a receiving agent can see *where* it drifted and act on it.

Expected-status derivation: CLOSED → `Done`; OPEN with a linked open PR → `In Progress`; otherwise → `Todo` (the board has no Blocked state, so blocked work is expected to sit in `Todo`).

| kind | meaning | informational |
| --- | --- | --- |
| `done_but_open` | Board says Done but the issue is still OPEN | no |
| `closed_not_done` | Issue CLOSED but the board card wasn't moved to Done | no |
| `work_started_board_stale` | Linked open PR exists but the board still shows Todo | no |
| `in_progress_without_pr` | Board shows In Progress with no linked open PR (may be pre-PR work) | yes |
| `missing_status` | Board item has no Status value | yes |
| `unexpected_status` | Board status outside the Todo/In Progress/Done model | yes |

Plus `not_on_board`: open queue issues absent from the project (informational).

`--project-owner` defaults to the repository owner. If the project query fails, the output carries `board.error` and the queue still renders — the diagnostic never breaks the core queue.

```bash
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --project 3 --format text
```

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
