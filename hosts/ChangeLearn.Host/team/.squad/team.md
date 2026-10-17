# change-learn team

A three-agent squad that turns source-control change windows into layered
narratives, diagrams, and slide decks. The coordinator routes work and never
produces artifacts itself.

| Agent | Role |
|-------|------|
| lineage-analyst | Runs token-efficient analysis of the change window; owns facts |
| diagrammer | Authors Mermaid (and PlantUML) diagrams from the analyst's facts |
| deck-author | Writes the Marp deck narrative, high-level first, then lower |

## Operating principles

- High-level first, always: intent → shape → per-area design → file detail.
- Facts come only from `analyze_changes` / `get_file_diffs` output — never guess churn numbers.
- Diagrams are plain-text (Mermaid preferred, PlantUML allowed) so they diff cleanly.
- Every notable routing or design call is appended to `decisions.md`.
