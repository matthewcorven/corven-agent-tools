# Visual proposal

A portable skill for turning expansive reference material into a **smallest-useful-outcome proposal**,
discussed visually with the decision maker. Works for a project, goal, epic, feature, process or policy;
it does not require agents, Squad, a particular language, or an existing PRD.

```shell
copilot plugin install visual-proposal@corven-agent-tools
```

The marketplace must first be registered with
`copilot plugin marketplace add matthewcorven/corven-agent-tools`.
Repository edits do not update an installed plugin. Installation is a separate, explicit action;
restart the harness afterward to load the skill.

## What ships

| Artifact | Purpose |
| --- | --- |
| [SKILL.md](skills/visual-proposal/SKILL.md) | Routing, delivery contract and incremental authoring loop |
| [Authoring method](skills/visual-proposal/references/authoring-method.md) | Evidence decomposition, diagram selection, coverage and minimal-text editing |
| [Rendering guide](skills/visual-proposal/references/rendering.md) | Local build, live focus, offline delivery, rendering traps and verification |
| [Original prompt](skills/visual-proposal/references/original-prompt.md) | The user's two-paragraph request, verbatim, including spelling and spacing |
| [Case study](skills/visual-proposal/references/case-study.md) | Provenance, observed outcomes, what to generalize and what not to inherit |
| [Golden example](skills/visual-proposal/examples/golden/README.md) | Runnable Marp/Markdown/Mermaid/PlantUML example with a single-file HTML build and Playwright checks |

This is **not** `change-learn`: that plugin explains a source-control change window. This skill
reconsiders outcomes, scope and design from reference material before implementation.
It preserves existing authority and labels recommendations as proposals, not accepted changes.

Try: "Use visual-proposal to survey our docs and present the smallest useful release. Keep a live
Marp deck focused on the decision we're discussing. Show architecture, use cases, failure states
and human handoffs; ask one high-impact question at a time."

## Development

```shell
python3 -m unittest discover -s plugins/visual-proposal/tests -v
cd plugins/visual-proposal/skills/visual-proposal/examples/golden
npm ci
npx playwright install chromium
npm run setup:plantuml
npm run build
npm test
```

Java 21+ and Node 22+ are build prerequisites. The setup command downloads only the pinned official
PlantUML jar and verifies its SHA-256; it never uploads diagram text. The generated `index.html`
works offline without Node, Java, a server, or external fonts. Generated output, dependencies,
renderer caches and browser evidence are ignored, not distributed as source.
