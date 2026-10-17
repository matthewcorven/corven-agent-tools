# Routing rules

The coordinator applies these rules in order; first match wins.

1. Request mentions analysis, churn, lineage, "what changed", refs, staged,
   working tree, or untracked → **lineage-analyst**.
2. Request mentions diagram, Mermaid, PlantUML, flowchart, pie, gitGraph,
   architecture picture → **diagrammer** (must receive the analyst's JSON first;
   if absent, route to lineage-analyst before diagrammer).
3. Request mentions slides, deck, Marp, presentation, PDF export, narrative →
   **deck-author** (same precondition: analyst JSON first).
4. Mixed requests are decomposed in the order analyst → diagrammer → deck-author.
5. Anything else → answer directly as coordinator, and record a decision noting
   the gap in routing coverage.

The coordinator never writes diagrams or slides itself.
