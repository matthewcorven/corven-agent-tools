---
name: squad-model-resolver
description: >-
  MANDATORY before spawning ANY Squad agent/sub-agent session via task when the
  target model is set, provider-qualified (provider://…), or non-default — call
  resolve_for_agent first, pass only the returned model_id, and run verify_spawn
  after. Resolver tool availability and permission are host-controlled; never
  infer denial from an absent repository .copilot/config.json. If the host does
  not expose or explicitly denies the resolver, report that observed condition.
  Never guess or hand-construct model IDs. If status is not resolved, stop and
  report; do not fall back or substitute.
---

# Squad Model Resolver

MANDATORY before spawning ANY Squad agent/sub-agent session via `task` when the target model is set, provider-qualified (`provider://…`), or non-default: call `resolve_for_agent` first, pass only the returned `model_id`, and run `verify_spawn` after. Never guess or hand-construct model IDs. If status is not `resolved`, stop and report; do not fall back or substitute.

## Upgrading the plugin

The resolver MCP server must be restarted after an upgrade. A harness that
already has the server open keeps the old tool schemas and resolution rules;
restart the harness (or force-quit the stale server process) before expecting
the new version to resolve models.

## Tool Availability and Permission

The runtime's exposed tools and its permission response are authoritative for the current session. Do not inspect a repository `.copilot/config.json` or infer that a missing file means the resolver is denied.

For GitHub Copilot CLI specifically, MCP server declarations and tool approvals are separate. The CLI stores saved tool approvals per repository/directory in `permissions-config.json` under its selected configuration directory (default `~/.copilot/permissions-config.json`; `COPILOT_HOME` or `--config-dir` can change it). `~/.copilot/config.json` is internal application state, not the CLI tool-approval file. Do not open or modify permission files to grant access automatically. If the resolver tool is explicitly denied by the runtime, report that observed denial and let the user approve it through the host's supported controls. A missing tool after discovery is a tool-availability blocker, not evidence of denial.

## Protocol for Squad Coordinator

1. **Tool Discovery:**
   If the resolver tools (`resolve_for_agent`, `verify_spawn`) are deferred or not yet loaded in context, search for and load them using `tool_search_tool` with query `"squad-model-resolver"`. If the tools are exposed and usable in this runtime, proceed without a repository-file precheck.

2. **Before spawn:**
   The coordinator must inspect `.squad/config.json`, the agent charter (`## Model`), and session directives. Call `resolve_for_agent` with:
   - `repo_path`: Root directory containing `.squad/`.
   - `agent_name`: Lowercase cast name of the agent to spawn.
   - `catalog`: List of model IDs exposed by the harness.
   - `session_directive`: Optional session-level override (if specified by user).

3. **Gating:**
   - If `status == "resolved"`: Proceed with `model_id`. When `model_id` is null (platform default fallback), omit the `model` parameter.
   - If `status != "resolved"` (`"unresolvable"` or `"ambiguous"`): **STOP**. Report failure reason. Do not guess or substitute.

4. **Post-spawn verification:**
   - Call `verify_spawn` with `selected_model_id` and the reported model ID from the spawned session.
