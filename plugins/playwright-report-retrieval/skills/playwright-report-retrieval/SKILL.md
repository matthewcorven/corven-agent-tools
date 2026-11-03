---
name: playwright-report-retrieval
description: >-
  Canonical Playwright reporting and AI-driven E2E standards, plus retrieval of
  CI Playwright HTML reports. Use when configuring playwright.config.ts
  reporters, traces or screenshots; when adding or reviewing E2E coverage; when
  a CI run failed and you need the report; or when a user asks to "open the
  Playwright report", "show the failed tests from CI", or "fetch the report for
  commit <sha>". Enforces Playwright-only E2E automation, Red-Green-Refactor,
  the coverage gate, deterministic data-driven journeys, and storageState
  forking for parallel alternate/edge paths.
---

# Playwright Reporting & AI-Driven E2E Standards

This skill is the canonical source for how E2E tests are written, reported and
retrieved. Repository-level instruction files should reference it rather than
restate it.

## 1. Tooling constraint

**Playwright is the only permitted E2E UI automation tool.** Do not introduce
Selenium, Cypress, WebdriverIO, Puppeteer-based harnesses, or bespoke
browser-driving scripts. If a repository already has one, migrate it rather than
running both.

## 2. Red-Green-Refactor

Work test-first:

1. **Red** — write the failing E2E test that describes the new behaviour. Run it
   and confirm it fails for the right reason.
2. **Green** — make the smallest production change that turns it green.
3. **Refactor** — clean up with the test still passing.

A test written after the implementation only proves the implementation matches
itself. (Note: some older documents call this "red-blue-green"; the correct
methodology is Red-Green-Refactor.)

## 3. Coverage gate

**No new UI feature and no UI modification may be committed or pushed without
corresponding E2E coverage.** A change is not reviewable until the suite
exercises it.

Every user workflow must be demonstrated by a **long-form, deterministic,
data-driven** test that arranges its own **ephemeral state**. Each workflow needs
all three path classes:

| Path class | What it proves |
| --- | --- |
| Happy path | The intended journey works end to end. |
| Alternate path | A legitimate variation works (keyboard vs mouse, replace-history navigation, second submission, different entry point). |
| Edge case | The boundary that breaks naive implementations (blank input, unknown route, empty collection, duplicate submit, oversized payload). |

## 4. Determinism

- No `waitForTimeout` as a synchronisation primitive. Assert on observable state.
- No shared mutable fixtures. Each test arranges its own records and cleans up.
- No order dependence. `fullyParallel: true` is a hard requirement; a test that
  only passes in sequence is a bug in the test.
- Stub non-deterministic dependencies (LLM, clock, third-party APIs) behind a
  config switch and assert on the stub's documented contract.

## 5. Compartmentalization and parallel execution

Alternate paths and edge cases must not replay a long predecessor journey.
Capture the predecessor state once and fork it into isolated workers:

```ts
// e2e/fixtures.ts
export const test = base.extend<{ fork: Fork }>({
  fork: async ({ browser }, use) => {
    const dir = path.join('test-results', '.state');
    await use({
      async capture(page, name) {
        await fs.mkdir(dir, { recursive: true });
        await page.context().storageState({ path: path.join(dir, `${name}.json`) });
      },
      async open(name) {
        const context = await browser.newContext({
          storageState: path.join(dir, `${name}.json`),
        });
        return context.newPage();
      },
    });
  },
});
```

`browserContext.storageState()` captures cookies and localStorage, so a completed
login or a half-finished multi-step wizard becomes a cheap starting point for
parallel workers.

**All backend integration must be part of the test arrangement**, so the state a
fork depends on can be created on demand rather than inherited from whatever
another worker left behind.

## 6. Local reporting: ephemeral storage

`playwright.config.ts`:

```ts
reporter: [['html', { open: 'on-failure' }]],
use: {
  trace: 'retain-on-failure',
  screenshot: 'only-on-failure',
  video: 'retain-on-failure',
},
```

`.gitignore` must contain:

```gitignore
playwright-report/
blob-report/
test-results/
playwright-reports/
node_modules/
```

The HTML report opens automatically **only on failure**. Traces, screenshots and
video are `retain-on-failure`, so a green run leaves no disk bloat.

## 7. CI reporting: time-limited artifacts

- Upload `playwright-report/` as a pipeline artifact **regardless of pass/fail**
  (`if: always()`).
- Hardcode `retention-days: 90`.
- **Never commit** reports, traces or screenshots back to any branch.

Sharded pipelines should emit a blob per shard and merge them:

```yaml
- run: npx playwright test --shard=${{ matrix.shard }}/${{ matrix.total }}
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: blob-report-${{ matrix.shard }}
    path: blob-report/
    retention-days: 90
```

