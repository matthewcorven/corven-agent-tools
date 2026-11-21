---
name: e2e-artifacts
description: >-
  Canonical standard and tooling for durable, discoverable visual and E2E
  artifacts across planning, mockups, development, debugging, and QA. Use when
  deciding where screenshots, wireframes, mockups, traces, or debug captures
  should live; when creating or pruning artifact runs; when wiring
  artifacts.budget.json, .githooks, or CI capture attachment; or when a repo
  needs one collision-free artifact root shared across git worktrees. Owns the
  <repo>-artifacts/wt-<basename>-<sha8>/runs/<utc>-<rand6>/ layout, the
  artifacts.py CLI, and the four discovery layers. DO NOT USE FOR: Playwright
  authoring style, locators, or flake debugging (use the sibling
  playwright-report-retrieval skill), or fetching a CI HTML report.
---

# Visual & E2E Artifact Standard

This skill owns where generated visual and E2E artifacts live, how they are
found again, and how they are bounded. It is the single owner of the durable run
artifact taxonomy: other skills and plugins consume `artifacts.py` rather than
inventing a second layout.

Sibling skill: [`playwright-report-retrieval`](../playwright-report-retrieval/SKILL.md)
owns Playwright authoring standards (locator ladder, flake debugging, anti-pattern
red flags) and CI HTML report retrieval. This skill does not restate that
guidance.

## 1. The two modes

Artifacts have two distinct lifecycles, and conflating them is the root cause of
every "screenshots in git" and "artifacts vanished with the worktree" problem.

| Mode | Where it lives | Committed? | Purpose |
| --- | --- | --- | --- |
| Local | Sibling `<repo>-artifacts/` root, outside the git tree | Never | Planning, mockups, development, debugging, QA evidence for a human or agent |
| CI | Pipeline artifact attached to the run | Never | Failure evidence and start/checkpoint state attached to the report |

Both modes are **behavioral and checkpoint-only by default**. There are no
committed pixel baselines: a screenshot proves *what a user saw at this moment*,
not *that the pixels must never change*. Visual-regression pixel diffs belong to
a dedicated tool with its own baseline policy, not to this standard.

## 2. The artifact root

Generated artifacts live in a sibling of the **main repository**, never inside the
git tree:

```text
<repo>-artifacts/
  runs.jsonl                       # append-only index (one JSON object per line)
  latest -> wt-<basename>-<sha8>/runs/<utc>-<rand6>
  .budget-check.stamp              # hook throttle stamp
  refs/
    issue-<N>/
      <utc>-<rand6> -> ../../wt-<basename>-<sha8>/runs/<utc>-<rand6>
  wt-<basename>-<sha8>/
    manifest.json                  # repo, worktree, branch, commit, firstSeen, lastUsed
    latest -> runs/<utc>-<rand6>
    runs/
      <YYYYMMDDTHHMMSSZ>-<rand6>/
        planning/  mockups/  dev/  debug/  qa/
```

Key properties:

- **Outside the tree.** Removing a worktree must never delete artifacts, and
  `git status` must never show them. Nothing under `<repo>-artifacts/` is
  committable.
- **One root per clone, not per worktree.** Every worktree of the same clone
  resolves the same `artifactRoot` (via `git rev-parse --git-common-dir`), so
  evidence from a feature worktree and from the main checkout is discoverable in
  one place.
- **Collision-free by construction.** `<basename>-<sha8>` is derived from the
  realpath of the worktree, so two worktrees never share a directory even when
  they are both named `repo-12`. `<utc>-<rand6>` makes two runs in the same second
  distinct.
- **All five phase directories exist on every run**, whether or not the run uses
  them. A consumer can write `qa/` without first probing for it.
- **Override for non-standard hosts:** `E2E_ARTIFACTS_ROOT` replaces the root
  (e.g. a scratch volume). `VDS_ARTIFACTS_ROOT` is accepted as a legacy alias.

## 3. The four discovery layers

A human or agent must be able to find the artifacts from four independent entry
points. Each layer is deliberate; none is a convenience alias for another.

| Layer | Mechanism | Answers |
| --- | --- | --- |
| 1 | `ARTIFACT run=… issue=… phase=… spec=… path=…` on stdout | What did the process I just ran create? |
| 2 | `runs.jsonl` + `artifacts.py find` / plain `find` | What runs exist for this issue, commit, branch, or spec? |
| 3 | `latest` pointers (root and worktree) | What is the most recent run right now? |
| 4 | `refs/issue-<N>/<run>` symlinks | What evidence belongs to issue N? |

Layer 1 is machine-readable and stable. Parse it; do not scrape prose. Print it
once per run, on the same stream the caller already reads.

Layer 2 is append-only: `run` appends one record, `record-meta` rewrites that
record in place, `prune` removes records whose directories it deleted. Never
hand-edit `runs.jsonl` in a way that reorders it — consumers treat the last
matching record as the newest.

