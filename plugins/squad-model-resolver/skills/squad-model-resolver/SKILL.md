---
name: squad-model-resolver
description: >-
  MANDATORY before spawning ANY Squad agent/sub-agent session via task when the
  target model is set, provider-qualified (provider://…), or non-default — call
  resolve_for_agent first, pass only the returned model_id, and run verify_spawn
  after. If the MCP is not pre-approved in .copilot/config.json, inform the user
  before spawning that model selection cannot be applied per
  .squad/templates/model-selection-reference.md because the MCP is not approved.
  Never guess or hand-construct model IDs. If status is not resolved, stop and
  report; do not fall back or substitute.
---

# Squad Model Resolver

MANDATORY before spawning ANY Squad agent/sub-agent session via `task` when the target model is set, provider-qualified (`provider://…`), or non-default: call `resolve_for_agent` first, pass only the returned `model_id`, and run `verify_spawn` after. Never guess or hand-construct model IDs. If status is not `resolved`, stop and report; do not fall back or substitute.

## Pre-Approval Verification

Before attempting invocation or spawning subagents, verify that `squad-model-resolver-*` tools are pre-approved in `.copilot/config.json` (under `tool_approvals` or `allowedTools`).
- If the MCP is NOT pre-approved in `.copilot/config.json`: before spawning subagents, the coordinator MUST explicitly inform the user that model selection cannot be applied per `.squad/templates/model-selection-reference.md` because the MCP is not approved.
- If pre-approved: proceed with the standard resolution protocol below.

## Protocol for Squad Coordinator

1. **Tool Discovery:**
   If the resolver tools (`resolve_for_agent`, `verify_spawn`) are deferred or not yet loaded in context, search for and load them using `tool_search_tool` with query `"squad-model-resolver"`.

2. **Pre-Approval Check:**
   Check `.copilot/config.json` in the target repository. If the `squad-model-resolver` tools are not pre-approved, inform the user immediately that model selection cannot be applied per `.squad/templates/model-selection-reference.md` because the MCP is not approved.

3. **Before spawn:**
   The coordinator must inspect `.squad/config.json`, the agent charter (`## Model`), and session directives. Call `resolve_for_agent` with:
   - `repo_path`: Root directory containing `.squad/`.
   - `agent_name`: Lowercase cast name of the agent to spawn.
   - `catalog`: List of model IDs exposed by the harness.
   - `session_directive`: Optional session-level override (if specified by user).

4. **Gating:**
   - If `status == "resolved"`: Proceed with `model_id`. When `model_id` is null (platform default fallback), omit the `model` parameter.
   - If `status != "resolved"` (`"unresolvable"` or `"ambiguous"`): **STOP**. Report failure reason. Do not guess or substitute.

5. **Post-spawn verification:**
   - Call `verify_spawn` with `selected_model_id` and the reported model ID from the spawned session.
