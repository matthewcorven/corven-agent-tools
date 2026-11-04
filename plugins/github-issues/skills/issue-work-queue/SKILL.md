---
name: issue-work-queue
description: >-
  Dependency-driven GitHub issue queue. Use when choosing the next issue to work
  on, reporting queue or board status, answering "what's ready", "what's next",
  "what's blocked", or when issue bodies contain "## Depends On" or "## Blocked
  By" sections. Runs the bundled issue_queue.py helper, whose output MUST take
  precedence over raw `gh issue list`. DO NOT USE FOR: creating issues,
  triage/label assignment, PR creation, or release planning.
---

# Issue Work Queue

Compute which GitHub issues are **ready to work on** versus **blocked**, from the dependency sections in issue bodies.

## Precedence rule (MUST / NEVER)

- **The issue-queue helper's output MUST take precedence over raw `gh issue list` output.** Before choosing work or reporting queue/board status, run the helper first (commands below).
- Agents **MUST NEVER** infer readiness, ordering, or blockers from raw `gh issue list` output or ad hoc label scanning.
- Raw GitHub queries (`gh issue view`, `gh pr view`, `gh pr list`, `gh issue list`) are **follow-up only** — permitted after the helper has identified the specific issue or PR to inspect.
- Issue bodies' `## Depends On` and `## Blocked By` sections are the source of truth for readiness and ordering; issue titles are never used for readiness or prioritization.

## Upgrading the plugin

If this plugin was just upgraded, an MCP server or helper process started by an
earlier version may still be running. A harness session that already has the
server open keeps using the old tools; restart the AI harness (or force-quit
the stale server process) before concluding the upgrade did not take effect.

## Consumer-project guidance

For reliable use across coordinator sessions and spawned agents, add an equivalent repository-specific rule to the consuming project's `AGENTS.md` or `.github/copilot-instructions.md`. This makes the helper's precedence visible even when an agent is not actively reading this skill.

Use this as a template, replacing the placeholders with the consuming repository's values:

```markdown
## Issue Backlog Scans

For issue backlog, triage, queue-status, and "what's next" scans, the `issue-work-queue` helper is authoritative. Run it before reporting readiness, ordering, blockers, or board status; its output takes precedence over raw `gh issue list` results.

- Default board-status scan: `python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --mode status --format text`
- A base `--label <team-label>` includes the exact `<team-label>` label and `<team-label>:*` member labels; use a fully qualified member label to scope one member.
- Use `--mode status` for the untriaged/assigned breakdown; do not reconstruct it from raw label JSON.
- This rule applies to coordinator sessions and every spawned agent that touches the backlog.
- This rule governs issue readiness, ordering, and board status only. PR scans (`gh pr list`, `gh pr view`) remain permitted, and raw issue queries are allowed after the helper identifies a specific issue.
```

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

# Dependency flow diagram — pick the representation that fits the moment:
#   ascii            tiered plain text (terminals, logs, plain-text chat)
#   mermaid          bare Mermaid source (pipe into any renderer)
#   markdown-mermaid Markdown doc with an embedded Mermaid block (GitHub / VS Code preview)
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format ascii
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format mermaid
python3 <skill-dir>/scripts/issue_queue.py --repo <owner/repo> --limit 100 --format markdown-mermaid
```

| Flag | Values | Default | Notes |
| --- | --- | --- | --- |
| `--mode` | `queue`, `status` | `queue` | `status` adds board counts, untriaged/assigned breakdown, open/draft PR counts |
| `--repo` | `owner/repo` | current repo (via `gh repo view`) | Errors if the repo cannot be detected — always pass `--repo` in scripts |
| `--state` | `all`, `open` | `all` | CLOSED issues resolve references as satisfied but never appear in output lists |
| `--label` | repeatable | `squad` | A base label matches itself plus `<label>:*` member labels; a fully qualified member label matches exactly; repeat for multiple (AND) |
| `--format` | `json`, `text`, `ascii`, `mermaid`, `markdown-mermaid` | `json` | `ascii`/`mermaid`/`markdown-mermaid` render the dependency graph as a flow diagram (see [Flow diagram formats](#flow-diagram-formats)) |
| `--limit` | int | `1000` | Max issues fetched from GitHub |
| `--project` | number | repo's `.github/issue-queue.json` | GitHub Projects V2 number; enables the board-drift diagnostic |
| `--project-owner` | login | repository owner | Owner of the project |

## Readiness semantics

- An issue is **available** when it is OPEN, has no unresolved `## Depends On` / `## Blocked By` references, and has no linked (closing) open PR.
- A reference is **unsatisfied only when its referent is OPEN**. A CLOSED referent is satisfied — completed work cannot block.
- A reference whose issue does not exist in any state is flagged `(missing)` in text output so broken references cannot hide.
- References are issue numbers (`#123`, `123`) or task ids (`7.4`, parsed from `[Task X.Y]` titles). `none` / `n/a` sections mean no dependencies.
- Output sections: `Next available`, `Available`, `Blocked` (with `depends on` / `blocked by` reasons).

## Flow diagram formats

`--format ascii`, `--format mermaid`, and `--format markdown-mermaid` render the same dependency graph so agents can pick the representation that fits the consumer: the model's own reasoning, a terminal, or a human-visible surface.

- `ascii` — tiered plain text: Tier 0 is ready work, each following tier waits on the tiers above it. Dependency cycles are listed separately under `Cyclic:` (never silently tiered). Missing references get their own section. Works anywhere plain text renders.
- `mermaid` — a bare `flowchart TD` block with per-kind node classes (green ready, blue in-progress, red blocked, gray done, dashed yellow missing) and links to the GitHub issues. Pipe it into a renderer, or paste it into any surface that renders Mermaid.
- `markdown-mermaid` — a complete Markdown document: metadata header, the Mermaid diagram in a fenced block, a color legend, and the Ready/Blocked lists. Paste directly into GitHub comments, issues, or a `.md` file for human readers.

All three are derived from the same queue computation as `text`/`json` — readiness semantics, precedence, and the board-drift diagnostic are unchanged; the format only changes the presentation. Diagram kinds map to queue state: `ready` = available, `in-progress` = linked open PR, `blocked` = unresolved references, `done` = satisfied (CLOSED) referent shown for graph context, `missing` = broken reference.

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
