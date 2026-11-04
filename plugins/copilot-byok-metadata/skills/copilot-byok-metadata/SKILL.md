---
name: copilot-byok-metadata
description: >-
  Use when the user wants GitHub Copilot desktop app BYOK models to report
  correct context-window sizes, output-token budgets, or reasoning-effort
  levels — for example when compaction triggers too early, when the reasoning
  picker is missing or empty for an OpenRouter model, or when the user asks to
  sync, refresh, or repair BYOK model metadata. Reads the app database, fetches
  authoritative values from the provider source-of-truth API or a local oMLX
  server, and applies them interactively or automatically. USE FOR: BYOK
  context window, BYOK reasoning effort, reasoning levels missing, compaction
  too aggressive, sync model metadata, refresh BYOK models, OpenRouter model
  limits, oMLX model limits, local MLX context window, provider_models
  metadata. DO NOT USE FOR: GitHub Copilot CLI configuration (not yet
  supported), built-in Copilot models, adding or removing BYOK providers, or
  editing API keys.
---

# Copilot BYOK Metadata

Keeps GitHub Copilot desktop app BYOK models honest about what they can do. The
app stores BYOK models with `max_prompt_tokens`, `max_output_tokens`, and
`supported_reasoning_efforts` all `NULL`, so it cannot size the context window
or offer reasoning levels. This skill fills those columns from the provider's
own source of truth.

## Scope

- **Supported target:** GitHub Copilot **desktop app** (`~/.copilot/data.db`).
- **Supported providers:** OpenRouter (public catalog) and a local **oMLX**
  server (queried at the provider's configured base URL). Other providers are
  reported as `unsupported_provider` and left untouched.
- **Reasoning levels:** derived only from providers that advertise a level set.
  oMLX accepts reasoning-effort values but does not publish a per-model level
  set, so oMLX models are planned with no reasoning efforts and any existing
  user selection is left alone. Say so instead of inventing levels.
- **Not supported yet:** GitHub Copilot CLI. If the user asks for CLI support,
  say it is planned and stop — do not improvise a CLI write path.

## Upgrading

If you just updated this plugin, an MCP server process started by an earlier
version may still be running. A harness that already has the server open keeps
serving the old tools until it shuts that process down: restart the harness (or
force-quit the stale server process) before expecting the new version's tools
and behavior.

## Tools

| Tool | Purpose |
|------|---------|
| `read_byok_config` | Full awareness in one call — providers, models, metadata, selections, gaps |
| `list_byok_models` | Read configured providers/models and their current metadata |
| `fetch_provider_metadata` | Fetch source-of-truth values and show what Copilot would get |
| `plan_metadata_updates` | Dry-run diff of current vs proposed — never writes |
| `apply_metadata_updates` | Write the plan; `dry_run` defaults to `true` |
| `get_provider_registry` | Show the hardcoded provider registry and supported targets |

If these tools are deferred, load them with `tool_search_tool` using the query
`copilot-byok-metadata` before proceeding.

## oMLX source

oMLX is a local inference server, so its "catalog" is whatever server the
provider's `baseUrl` points at. The server queries:

- `GET <baseUrl>/v1/models` — `id` and `max_model_len` (the effective context
  window, a vLLM-style extension).
- `GET <baseUrl>/v1/models/status` — the same window as `max_context_window`,
  plus `model_context_length` (discovered native length) and `max_tokens` (the
  per-model generation ceiling). Status wins when both are present.

oMLX resolves the effective window in this order: a per-model override, then
the model's discovered native context (optionally clamped by an operator
policy), then the oMLX app default. `provider_metadata.context_source` reports
which tier supplied the value. If the status endpoint is unavailable the
`/v1/models` value is used instead.

A provider is treated as oMLX when its name contains "omlx" and it has a base
URL. Providers without a matching registry entry stay `unsupported_provider`.

## Upgrading the plugin

This plugin ships an MCP server. After an upgrade, a server process started by
the previous version can still be alive — a harness session that already has it
open keeps calling the old tools and will not see new tools or behavior. Shut
that instance down (restart the AI harness, or force-quit the stale `server.py`
process) before concluding the upgrade did not take effect.

## Protocol

1. **Read.** Call `read_byok_config` with the user's workspace path and no
   other arguments — `include_catalog_cache` already defaults to `true`, and
   the catalog cache is where the app actually reads a BYOK model's context
   window. It returns every provider (credentials redacted), every model with
   its stored metadata, the user's selected reasoning effort and context tier,
   and which fields are still missing. Report the provider count, model count,
   and how many models are missing metadata.

   Also read `catalog_cache.byok_entries_with_limits` and
   `catalog_cache.caches`. `provider_models` has no context-window column, so a
   model can have full metadata in the database and still show the app's
   fallback window because the cache was never patched. If
   `byok_entries_with_limits` is `0` while `summary.models_with_metadata` is
   greater than `0`, the cache is stale — that is the state to repair, and
   `notes` will say so. If every model has metadata *and* every BYOK cache
   entry has limits, say so and stop; there is nothing to do.

   BYOK configuration is app-global, not per-workspace. The `workspace_path`
   input only identifies the calling project and workspace for context; never
   claim the config is scoped to that workspace.

