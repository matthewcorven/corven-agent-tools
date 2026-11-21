---
name: playwright-report-retrieval
description: >-
  Canonical Playwright reporting and AI-driven E2E standards, plus retrieval of
  CI Playwright HTML reports. Use when configuring playwright.config.ts
  reporters, traces or screenshots; when adding or reviewing E2E coverage; when
  choosing locators, debugging a flaky or racy test, or reviewing test code for
  anti-patterns; when a CI run failed and you need the report; or when a user
  asks to "open the Playwright report", "show the failed tests from CI", or
  "fetch the report for commit <sha>". Enforces Playwright-only E2E automation,
  Red-Green-Refactor, the coverage gate, deterministic data-driven journeys,
  and storageState forking for parallel alternate/edge paths. DO NOT USE FOR:
  deciding where generated screenshots, mockups, or debug captures should live,
  or how to prune and budget them (use the sibling e2e-artifacts skill).
---

# Playwright Reporting & AI-Driven E2E Standards

This skill is the canonical source for how E2E tests are written, reported and
retrieved. Repository-level instruction files should reference it rather than
restate it.

Sibling skill: [`e2e-artifacts`](../e2e-artifacts/SKILL.md) owns the durable
visual/E2E artifact standard — the sibling `<repo>-artifacts/` root, the
`artifacts.py` CLI, the four discovery layers, `artifacts.budget.json`, opt-in
`.githooks`, and the CI start/fail/checkpoint capture model. This skill owns how
the tests are authored and how the CI HTML report is retrieved; it deliberately
does not restate artifact placement.

## Upgrading the plugin

This plugin ships an MCP server. After an upgrade, a server process started by
the previous version can still be running, and a harness session that already
has it open keeps calling the old tools. Restart the AI harness (or force-quit
the stale server process) before checking for new tools or behavior.

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

## 4. Declared profiles / overrides

`fullyParallel: true` is the default requirement, not a universal law. A
repository may run an intentional profile that departs from it, and that profile
is **compliant by declaration** — but only when the deviation is named, scoped,
and justified in the repo's own E2E documentation. An undeclared serial test is
still a bug.

| Profile | Why it legitimately exists | What the declaration must state |
| --- | --- | --- |
| Serial / `workers: 1` | Evidence capture that must not interleave; a shared external resource that cannot be cloned | Which config file or project selects it, which specs it covers, and why parallelism is unsafe there |
| Loopback-only | A journey that must exercise a real socket the harness cannot stub | The exact bind address/port, how the port is reserved, and that no other test may bind it |
| No-capture | A profile whose purpose is behavior only (perf, smoke, contract) | That no visual evidence is produced, so a missing capture is expected rather than a failure |
| Reduced matrix | One browser/project on a long-running branch to keep feedback usable | Which projects are omitted and where the full matrix still runs |

Rules for a declared profile:

1. **Name it in the config.** Prefer a separate config file (for example
   `playwright.isolated.config.ts`) or a named `projects[]` entry over an
   environment variable that silently changes behavior.
2. **Scope it narrowly.** A profile selects specific specs; it does not become
   the repo's default because it was easier.
3. **Document the exception where a reviewer will find it** — in the repo's E2E
   doc, next to the config it describes, cross-linked from the config's own
   comment.
4. **Keep the parallel suite parallel.** The main suite must still pass with
   `fullyParallel: true` and no ordering assumptions. If a spec only passes in
   the serial profile, it is broken, not special.
5. **Never let a profile hide a failure.** A profile may change *how* a journey
   runs; it may not turn a failing assertion into a skip.

If you find an undeclared deviation, the fix is to declare it or remove it — not
to loosen `fullyParallel` for everyone.

## 5. Determinism

- No `waitForTimeout` as a synchronisation primitive. Assert on observable state.
- No shared mutable fixtures. Each test arranges its own records and cleans up.
- No order dependence. `fullyParallel: true` is the default requirement (a test
  that only passes in sequence is a bug in the test); a deliberately serial
  profile must be declared as described in §4.
- Stub non-deterministic dependencies (LLM, clock, third-party APIs) behind a
  config switch and assert on the stub's documented contract.

## 6. Compartmentalization and parallel execution

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

## 7. Local reporting: ephemeral storage

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

### What is never committed, and what the consuming repo owns

Two categories are easy to confuse. Only the first is settled by this skill.

**Never committed — settled here:**

- Generated report outputs: `playwright-report/`, `blob-report/`,
  `playwright-reports/`, `test-results/`.
- Run captures: every artifact written under the sibling
  `<repo>-artifacts/wt-<basename>-<sha8>/runs/<utc>-<rand6>/` root, including
  planning, mockup, development, debug, and QA evidence. That root lives outside
  the git tree precisely so it can never be committed. Placement, discovery,
  pruning, and budgeting are owned by the sibling
  [`e2e-artifacts`](../e2e-artifacts/SKILL.md) skill.

