# Repository instructions

## Pre-push gate

Never push without first validating that all unit tests are green. Run the full
suite before every `git push`, and state the green result explicitly:

```bash
python3 -m unittest discover -s plugins/github-issues/skills/issue-work-queue/tests -v
```

## SKILL.md frontmatter

- Every `plugins/*/skills/*/SKILL.md` needs `name` and `description`; the
  description must use a folded block scalar (`>-`).
- Descriptions must not contain `": "` except inside the routing markers
  `USE FOR:` and `DO NOT USE FOR:`. The unit tests enforce this across all
  skills — prefer em dashes elsewhere.