2. **Plan.** Call `plan_metadata_updates`. This queries every provider that has
   a source of truth — OpenRouter's catalog, and each configured oMLX server at
   its own `base URL + /v1/models` (enriched by `/v1/models/status`) — and
   returns a per-model diff with `status` of `update`, `unchanged`, `unresolved`,
   or `unsupported_provider`. Read `warnings` too: an oMLX server that is not
   running is reported there and its models come back `unresolved` rather than
   failing the whole plan. Tell the user to start oMLX and re-run if that
   happens.

3. **Present.** Show the user a compact table of models with `status == "update"`:
   model id, proposed context window, proposed output budget, and reasoning
   levels. Read the numbers from `proposed` — never from `provider_metadata`.
   `provider_metadata.provider_max_output_tokens` is the provider's raw aggregate
   maximum (often 900k+), not the budget that gets written; showing it will
   mislead the user. For oMLX, `provider_metadata.context_source` says whether
   the window came from the server's per-model override, the discovered model
   native length, or the oMLX app default — mention it when the user asks where
   the number came from. oMLX models have no reasoning levels: show that column
   as "n/a (oMLX)" rather than listing invented values. Mention `unresolved`
   models by name — those ids are absent from the provider catalog and need the
   user's attention. Do not list `unsupported_provider` models individually;
   summarize them as "N models on providers without a source of truth".

4. **Decide.** Choose interactive or automatic per the rule below.

5. **Apply.** Call `apply_metadata_updates` with `dry_run=false`. Leave `backup`
   and `refresh_catalog_cache` at their defaults of `true` unless the user
   explicitly declines them — the app reads the context window from the cached
   catalogs, not from `provider_models`, so without the cache patch the model
   keeps its fallback window and compaction still fires early. Both catalogs
   (`copilot-available-models-v2` and `copilot-cloud-available-models-v2`) are
   patched; `applied.catalog_caches_patched` reports each one.

6. **Report.** State how many models were updated, the backup path, and that the
   user should reopen the model picker (or restart the app) to see the change.
   List the applied `max_prompt_tokens` / `max_output_tokens` per model so the
   user can verify the outcome. If `applied.catalog_cache_entries_patched` is
   `0` while models were updated, surface the returned `WARNING` note verbatim —
   it means the app will not learn the new context window. Confirm the outcome
   by re-reading `catalog_cache.byok_entries_with_limits`; it should now equal
   the number of models you updated.

## Interactive vs automatic

Decide from the conversation, not from a guess:

- **Automatic** — apply without asking when the user is in autopilot mode, or
  when the current request already authorizes the write ("sync them", "fix it",
  "apply the metadata", "just do it"). A standing preference stated earlier in
  the session also counts.
- **Interactive** — present the plan and wait for confirmation when the user
  asked a question rather than issuing an instruction ("can you check…",
  "what's wrong with…"), when the plan would change more than a handful of
  models and the user has not seen a plan yet, or when the request is ambiguous.

When in doubt, ask. A dry run costs nothing; a wrong write costs a backup
restore.

## Optional behaviors

Both are off by default. Enable only when the user asks for them.

- **`set_reasoning_defaults`** — also writes a reasoning-effort selection for
  models that have no explicit user choice, using the provider's default effort.
  Without this, the app shows the levels but keeps its own default selection.
- **`set_context_tier`** — writes `default` or `long_context` for every resolved
  model. Only use when the user explicitly wants a tier applied; the app's
  meaning for `long_context` on BYOK models is not documented.
- **`refresh_catalog_cache`** — also patches the app's cached model catalogs so
  the new limits appear without a restart. **On by default; leave it on.** The
  app resolves a BYOK model's context window from these caches, and
  `provider_models` has no context-window column, so skipping it leaves the
  model on the app's fallback window. The app may overwrite the caches on its
  next refresh; re-run if `byok_entries_with_limits` drops back to `0`.

## Safety

- Never write to `~/.copilot/config.json` or `~/.copilot/settings.json`. The
  database is the only store this skill touches.
- Never run `apply_metadata_updates` with `dry_run=false` against a database the
  user has not seen a plan for, unless the automatic rule above applies.
- Never edit `provider_models` rows directly with SQL. The server owns the
  transaction, the backup, and the `updated_at` bookkeeping.
- If the app is running, writes still succeed (WAL mode), but tell the user to
  reopen the model picker so the UI re-reads the values.
