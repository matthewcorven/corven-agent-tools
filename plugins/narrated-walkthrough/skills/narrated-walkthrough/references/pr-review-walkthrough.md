# PR Review Walkthrough

Use this recipe when presenting a **spoken, visual pull request review** to the
Architect: continuity, momentum, already-completed validation, an interactive
Architect HUD, and A2UI v1.0 action choices.

It composes the existing `corven-agent-tools` plugins rather than replacing any
of them:

| Step | Plugin / Tool | What it produces |
| --- | --- | --- |
| 1. Diff lineage | `change-learn` (`lineage.py summary`) / `branch-diff` | Structured area breakdown, commit list, net add/delete counts, risk flags |
| 2. Real code review | Direct diff + LSP consumer check (new public APIs, security, invariants) | Concrete findings (blocking vs. non-blocking) and what stayed untouched |
| 3. Retrieved CI proof | `playwright-report-retrieval` (`fetch_report`, `list_failed_tests`) + `gh pr checks` | Actual parsed test counts, artifact IDs, expiry dates, CI budget — never a badge alone |
| 4. Artifact run dir | `e2e-artifacts` (`artifacts.py run --phase planning --spec pr-<N>-review-walkthrough`) | Collision-free outside-Git directory under `<repo>-artifacts/` |
| 5. Synchronized deck | `narrated-walkthrough` (`build_narration.py`, `anchor_scene` + `temperature: 0.0`) | Consistent-voice WAV clips, derived clock, offline inlined Mermaid, Architect HUD, A2UI v1.0 choices, sticky minimal media controls |
| 6. Browser check | Browser canvas / Playwright (`window.state()`, `window.seek(t)`) | Verified SVG rendering, sticky-header compact/uncompress scroll toggle, boundary crossing, `hudShown` & `askShown` |

## Architect-Assist Tone & Framing

Do **not** frame a review like a ticket queue ("What is your verdict on pull
request N?"). Frame it as an executive **Architect-assist** briefing:

1. **Lead with continuity, momentum, and already-completed validation:**
   explain what epic/workstream the PR continues, whether we are ahead or behind,
   and what is already validated against concrete checks and test suites.
2. **Surface the combined Architect HUD (`hud`):** expose high-level stats &
   trends, oversight depth (`Executive` / `Lead & Nodes` / `Full Trace`), direct
   `"Chat with <agent>"` handoffs for the Lead agent, individual specialist
   nodes, and the optional **walkthrough video agent**, plus linked in-repo
   conversation artifacts.
3. **Deliver choices as A2UI v1.0 JSON (`a2ui`):** emit valid A2UI v1.0
   messages (`createSurface` + `updateComponents` mapped to the consuming repo's
   common UI building blocks) both inside the player's action surface and in the
   accompanying chat reply.

## Sticky-Header Media Continuity

When the Architect scrolls down past the uncompressed media bar into slides or
the HUD, the sticky header automatically reveals a minimal Play/Pause toggle and
compact progress bar; scrolling back near the top hides the minimal controls and
returns the uncompressed bar cleanly in place.

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
  the human's explicit decision.
