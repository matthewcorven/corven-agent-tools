# Standing directives

- Never dump raw `git diff` output into context; use `analyze_changes` first and
  `get_file_diffs` only for the handful of files that matter.
- Respect `.change-learn.json` at the repo root: `focus` ranks first, `ignore`
  is excluded, `context_files` are read before narrating.
- Summaries follow the narrative ladder: intent → shape → area design → file
  detail. Never lead with file lists.
- Diagrams must be plain-text syntax (Mermaid default; PlantUML when sequence
  detail demands it). No binary images.
- Slide decks follow Marp conventions (`marp: true` frontmatter, one idea per
  slide, `<!-- ai-fill -->` markers resolved before delivery).
- Decisions with lasting effect are appended to `decisions.md` — never edited.
