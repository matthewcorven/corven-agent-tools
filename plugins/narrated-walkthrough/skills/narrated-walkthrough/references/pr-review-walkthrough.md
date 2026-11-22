# PR Review Walkthrough

Use this recipe when a user asks for a **spoken, visual pull request review**
that walks through what changed, what was checked, what CI actually proved, and
ends on an explicit merge/review verdict.

It composes the existing `corven-agent-tools` plugins rather than replacing any
of them:

| Step | Plugin / Tool | What it produces |
| --- | --- | --- |
| 1. Diff lineage | `change-learn` (`lineage.py summary`) / `branch-diff` | Structured area breakdown, commit list, net add/delete counts, risk flags |
| 2. Real code review | Direct diff + LSP consumer check (new public APIs, security, invariants) | Concrete findings (blocking vs. non-blocking) and what stayed untouched |
| 3. Retrieved CI proof | `playwright-report-retrieval` (`fetch_report`, `list_failed_tests`) + `gh pr checks` | Actual parsed test counts, artifact IDs, expiry dates, CI budget — never a badge alone |
| 4. Artifact run dir | `e2e-artifacts` (`artifacts.py run --phase planning --spec pr-<N>-review-walkthrough`) | Collision-free outside-Git directory under `<repo>-artifacts/` |
| 5. Synchronized deck | `narrated-walkthrough` (`build_narration.py`, `anchor_scene` + `temperature: 0.0`) | Consistent-voice WAV clips, derived clock, offline inlined Mermaid, captions, `#ask` panel |
| 6. Browser check | Browser canvas / Playwright (`window.state()`, `window.seek(t)`) | Verified SVG rendering, boundary crossing, `askShown: true` |

## Recommended 6-Scene Arc

Keep one claim per scene (~12–20 s each; ~90–110 s total):

1. **Verdict & footprint** *(scoreboard)* — Net file/line change, CI pass summary, upfront recommendation (Approve & merge vs. Request changes).
2. **What changed** *(before/after)* — The structural move, not a file-by-file changelog.
3. **Key mechanism / guard** *(sequence)* — The most important new invariant, contract, or safety check and how it fails closed.
4. **Risk, scope & findings** *(contrast / scoreboard)* — Blocking defects (if any), non-blocking notes, and what stayed deliberately untouched.
5. **Verified CI evidence** *(scoreboard)* — Retrieved Playwright/unit test totals, budget numbers, and zero-failure confirmation.
6. **Your verdict** *(top-down decision)* — Explicit spoken + visual invitation matching the `disposition` block.

## Mandatory Disposition Contract (PR Verdict)

A pull request review always ends on a human decision, so `disposition` is
required and must match across all three surfaces (final scene narration, slide
`#ask` panel, and accompanying chat reply):

```json
"disposition": {
  "kind": "pr_verdict",
  "prompt": "What is your verdict on pull request #353?",
  "options": [
    "Approve and merge PR #353",
    "Post review comment only (keep open)",
    "Inspect specific files or findings first",
    "Park it for now"
  ],
  "note": "Any answer works, including requesting specific edits or no answer at all. Type it in the chat, or use the microphone."
}
```

## Voice & Hygiene Rules

- **Consistent speaker across every scene:** pin a real server voice (e.g.
  `US_James` on `Ming-3.0-tts-tiny-4bit`), set `tts.anchor_scene` to the first
  scene id, and pass `language` + `temperature: 0.0`.
- **Keep raw coordinates in chat, not narration:** file paths, line numbers,
  commit SHAs, and clickable GitHub/Actions URLs belong in the accompanying chat
  summary; narration speaks plain English claims and human-readable numbers.
- **Hard guardrail — review does not authorize:** a PR review walkthrough
  explains and recommends. Never merge, approve, dismiss reviews, or post
  GitHub comments automatically as part of building the walkthrough; wait for
  the human's explicit verdict.