**Governed by the consuming repository's own contract — not by this skill:**

- **Durable consumed fixtures** that a spec *reads* rather than produces — for
  example committed HTML email samples a branding audit parses, or seed JSON a
  journey loads. These are inputs, they must be versioned with the code that
  consumes them, and the repo decides their path and lifecycle.
- **Grandfathered historical evidence** already committed before this standard
  existed. It is not retroactively a violation; the repo decides whether to keep,
  relocate, or retire it, and should record that decision where its E2E docs
  live.

The test is direction of flow: if the spec writes it, it is generated and never
committed; if the spec reads it as input, it is durable and the repo governs it.
A spec must never write into a directory that also holds committed fixtures.

## 8. CI reporting: time-limited artifacts

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

## 9. Retrieving a report

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
entire report as a base64-encoded zip inside an element carrying
`id="playwrightReportBase64"`. The reporter has shipped that element in two
shapes, and both are supported:

- `@playwright/test` 1.64.x — `<template id="playwrightReportBase64">` inside `<body>`.
- `@playwright/test` 1.57.x — `<script id="playwrightReportBase64" type="application/zip">`
  appended **after** `</html>`.

The zip holds `report.json` plus one `<fileId>.json` per test file, and each
per-file entry carries its own `tests[]` array. `report.json` has no top-level
`tests` key. Some versions instead inline every test into `report.json` under a
top-level `files[]`; that shape is flattened too.

`list_failed_tests` decodes that embedded payload first, then falls back to the
legacy `data/report.json` layout. A test counts as failed when its derived status
is `unexpected`, `failed`, `timedOut` or `interrupted`; the status comes from the
test's `outcome`, or from the first failing `results[*].status` when `outcome` is
absent.

**Global errors are failures.** When a report aborts in `globalSetup` (or any
other global hook) before a spec runs, Playwright writes `files: []` and a
non-empty `errors[]` while still reporting `stats.ok: true`. `list_failed_tests`
surfaces those errors as `errors[]` / `error_count` and counts each one in
`failed_count`, so a zero-test run is never reported as a clean pass. A report
with no tests *and* no errors returns a `warning` telling the caller to treat it
as a failure.

### MCP tools

| Tool | Purpose |
| --- | --- |
| `fetch_report` | Download/extract a report and return its deep link. |
| `list_failed_tests` | List failing tests from a fetched report or an existing `report_dir`. |
| `create_artifact_run` | Allocate a collision-free visual/E2E artifact run (requires `repo_path`). |
| `find_artifact_runs` | Query the artifact run index (requires `repo_path`). |
| `check_artifact_budget` | Report local or per-run CI artifact usage against the budget (requires `repo_path`). |

The three artifact tools implement the [`e2e-artifacts`](../e2e-artifacts/SKILL.md)
standard; see that skill for the layout, discovery layers, and budget contract.
All three require an explicit `repo_path` — the server's cwd is never a safe
default for a repo-scoped write.

Natural-language prompts that map onto these tools:

- "Open the Playwright report for run 123456789."
- "Show me the failed tests from the last CI run on commit abc1234."
- "Fetch the report and jump to the Login Flow test."

## 10. Provider seam

`scripts/fetch_report.py` selects an implementation from `PROVIDERS`.
`GitHubActionsProvider` (backed by the `gh` CLI) is the only implementation
today. To add another CI system, subclass `CIProvider`, implement
`resolve_run_id`, `find_artifact` and `download_artifact`, and register it in
`PROVIDERS`. The CLI and MCP pick it up through `--provider` / `provider`.

## 11. Playwright SPA deep links

The HTML report is a single-page app. Deep links are hash routes on
`index.html`:

- `#?q=<urlencoded search>` — filters by test title
- `#?testId=<id>` — opens a specific test

Always URL-encode the search term (`urllib.parse.quote` / `encodeURIComponent`).

## 12. Authoring standard: locators, waiting, flake debugging

Portable guidance that applies to any Playwright suite. Artifact placement lives
in the sibling `e2e-artifacts` skill and is not repeated here.

**Core principle.** Test what users see and do. If a user cannot find an element
by its role or text, neither should the test.

**Quality layers.** Reliable (no flakes) → Fast (parallel, minimal waits) →
Maintainable (survives refactors).

**The process.** Identify the user behavior → locate the element the way a user
would → act → assert on a visible outcome → stabilize async behavior.

### 12.1 Locator priority ladder

Work down the ladder and stop at the first rung that fits. A lower rung is an
escape hatch, not a shortcut.