Layer 3 is a symlink, so it is atomic to replace and cheap to read. Both pointers
move together on every new run.

Layer 4 is a symlink directory, so one run can be referenced from several issues
without copying bytes.

## 4. The CLI contract

`scripts/artifacts.py` is stdlib-only Python 3 and works on macOS and Linux. All
subcommands accept `--repo-path` and most accept `--json`.

```bash
# Where do artifacts go? (prints the root, or full context with --json)
python3 scripts/artifacts.py --repo-path . root

# Allocate a run; prints the ARTIFACT line
python3 scripts/artifacts.py --repo-path . run --phase qa --spec "checkout-flow" --issue 12

# Same, machine-readable
python3 scripts/artifacts.py --repo-path . run --phase planning --issue 12 --json

# Update status and measured size in the index
python3 scripts/artifacts.py --repo-path . record-meta --run-dir <run-dir> --status passed

# Find runs (all filters compose; --latest returns only the newest match)
python3 scripts/artifacts.py --repo-path . find --issue 12 --json
python3 scripts/artifacts.py --repo-path . find --commit abc1234 --phase qa --limit 5

# Local usage against artifacts.budget.json (warn-only, always exit 0)
python3 scripts/artifacts.py --repo-path . usage --verbose
python3 scripts/artifacts.py --repo-path . usage --throttle     # hook-safe, rate-limited

# Reclaim space (dry-run unless --confirm)
python3 scripts/artifacts.py --repo-path . prune --older-than 14d
python3 scripts/artifacts.py --repo-path . prune --older-than 14d --confirm

# Per-run CI budget with a GitHub Actions annotation (warn-only)
python3 scripts/artifacts.py --repo-path . check-ci-budget playwright-report/
```

Rules that are not negotiable:

- **Subcommand names are a contract.** `root`, `run`, `record-meta`, `find`,
  `usage`, `prune`, `check-ci-budget`, `stamp`, `check-parity`. Downstream
  plugins consume them; renaming one is a breaking change.
- **The run-root layout is a contract.** `<repo>-artifacts/wt-<basename>-<sha8>/runs/<utc>-<rand6>/{planning,mockups,dev,debug,qa}`.
- **Budget checks never fail a build.** `usage` and `check-ci-budget` exit 0 even
  when over limit; they emit a warning (and, in CI, an annotation) instead. A
  budget is a signal to prune, not a gate that blocks a merge.
- **`prune` is a dry run by default.** Only `--confirm` deletes, and deletion also
  drops the matching index records and cleans dangling symlinks.

## 5. `artifacts.budget.json`

A repository opts in by committing one file at the repo root. Absent, the
built-in defaults apply — the file is a contract, not a prerequisite.

```json
{
  "schemaVersion": 1,
  "local": {
    "maxGb": 2,
    "warnAtPercent": 80,
    "hookThrottleSeconds": 300
  },
  "ci": {
    "maxMbPerRun": 500,
    "warnAtPercent": 80,
    "captureKinds": ["start", "fail", "checkpoint"]
  }
}
```

| Key | Meaning |
| --- | --- |
| `local.maxGb` | Warn once the whole sibling artifact root exceeds this. |
| `local.warnAtPercent` | Percentage of `maxGb` at which the warning fires. |
| `local.hookThrottleSeconds` | How long a `--throttle` check stays silent after a scan. |
| `ci.maxMbPerRun` | Warn when one run's attached artifacts exceed this. |
| `ci.warnAtPercent` | Percentage of `maxMbPerRun` at which the warning fires. |
| `ci.captureKinds` | Which capture kinds CI attaches. Default `start`, `fail`, `checkpoint`. |

`local` and `ci` merge independently over the defaults, so a repo may override
only the one it cares about. Unknown keys are ignored rather than rejected — a
newer contract must not break an older repo.

## 6. Opt-in `.githooks` pattern

Budget awareness at the moment work happens is worth more than a nightly report.
Commit the hooks and let each developer opt in once:

```bash
git config core.hooksPath .githooks
```

```bash
#!/usr/bin/env bash
# .githooks/pre-commit
# Warn-only local visual/E2E artifact budget check (opt-in via: git config core.hooksPath .githooks)
# Always exits 0 — never blocks a commit.
set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if command -v python3 >/dev/null 2>&1 && [ -f "$REPO_ROOT/scripts/artifacts.py" ]; then
  python3 "$REPO_ROOT/scripts/artifacts.py" usage --throttle || true
fi

exit 0
```

The same body is used for `pre-commit`, `post-checkout`, and `post-merge`. Every
hook must:

- guard on `python3` and on `scripts/artifacts.py` existing, so a clone without
  the vendored copy is unaffected;
