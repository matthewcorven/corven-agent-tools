# Repository instructions

## Pre-push gate

Never push without first validating that all unit tests are green. Run the full
suite before every `git push`, and state the green result explicitly:

```bash
python3 -m unittest discover -s plugins/github-issues/skills/issue-work-queue/tests -v
```

## Local plugin version changes

Bumping a plugin version only changes this repository — every developer's AI
harness keeps running the copy already installed under
`~/.copilot/installed-plugins/corven-agent-tools/<plugin>/`. Treat reinstall as
an explicit follow-up, never as an implicit side effect of editing files.

When a change modifies any of these, the local install is stale:

- `version` in `plugins/<plugin>/plugin.json`
- `version` for that plugin in `.github/plugin/marketplace.json`
- `SERVER_VERSION` in a plugin's `server.py`

**Required behavior after such a change is merged** (or when working on the
branch that contains it): ask the developer whether to install the new version
before considering the work finished. Do not install silently, and do not skip
the question because the harness happens to work.

1. **Ask first.** One question, no bundling: install the new version now?
   Recommended when the change altered tools, schemas, or skill behavior.
2. **On approval, update.** The marketplace must already be registered
   (`copilot plugin marketplace add matthewcorven/corven-agent-tools`):

   ```bash
   copilot plugin update <plugin>@corven-agent-tools
   ```

   Use `copilot plugin install <plugin>@corven-agent-tools` for a plugin the
   developer has never installed.
3. **Offer to stop stale MCP servers.** If that plugin ships an MCP server, an
   instance started by the previous version can still be alive and will keep
   serving the old tools to any harness that already has it open. Ask whether
   to stop those instances now — this is its own question, not part of the
   install approval — and only stop them once the developer has approved it.

   Report the instances and let the developer close them, or stop a single
   approved instance by PID from the list. Never kill processes on your own,
   and never use `pkill`, `killall`, or any name-based match: other plugins and
   unrelated processes share the `server.py` name.
4. **Confirm the outcome.** Reinstall takes effect only in a new harness
   session; installed copies live under
   `~/.copilot/installed-plugins/corven-agent-tools/<plugin>/`. Tell the
   developer to reopen or restart the harness so the new skills, scripts, and
   MCP tools are what the session actually loads, and note that
   `copilot plugin list` reflects the installed version.

A developer may decline. Record the decline and the exact command to run
later, and leave the working tree untouched.

## SKILL.md frontmatter

- Every `plugins/*/skills/*/SKILL.md` needs `name` and `description`; the
  description must use a folded block scalar (`>-`).
- Descriptions must not contain `": "` except inside the routing markers
  `USE FOR:` and `DO NOT USE FOR:`. The unit tests enforce this across all
  skills — prefer em dashes elsewhere.
