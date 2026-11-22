# Source case and transfer boundaries

The source is `matthewcorven/corven-agents`, commit `a172cc0`, under `docs/proposal-2/`.
The original survey pinned source revision `439dee711e27d6d7de20cf2d8bdc2ba68ab26134`.
These references are provenance, not runtime dependencies of this skill.

## Observed sequence

The author read README, all eight original `docs/` documents (1,829 lines), the team roster and
relevant charters/policies. It separated the implemented spike from accepted but unbuilt requirements.
It presented a four-slide opening with three outcome options before expanding the proposal.
The question interface reported the Architect unavailable; recommendations remained explicitly
unaccepted rather than being presented as decisions.

The finished artifact contained **48 slides and 37 locally rendered diagrams**, using Marp,
Mermaid and PlantUML. It covered PRD, L0 architecture, stores, individual/collective team use cases,
state/trigger families, inner gates, human handoffs, change at rest, failure/recovery, and delivery
evidence. The provisional recommendation was a calendar-first vertical slice.

Playwright checked all slides at **1440x1000, 390x844 and 216x713**, diagram decode/text, overflow,
offline operation, controls/source download, live updates and failure retention. Negative mutations
removed a diagram and corrupted SVG output. Rendering evidence was recorded separately from product
acceptance. The user praised the result and explicitly requested this reusable skill.

## Transfer the method, not the domain

| Keep | Do not universalize |
| --- | --- |
| Full source survey with evidence/authority distinctions | Eight documents, 48 slides or 37 diagrams as targets |
| Outcome-first options and thin end-to-end delivery | A calendar workflow as every project's best MVP |
| Deterministic checks for exact rules and authority | The original hub topology, store choices or deployment forge |
| Explicit change-at-rest / failure semantics where relevant | The original role names, device restrictions or lifecycle enum |
| Live focus with one real human decision at a time | Assuming the human answered when the tool was unavailable |
| Local rendering, offline artifact, real browser inspection | Assuming a successful compile makes diagrams readable |

The original prompt retains its project-specific names because the user requested a verbatim example.
The golden deck instead uses a fictional **release-note review** workflow. It illustrates the same
decomposition without requiring access to the source repository or exposing its internal design as
instructions for other agents.