| Priority | Locator | Use for | Example |
| --- | --- | --- | --- |
| 1 | `getByRole` | Buttons, links, inputs, headings, lists, dialogs, tables | `getByRole('button', { name: 'Submit' })` |
| 2 | `getByLabel` | Form inputs with a label | `getByLabel('Email address')` |
| 3 | `getByPlaceholder` | Inputs whose only affordance is placeholder text | `getByPlaceholder('Search...')` |
| 4 | `getByText` | Static text, paragraphs, notices | `getByText('Welcome back')` |
| 5 | `getByAltText` | Images | `getByAltText('Company logo')` |
| last | `getByTestId` | No semantic role and no stable text | `getByTestId('cart-total')` |

```ts
// Scope and disambiguate instead of reaching for CSS.
await page.getByRole('dialog', { name: 'Confirm deletion' })
          .getByRole('button', { name: 'Confirm' }).click();

await page.getByRole('listitem').filter({ hasText: 'John' }).click();
await page.getByRole('row').filter({ has: page.getByRole('button', { name: 'Edit' }) }).click();
```

Reach for `data-testid` only when the element has no semantic role (a decorative
container), its text is genuinely dynamic (a computed price, a generated id), or
position among identical siblings is the thing under test. Prefer scoping a
semantic locator over adding a test id: `getByRole('region', { name: 'Cart' }).getByTestId('total')`.

### 12.2 Waiting and assertions

Playwright auto-waits for actionability on `click`, `fill`, `check`, and
`selectOption`. `expect()` assertions auto-retry until timeout. That is the
waiting mechanism — use it.

```ts
// Wait for a specific response, then assert on the rendered result.
const response = page.waitForResponse(r => r.url().includes('/api/users') && r.status() === 200);
await page.getByRole('button', { name: 'Refresh' }).click();
await response;
await expect(page.getByRole('list')).toContainText('John');

// Click and wait simultaneously when the request is triggered by the click.
await Promise.all([
  page.waitForResponse(r => r.url().includes('/api/data')),
  page.getByRole('button', { name: 'Submit' }).click(),
]);
```

Use `expect.soft()` when several independent facts should be reported together
instead of aborting on the first. Use `addLocatorHandler()` for an overlay that
may or may not appear (cookie consent, a tour popup) rather than sprinkling
defensive clicks through the test.

### 12.3 Flake and race debugging

A flaky test is a defect in the test or in the app's observable contract. Retries
hide it; they do not fix it.

| Symptom | Likely cause | Debug step |
| --- | --- | --- |
| Element not found | Wrong locator | Inspect the live DOM; confirm the role and accessible name |
| Element not found intermittently | Race — asserting before data arrives | Wait for the response that renders it, then assert |
| Timeout on click | Element obscured or animating | Capture a screenshot immediately before the action |
| Passes locally, fails in CI | Viewport, timing, or missing seed data | Compare viewport and setup; check the trace |
| Passes alone, fails in the suite | Shared mutable state | Give the test its own data; remove ordering assumptions |
| Assertion sees stale text | No auto-retry used | Replace a manual read-then-`expect` with `await expect(locator)…` |

Debug locally with `npx playwright test --debug` (Inspector) and `await page.pause()`.
Debug CI with the trace: `npx playwright show-trace trace.zip` replays DOM
snapshots, network, and console. Log what you need with `page.on('console' | 'request' | 'response' | 'requestfailed')`.
When a locator is suspect, check `await locator.count()` and `await locator.textContent()`
before blaming timing.

Before committing, run the new test several times locally. A test that passes
once has not been shown to be stable.

### 12.4 Anti-pattern red flags

Stop and rewrite when you see any of these.

| Red flag | Why it fails | Instead |
| --- | --- | --- |
| `page.locator('.btn-primary')` | Breaks on a class rename, proves nothing about the user | `getByRole('button', { name: … })` |
| `page.locator('#email-input')` | Coupled to markup ids | `getByLabel('Email')` |
| `page.locator('div > span:nth-child(2)')` | Brittle structure snapshot | Semantic locator, or add a test id |
| `waitForTimeout(1000)` | Arbitrary, slow, still flaky | Wait for the specific condition |
| `waitForLoadState('networkidle')` everywhere | Slow and unreliable with polling | Wait for the specific response |
| Manual read-then-`expect` | No auto-retry | `await expect(locator)…` |
| `getByTestId` as the first choice | Misses accessibility regressions | Climb the ladder first |
| Unscoped locator with several matches | Clicks the wrong element | `.filter()`, `.first()`, or scope with a parent |
| Assertions inside page objects | Hides intent, hard to debug | Page objects act; tests assert |
| One test asserting several unrelated behaviors | A failure names nothing useful | One behavior per test |
| `test.only` committed | Silently disables the rest of the suite | `forbidOnly: !!process.env.CI` |
| A retry added instead of a fix | Debt compounds | Fix the root cause; track it if deferred |
| Creating all test data through the UI | Slow and cascading failures | Create data via the API in a fixture |
| Logging in through the UI in every test | Multiplies runtime | Reuse `storageState` (see §6) |