```yaml
- run: npx playwright merge-reports --reporter html ./all-blob-reports
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: playwright-report
    path: playwright-report/
    retention-days: 90
```

## 8. Retrieving a report

### CLI

```bash
python3 scripts/fetch_report.py --run-id 123456789
python3 scripts/fetch_report.py --run-id 123456789 --test "Login Flow"
python3 scripts/fetch_report.py --commit abc1234 --test-id 3f2a1b
python3 scripts/fetch_report.py --commit abc1234 --workflow e2e.yml
python3 scripts/fetch_report.py --run-id 123456789 --no-open --json
python3 scripts/fetch_report.py --run-id 123456789 --repo owner/repo
```

The script:

1. Resolves the run (by ID, or by commit SHA via the provider API).
2. Locates the `playwright-report` artifact.
3. Prunes namespace entries older than 7 days (`--max-age-days`, `0` disables).
4. Downloads and extracts to `<git-toplevel>/playwright-reports/<run-id>/`
   (override with `--namespace` or `PLAYWRIGHT_REPORT_NAMESPACE`). The namespace
   falls back to the process working directory, then to the system temp
   directory, so a server whose cwd was deleted still resolves one.
5. Builds a `file://` URL and appends the deep link:
   - `--test "Login Flow"` → `#?q=Login%20Flow`
   - `--test-id 3f2a1b` → `#?testId=3f2a1b`
6. Launches the default browser (`--no-open` suppresses it).

The repository is inferred from the `origin` remote when `--repo` is omitted, so
the script works from any subdirectory of a checkout. Outside a git checkout, or
when `origin` is not a GitHub remote, pass `--repo owner/repo` explicitly. **MCP
callers must always pass `repo`** — the stdio server's working directory is
whatever the CLI happened to spawn it in, not your checkout, and it may not
exist at all. For the same reason, MCP callers should pass `namespace`
explicitly when they care where the report lands; otherwise it resolves to the
git worktree root, then the server's cwd, then the system temp directory.

Resolving by commit SHA scans every workflow that ran for that SHA, so pass
`--workflow e2e.yml` (or the numeric workflow id) whenever the repository has
more than one workflow per commit. The workflow-scoped
`/actions/workflows/{workflow}/runs` endpoint is used, because `workflow_id` is
not a valid filter on `/actions/runs` — GitHub accepts it and silently ignores
it. Without `--workflow`, the script prefers the first run whose artifact list
contains the requested artifact, then falls back to the first completed run.

`gh run download --dir <path>` **extracts** the artifact into `<path>`; it does
not leave a `.zip` behind. The script therefore accepts either an extracted
`index.html` (the normal case) or a `.zip` archive, and searches nested
directories for the report root.

Cached reports are reused without contacting CI, so retrieval still works offline
or after the artifact has expired; pass `--force` to re-download.

### Report format

Modern Playwright HTML reports are **self-contained**: `index.html` embeds the
entire report as a base64-encoded zip inside
`<template id="playwrightReportBase64">`. The zip holds `report.json` plus one
`<fileId>.json` per test file, and each per-file entry carries its own `tests[]`
array. `report.json` has no top-level `tests` key.

`list_failed_tests` decodes that embedded payload first, then falls back to the
legacy `data/report.json` layout. A test counts as failed when its derived status
is `unexpected`, `failed`, `timedOut` or `interrupted`; the status comes from the
test's `outcome`, or from the first failing `results[*].status` when `outcome` is
absent.

### MCP tools

| Tool | Purpose |
| --- | --- |
| `fetch_report` | Download/extract a report and return its deep link. |
| `list_failed_tests` | List failing tests from a fetched report or an existing `report_dir`. |

Natural-language prompts that map onto these tools:

- "Open the Playwright report for run 123456789."
- "Show me the failed tests from the last CI run on commit abc1234."
- "Fetch the report and jump to the Login Flow test."

## 9. Provider seam

`scripts/fetch_report.py` selects an implementation from `PROVIDERS`.
`GitHubActionsProvider` (backed by the `gh` CLI) is the only implementation
today. To add another CI system, subclass `CIProvider`, implement
`resolve_run_id`, `find_artifact` and `download_artifact`, and register it in
`PROVIDERS`. The CLI and MCP pick it up through `--provider` / `provider`.

## 10. Playwright SPA deep links

The HTML report is a single-page app. Deep links are hash routes on
`index.html`:

- `#?q=<urlencoded search>` — filters by test title
- `#?testId=<id>` — opens a specific test

Always URL-encode the search term (`urllib.parse.quote` / `encodeURIComponent`).