- pass `--throttle`, so a busy branch switch does not rescan the root each time;
- end with `exit 0`, unconditionally. A hook that can fail a commit will be
  disabled by the first developer it inconveniences.

Never install hooks implicitly. `core.hooksPath` is a per-clone developer choice.

## 7. CI capture model

CI attaches **behavioral evidence at defined moments**, not a filmstrip.

| Kind | When | Why |
| --- | --- | --- |
| `start` | Before the journey acts | Proves the entry state the test actually began from |
| `fail` | On failure | Proves what the user saw when the assertion failed |
| `checkpoint` | At a named intermediate assertion | Proves the path reached the state under test |

```ts
// e2e/helpers/visual-artifacts.ts (shape, not a required file)
export function shouldCaptureInCurrentEnv(kind: CaptureKind): boolean {
  if (!process.env.CI) return true;                       // local: capture everything
  if (process.env.CAPTURE_ALL_VISUALS === 'true') return true;  // explicit debugging override
  return kind === 'start' || kind === 'checkpoint';       // CI: start + checkpoint only
}
```

In CI the capture is attached in memory via `testInfo.attach()`, so it rides
inside the `playwright-report` artifact and expires with it. Failures are already
covered by `screenshot: 'only-on-failure'` in the config; a helper must not
duplicate that.

Budget the result with `check-ci-budget` and warn rather than fail:

```yaml
- name: Check E2E artifact budget
  if: always()
  run: python3 scripts/artifacts.py check-ci-budget --budget-file artifacts.budget.json playwright-report/
```

`CAPTURE_ALL_VISUALS=true` exists for a debugging run on a branch. It is not a
permanent CI setting; a pipeline that needs it permanently has a test-design
problem, not a capture problem.

## 8. Vendoring into a consuming repository

CI runners and `.githooks/` have no `~/.copilot/installed-plugins/`, so a
consuming repository keeps its own copy of `scripts/artifacts.py` at the repo root
(outside `<repo>-artifacts/`) and stamps it:

```bash
# copy the canonical file, then stamp it with the plugin commit you copied from
python3 scripts/artifacts.py stamp scripts/artifacts.py --commit <plugin-commit-sha>
```

The stamp is two comment lines:

```text
# vendored-from: playwright-report-retrieval@corven-agent-tools v0.3.0 sha=<plugin-commit-sha>
# VENDORED_SHA256: <sha256 of the body with stamp lines removed>
```

The commit sha is required. A bare `vX.Y.Z` cannot distinguish a copy taken
before the version bump from one taken after it, so a bare version stamp is a
parity failure.

Make drift fail loudly in CI:

```yaml
- name: Verify vendored artifacts.py parity
  run: |
    python3 plugins/playwright-report-retrieval/scripts/artifacts.py check-parity scripts/artifacts.py
```

`check-parity` compares three things and exits non-zero on any mismatch: the
declared `VENDORED_SHA256` against the copy's actual body hash, the declared
version against the canonical plugin version, and — when a canonical file is
available via `--canonical` — the copy's body against the canonical body. A
comment alone never fails a build; this does.

Re-stamping is idempotent and never changes the body hash, so refreshing the
recorded commit is a no-op for parity as long as the code is unchanged.

## 9. Choosing the right phase directory

| Phase | What belongs there |
| --- | --- |
| `planning/` | Problem framing, acceptance criteria, flow sketches |
| `mockups/` | Wireframes, design comparisons, approved visual proposals |
| `dev/` | Implementation screenshots taken while building |
| `debug/` | Captures taken while diagnosing a failure, with the hypothesis in the filename |
| `qa/` | Final evidence for the journey, including CI-equivalent captures |

A run's `--phase` names the phase it started in; all five directories still exist,
so a run may write into several. Name files `<NN>-<use-case>-<action>.png` so
lexical order matches the journey.

## 10. Anti-patterns

| Anti-pattern | Why it fails | Do instead |
| --- | --- | --- |
| Writing captures into `test-results/` or `screenshots/` inside the repo | Committed by accident, deleted with the worktree, invisible to `find` | Allocate a run and write into its phase directory |
| Committing pixel baselines as the default policy | Every cosmetic change becomes a red build; nobody trusts the suite | Behavioral/checkpoint-only; dedicated tool for pixel diffs |
| One shared `artifacts/` directory at the repo root | Collides across worktrees and branches | Sibling root keyed by `<basename>-<sha8>` |
| A hook that exits non-zero on budget | Developers disable hooks | Always `exit 0`; warn only |
| Hand-writing `runs.jsonl` | Breaks append-only ordering and `find` | Use `run` / `record-meta` / `prune` |
| Bare `vX.Y.Z` vendored stamp | Cannot distinguish pre-merge from post-merge | Record the plugin commit sha |
| Pruning without `--confirm` in a script | Silent data loss when `--confirm` is added by accident | Dry run first, confirm deliberately |
