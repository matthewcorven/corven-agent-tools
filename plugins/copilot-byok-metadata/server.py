#!/usr/bin/env python3
"""copilot-byok-metadata MCP server.

Reads the GitHub Copilot desktop app's BYOK model configuration from
``~/.copilot/data.db``, fetches authoritative context-window and
reasoning-effort metadata from provider source-of-truth APIs (OpenRouter
only for now), and applies that metadata back to the app config.

The server is deliberately stdlib-only and speaks raw JSON-RPC 2.0 over
stdio, matching the other plugins in this repository.

Design notes
------------
* ``provider_models.id`` is a UUID primary key. The app derives per-model
  app-state keys as ``<provider_id>/<model_id>``; that derived form is what
  ``copilot-reasoning-effort`` and ``copilot-context-tier`` are keyed by.
* ``supported_reasoning_efforts`` is stored as a JSON array string. NULL
  means "this model has no reasoning effort", which is the app default.
* ``max_prompt_tokens`` is the *prompt* budget, not the total context
  window. The app's built-in catalog overwhelmingly satisfies
  ``max_prompt_tokens + max_output_tokens == max_context_window_tokens``,
  so that is the derivation used here.
* The app may be running while we write. Every write is a single
  transaction with a busy timeout, and a backup is taken first by default.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

SERVER_NAME = "copilot-byok-metadata"
SERVER_VERSION = "0.2.0"

DEFAULT_DB_PATH = os.path.join("~", ".copilot", "data.db")

#: Canonical Copilot ``ReasoningEffort`` union. Provider vocabularies are
#: intersected with this set, so provider-only values such as ``minimal``
#: and ``none`` are dropped.
COPILOT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Canonical Copilot ``ContextTier`` union.
COPILOT_CONTEXT_TIERS = ("default", "long_context")

#: Ceiling applied to provider-reported output budgets. The Copilot app's own
#: built-in catalog never exceeds 128000 output tokens, while some providers
#: (notably OpenRouter) report aggregate maxima such as 943718 for a 1M window.
#: Honouring those verbatim would leave only ~100k of prompt budget and force
#: compaction almost immediately, so the value is clamped here.
MAX_OUTPUT_TOKENS_CEILING = 131072

#: Fraction of the context window that must remain available for the prompt.
MIN_PROMPT_FRACTION = 0.5

APP_STATE_REASONING_KEY = "copilot-reasoning-effort"
APP_STATE_CONTEXT_TIER_KEY = "copilot-context-tier"
APP_STATE_CATALOG_KEY = "copilot-available-models-v2"
APP_STATE_CLOUD_CATALOG_KEY = "copilot-cloud-available-models-v2"
APP_STATE_SELECTED_MODEL_KEY = "copilot-selected-model"

#: Catalog caches the app keeps in ``app_state``. They differ in shape: the
#: local catalog is ``{"accountId": ..., "models": [...]}`` while the cloud
#: catalog is a bare JSON list. Both carry BYOK entries and both are patched.
CATALOG_CACHE_KEYS = (APP_STATE_CATALOG_KEY, APP_STATE_CLOUD_CATALOG_KEY)

#: Substrings that mark a provider setting as credential-bearing. Matching
#: values are replaced with ``<redacted>`` before anything leaves the server.
SECRET_SETTING_MARKERS = (
    "apikey",
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
)

#: Hardcoded provider source-of-truth registry. Only OpenRouter is wired up
#: today; the shape is intentionally extensible so additional providers (and
#: a future GitHub Copilot CLI target) can be added without changing tools.
PROVIDER_REGISTRY: dict[str, dict[str, Any]] = {
    "openrouter": {
        "display_name": "OpenRouter",
        "catalog_url": "https://openrouter.ai/api/v1/models",
        "endpoints_url_template": "https://openrouter.ai/api/v1/models/{model_id}/endpoints",
        "match_hosts": ["openrouter.ai"],
        "match_names": ["openrouter"],
        "docs_url": "https://openrouter.ai/docs/api-reference/list-available-models",
    },
}

HTTP_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def resolve_db_path(db_path: Optional[str] = None) -> str:
    """Resolve the Copilot app database path, expanding ``~``."""
    return os.path.abspath(os.path.expanduser(db_path or DEFAULT_DB_PATH))


def connect(db_path: str, readonly: bool = True) -> sqlite3.Connection:
    """Open the app database with a busy timeout suitable for a live app."""
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def load_providers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, type, settings_json, account_id FROM model_providers ORDER BY name"
    ).fetchall()
    providers = []
    for row in rows:
        settings: dict[str, Any] = {}
        raw = row["settings_json"]
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    settings = parsed
            except (TypeError, ValueError):
                settings = {}
        providers.append(
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "base_url": settings.get("baseUrl") or "",
                "wire_api": settings.get("wireApi"),
                "auth_kind": settings.get("authKind"),
                "account_id": row["account_id"],
                "settings": redact_settings(settings),
            }
        )
    return providers


def load_models(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, provider_id, model_id, wire_model, display_name,
               max_prompt_tokens, max_output_tokens, supported_reasoning_efforts
        FROM provider_models
        ORDER BY provider_id, model_id
        """
    ).fetchall()
    models = []
    for row in rows:
        models.append(
            {
                "row_id": row["id"],
                "provider_id": row["provider_id"],
                "model_id": row["model_id"],
                "wire_model": row["wire_model"],
                "display_name": row["display_name"],
                "state_key": f"{row['provider_id']}/{row['model_id']}",
                "max_prompt_tokens": row["max_prompt_tokens"],
                "max_output_tokens": row["max_output_tokens"],
                "supported_reasoning_efforts": parse_efforts(
                    row["supported_reasoning_efforts"]
                ),
            }
        )
    return models


def load_app_state(conn: sqlite3.Connection, key: str) -> Optional[Any]:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def load_app_state_scalar(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Read an app-state value that may be stored as a bare string or JSON."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    raw = row["value"]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return parsed if isinstance(parsed, str) else raw


def parse_efforts(raw: Optional[str]) -> Optional[list[str]]:
    """Parse the stored ``supported_reasoning_efforts`` JSON array string."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


def redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Return provider settings with credential-bearing values masked."""
    redacted: dict[str, Any] = {}
    for key, value in settings.items():
        lowered = key.lower()
        if any(marker in lowered for marker in SECRET_SETTING_MARKERS):
            redacted[key] = "<redacted>" if value else value
        else:
            redacted[key] = value
    return redacted


def resolve_workspace(conn: sqlite3.Connection, workspace_path: str) -> dict[str, Any]:
    """Map a filesystem path onto the app's project/workspace records.

    BYOK model configuration is app-global, so this only supplies context:
    which project and workspace the calling agent is working in. Matching is
    longest-prefix over ``projects.main_repo_path`` and
    ``workspace_checkout_bindings.checkout_path`` so worktrees resolve to
    their owning project.
    """
    target = os.path.abspath(os.path.expanduser(workspace_path))
    result: dict[str, Any] = {
        "workspace_path": target,
        "matched": False,
        "project": None,
        "workspace": None,
        "note": (
            "BYOK model configuration is app-global; workspace_path is used "
            "only to identify the calling project and workspace."
        ),
    }

    best_project: Optional[dict[str, Any]] = None
    best_len = -1
    try:
        rows = conn.execute(
            "SELECT id, name, main_repo_path, default_branch FROM projects"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    project_rows = list(rows)

    def _match_project(root: Optional[str]) -> Optional[dict[str, Any]]:
        if not root:
            return None
        root = os.path.abspath(root)
        if target == root or target.startswith(root + os.sep):
            return {"root": root}
        return None

    for row in project_rows:
        hit = _match_project(row["main_repo_path"])
        if hit and len(hit["root"]) > best_len:
            best_len = len(hit["root"])
            best_project = {
                "id": row["id"],
                "name": row["name"],
                "main_repo_path": hit["root"],
                "default_branch": row["default_branch"],
            }

    best_workspace: Optional[dict[str, Any]] = None
    best_ws_len = -1
    try:
        rows = conn.execute(
            """
            SELECT b.workspace_id, b.checkout_path, b.checkout_kind, b.repo_path,
                   w.name, w.branch, w.workspace_type
            FROM workspace_checkout_bindings b
            LEFT JOIN workspaces w ON w.id = b.workspace_id
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        checkout = row["checkout_path"]
        if not checkout:
            continue
        checkout = os.path.abspath(checkout)
        if target == checkout or target.startswith(checkout + os.sep):
            if len(checkout) > best_ws_len:
                best_ws_len = len(checkout)
                best_workspace = {
                    "id": row["workspace_id"],
                    "name": row["name"],
                    "branch": row["branch"],
                    "workspace_type": row["workspace_type"],
                    "checkout_path": checkout,
                    "checkout_kind": row["checkout_kind"],
                    "repo_path": row["repo_path"],
                }

    # A worktree checkout lives outside main_repo_path, so fall back to the
    # binding's repo_path to identify the owning project.
    if best_project is None and best_workspace is not None:
        repo_path = best_workspace.get("repo_path")
        for row in project_rows:
            if repo_path and row["main_repo_path"] and os.path.abspath(
                row["main_repo_path"]
            ) == os.path.abspath(repo_path):
                best_project = {
                    "id": row["id"],
                    "name": row["name"],
                    "main_repo_path": os.path.abspath(row["main_repo_path"]),
                    "default_branch": row["default_branch"],
                }
                break

    if best_project is not None:
        result["matched"] = True
        result["project"] = best_project

    if best_workspace is not None:
        result["matched"] = True
        result["workspace"] = best_workspace

    return result


# ---------------------------------------------------------------------------
# Provider detection and metadata normalization
# ---------------------------------------------------------------------------


def detect_provider_kind(provider: dict[str, Any]) -> Optional[str]:
    """Map a configured provider onto a registry key, or ``None``."""
    base_url = (provider.get("base_url") or "").strip()
    host = ""
    if base_url:
        try:
            host = (urlparse(base_url).hostname or "").lower()
        except ValueError:
            host = ""

    name = (provider.get("name") or "").strip().lower()

    for kind, spec in PROVIDER_REGISTRY.items():
        for match_host in spec.get("match_hosts", []):
            if host and (host == match_host or host.endswith("." + match_host)):
                return kind
        for match_name in spec.get("match_names", []):
            if name and match_name in name:
                return kind
    return None


def normalize_openrouter_model(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize one OpenRouter catalog entry into Copilot metadata terms."""
    context_length = entry.get("context_length")
    if context_length is None:
        top_provider = entry.get("top_provider") or {}
        context_length = top_provider.get("context_length")

    top_provider = entry.get("top_provider") or {}
    max_output_tokens = top_provider.get("max_completion_tokens")

    reasoning = entry.get("reasoning") or {}
    raw_efforts = reasoning.get("supported_efforts")
    efforts: Optional[list[str]] = None
    if isinstance(raw_efforts, list):
        efforts = [str(item) for item in raw_efforts if str(item) in COPILOT_REASONING_EFFORTS]
        # Preserve the provider's ordering but drop duplicates.
        seen: set[str] = set()
        efforts = [item for item in efforts if not (item in seen or seen.add(item))]
        if not efforts:
            efforts = None

    default_effort = reasoning.get("default_effort")
    if default_effort is not None:
        default_effort = str(default_effort)
        if default_effort not in COPILOT_REASONING_EFFORTS:
            default_effort = None
        elif efforts is not None and default_effort not in efforts:
            default_effort = None

    return {
        "model_id": entry.get("id"),
        "name": entry.get("name"),
        "max_context_window_tokens": context_length,
        "max_output_tokens": max_output_tokens,
        "supported_reasoning_efforts": efforts,
        "default_reasoning_effort": default_effort,
        "supports_reasoning": efforts is not None,
    }


def derive_copilot_limits(metadata: dict[str, Any]) -> dict[str, Any]:
    """Derive the Copilot ``provider_models`` columns from provider metadata.

    ``max_output_tokens`` is clamped to ``MAX_OUTPUT_TOKENS_CEILING`` and to
    ``(1 - MIN_PROMPT_FRACTION)`` of the context window, because providers
    report aggregate maxima that would otherwise starve the prompt budget.
    ``max_prompt_tokens`` is then the remaining window, matching the app's
    built-in catalog convention of ``prompt + output == context``.
    """
    context_window = metadata.get("max_context_window_tokens")
    max_output = metadata.get("max_output_tokens")

    if max_output is not None:
        max_output = int(max_output)
        max_output = min(max_output, MAX_OUTPUT_TOKENS_CEILING)
        if context_window is not None:
            max_output = min(
                max_output, int(int(context_window) * (1 - MIN_PROMPT_FRACTION))
            )
        max_output = max(max_output, 0)

    max_prompt: Optional[int] = None
    if context_window is not None:
        if max_output is not None:
            max_prompt = max(int(context_window) - max_output, 0)
        else:
            max_prompt = int(context_window)

    return {
        "max_prompt_tokens": max_prompt,
        "max_output_tokens": max_output,
        "supported_reasoning_efforts": metadata.get("supported_reasoning_efforts"),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_json(url: str, timeout: int = HTTP_TIMEOUT_SECONDS) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def fetch_catalog(kind: str, catalog_url: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Fetch and normalize a provider catalog, keyed by model id."""
    spec = PROVIDER_REGISTRY.get(kind)
    if spec is None:
        raise ValueError(f"Unsupported provider kind '{kind}'")

    url = catalog_url or spec["catalog_url"]
    payload = http_get_json(url)

    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"Unexpected catalog payload from {url}")

    catalog: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        catalog[str(entry["id"])] = normalize_openrouter_model(entry)
    return catalog


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _efforts_equal(current: Optional[list[str]], proposed: Optional[list[str]]) -> bool:
    if current is None and proposed is None:
        return True
    if current is None or proposed is None:
        return False
    return sorted(current) == sorted(proposed)


def build_plan(
    providers: list[dict[str, Any]],
    models: list[dict[str, Any]],
    catalogs: dict[str, dict[str, dict[str, Any]]],
    reasoning_state: Optional[dict[str, Any]] = None,
    context_tier_state: Optional[dict[str, Any]] = None,
    model_ids: Optional[list[str]] = None,
    set_reasoning_defaults: bool = False,
    set_context_tier: Optional[str] = None,
) -> dict[str, Any]:
    """Compute a dry-run plan without touching the database."""
    provider_by_id = {provider["id"]: provider for provider in providers}
    wanted = set(model_ids) if model_ids else None
    reasoning_state = reasoning_state or {}
    context_tier_state = context_tier_state or {}

    if set_context_tier is not None and set_context_tier not in COPILOT_CONTEXT_TIERS:
        raise ValueError(
            f"set_context_tier must be one of {list(COPILOT_CONTEXT_TIERS)}"
        )

    entries: list[dict[str, Any]] = []
    for model in models:
        if wanted is not None and model["model_id"] not in wanted and model["state_key"] not in wanted:
            continue

        provider = provider_by_id.get(model["provider_id"])
        kind = detect_provider_kind(provider) if provider else None

        entry: dict[str, Any] = {
            "model_id": model["model_id"],
            "provider_id": model["provider_id"],
            "provider_name": provider["name"] if provider else None,
            "provider_kind": kind,
            "row_id": model["row_id"],
            "state_key": model["state_key"],
            "current": {
                "max_prompt_tokens": model["max_prompt_tokens"],
                "max_output_tokens": model["max_output_tokens"],
                "supported_reasoning_efforts": model["supported_reasoning_efforts"],
            },
            "proposed": None,
            "changes": [],
            "app_state_changes": [],
            "status": "unchanged",
        }

        if kind is None:
            entry["status"] = "unsupported_provider"
            entry["reason"] = (
                "No source-of-truth registry entry matches this provider. "
                f"Supported providers: {sorted(PROVIDER_REGISTRY)}"
            )
            entries.append(entry)
            continue

        catalog = catalogs.get(kind) or {}
        metadata = catalog.get(model["model_id"])
        if metadata is None:
            entry["status"] = "unresolved"
            entry["reason"] = f"Model id not present in the {kind} catalog"
            entries.append(entry)
            continue

        proposed = derive_copilot_limits(metadata)
        entry["proposed"] = proposed
        entry["provider_metadata"] = {
            "max_context_window_tokens": metadata.get("max_context_window_tokens"),
            "provider_max_output_tokens": metadata.get("max_output_tokens"),
            "supported_reasoning_efforts": metadata.get("supported_reasoning_efforts"),
            "default_reasoning_effort": metadata.get("default_reasoning_effort"),
        }

        changes: list[str] = []
        if proposed["max_prompt_tokens"] is not None and (
            proposed["max_prompt_tokens"] != model["max_prompt_tokens"]
        ):
            changes.append("max_prompt_tokens")
        if proposed["max_output_tokens"] is not None and (
            proposed["max_output_tokens"] != model["max_output_tokens"]
        ):
            changes.append("max_output_tokens")
        if not _efforts_equal(
            model["supported_reasoning_efforts"], proposed["supported_reasoning_efforts"]
        ):
            changes.append("supported_reasoning_efforts")

        entry["changes"] = changes

        if set_reasoning_defaults:
            default_effort = metadata.get("default_reasoning_effort")
            if default_effort and model["state_key"] not in reasoning_state:
                entry["app_state_changes"].append(
                    {
                        "key": APP_STATE_REASONING_KEY,
                        "state_key": model["state_key"],
                        "from": None,
                        "to": default_effort,
                    }
                )

        if set_context_tier is not None and metadata.get("max_context_window_tokens"):
            current_tier = context_tier_state.get(model["state_key"])
            if current_tier != set_context_tier:
                entry["app_state_changes"].append(
                    {
                        "key": APP_STATE_CONTEXT_TIER_KEY,
                        "state_key": model["state_key"],
                        "from": current_tier,
                        "to": set_context_tier,
                    }
                )

        if changes or entry["app_state_changes"]:
            entry["status"] = "update"
        entries.append(entry)

    summary = {
        "total": len(entries),
        "update": sum(1 for entry in entries if entry["status"] == "update"),
        "unchanged": sum(1 for entry in entries if entry["status"] == "unchanged"),
        "unresolved": sum(1 for entry in entries if entry["status"] == "unresolved"),
        "unsupported_provider": sum(
            1 for entry in entries if entry["status"] == "unsupported_provider"
        ),
    }

    return {
        "summary": summary,
        "providers": [
            {
                "id": provider["id"],
                "name": provider["name"],
                "type": provider["type"],
                "base_url": provider["base_url"],
                "provider_kind": detect_provider_kind(provider),
            }
            for provider in providers
        ],
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def list_byok_models(db_path: Optional[str] = None) -> dict[str, Any]:
    """List configured BYOK providers and models with their current metadata."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn)
        models = load_models(conn)
        reasoning_state = load_app_state(conn, APP_STATE_REASONING_KEY) or {}
        context_tier_state = load_app_state(conn, APP_STATE_CONTEXT_TIER_KEY) or {}
    finally:
        conn.close()

    provider_by_id = {provider["id"]: provider for provider in providers}
    enriched = []
    for model in models:
        provider = provider_by_id.get(model["provider_id"], {})
        enriched.append(
            {
                **model,
                "provider_name": provider.get("name"),
                "provider_kind": detect_provider_kind(provider) if provider else None,
                "reasoning_effort": reasoning_state.get(model["state_key"]),
                "context_tier": context_tier_state.get(model["state_key"]),
                "has_metadata": any(
                    model[field] is not None
                    for field in (
                        "max_prompt_tokens",
                        "max_output_tokens",
                        "supported_reasoning_efforts",
                    )
                ),
            }
        )

    return {
        "db_path": path,
        "providers": [
            {
                **provider,
                "provider_kind": detect_provider_kind(provider),
                "model_count": sum(
                    1 for model in models if model["provider_id"] == provider["id"]
                ),
            }
            for provider in providers
        ],
        "models": enriched,
        "summary": {
            "provider_count": len(providers),
            "model_count": len(models),
            "models_with_metadata": sum(1 for model in enriched if model["has_metadata"]),
            "models_without_metadata": sum(
                1 for model in enriched if not model["has_metadata"]
            ),
        },
    }


def read_byok_config(
    workspace_path: Optional[str] = None,
    db_path: Optional[str] = None,
    include_catalog_cache: bool = True,
) -> dict[str, Any]:
    """Return a complete, self-contained snapshot of the BYOK model config.

    This is the awareness entry point: a consuming agent gets every provider,
    every model, the metadata currently stored for each, the user's selected
    reasoning effort and context tier, and what is still missing — without
    locating or reading the app database itself.
    """
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn)
        models = load_models(conn)
        reasoning_state = load_app_state(conn, APP_STATE_REASONING_KEY) or {}
        context_tier_state = load_app_state(conn, APP_STATE_CONTEXT_TIER_KEY) or {}
        selected_model = load_app_state_scalar(conn, APP_STATE_SELECTED_MODEL_KEY)
        catalog_caches = (
            {key: load_app_state(conn, key) for key in CATALOG_CACHE_KEYS}
            if include_catalog_cache
            else {}
        )
        workspace = (
            resolve_workspace(conn, workspace_path) if workspace_path else None
        )
    finally:
        conn.close()

    provider_by_id = {provider["id"]: provider for provider in providers}

    enriched_models = []
    for model in models:
        provider = provider_by_id.get(model["provider_id"], {})
        kind = detect_provider_kind(provider) if provider else None
        efforts = model["supported_reasoning_efforts"]
        missing = [
            field
            for field in (
                "max_prompt_tokens",
                "max_output_tokens",
                "supported_reasoning_efforts",
            )
            if model[field] is None
        ]
        enriched_models.append(
            {
                **model,
                "provider_name": provider.get("name"),
                "provider_kind": kind,
                "provider_supported": kind is not None,
                "reasoning_effort": reasoning_state.get(model["state_key"]),
                "context_tier": context_tier_state.get(model["state_key"]),
                "has_metadata": len(missing) < 3,
                "missing_metadata": missing,
                "supports_reasoning": bool(efforts),
            }
        )

    enriched_providers = []
    for provider in providers:
        kind = detect_provider_kind(provider)
        spec = PROVIDER_REGISTRY.get(kind) if kind else None
        enriched_providers.append(
            {
                **provider,
                "provider_kind": kind,
                "provider_supported": kind is not None,
                "catalog_url": spec["catalog_url"] if spec else None,
                "model_count": sum(
                    1 for model in models if model["provider_id"] == provider["id"]
                ),
            }
        )

    byok_catalog_entries: list[dict[str, Any]] = []
    catalog_cache_summary: dict[str, Any] = {}
    for cache_key in CATALOG_CACHE_KEYS:
        cache = catalog_caches.get(cache_key)
        entries = _catalog_entries(cache) if cache is not None else None
        if entries is None:
            catalog_cache_summary[cache_key] = {"present": False}
            continue
        byok_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("providerId")
        ]
        with_limits = sum(
            1
            for entry in byok_entries
            if isinstance((entry.get("capabilities") or {}).get("limits"), dict)
            and (entry.get("capabilities") or {}).get("limits")
        )
        with_efforts = sum(
            1 for entry in byok_entries if entry.get("supportedReasoningEfforts")
        )
        catalog_cache_summary[cache_key] = {
            "present": True,
            "shape": "list" if isinstance(cache, list) else "object",
            "entry_count": len(entries),
            "byok_entry_count": len(byok_entries),
            "byok_entries_with_limits": with_limits,
            "byok_entries_with_reasoning_efforts": with_efforts,
        }
        if cache_key == APP_STATE_CATALOG_KEY:
            for entry in byok_entries:
                capabilities = entry.get("capabilities") or {}
                limits = capabilities.get("limits") or {}
                byok_catalog_entries.append(
                    {
                        "id": entry.get("id"),
                        "name": entry.get("name"),
                        "providerId": entry.get("providerId"),
                        "max_context_window_tokens": limits.get(
                            "max_context_window_tokens"
                        ),
                        "max_prompt_tokens": limits.get("max_prompt_tokens"),
                        "max_output_tokens": limits.get("max_output_tokens"),
                        "has_limits": bool(limits),
                        "supportedReasoningEfforts": entry.get(
                            "supportedReasoningEfforts"
                        ),
                        "defaultReasoningEffort": entry.get(
                            "defaultReasoningEffort"
                        ),
                    }
                )

    supported_kinds = sorted(
        {p["provider_kind"] for p in enriched_providers if p["provider_kind"]}
    )
    unsupported = [
        p["name"] for p in enriched_providers if not p["provider_supported"]
    ]

    notes = [
        "BYOK model configuration is app-global, not per-workspace.",
        "Provider settings are returned with credential values redacted.",
        "Use plan_metadata_updates to see proposed changes, then "
        "apply_metadata_updates to write them.",
    ]
    if include_catalog_cache and byok_catalog_entries:
        missing_limits = sum(
            1 for entry in byok_catalog_entries if not entry["has_limits"]
        )
        if missing_limits:
            # Only models whose provider has a source-of-truth registry entry can
            # ever be patched; the rest are expected to stay unset.
            patchable = {
                entry["id"]
                for entry in byok_catalog_entries
                if not entry["has_limits"]
                and any(
                    model["state_key"] == entry["id"]
                    and model["provider_kind"] in PROVIDER_REGISTRY
                    for model in enriched_models
                )
            }
            if patchable:
                notes.append(
                    f"{len(patchable)} of {len(byok_catalog_entries)} BYOK catalog "
                    "entries carry no capabilities.limits and belong to a provider "
                    "with a source-of-truth registry. The app reads a BYOK model's "
                    "context window from this cache, not from provider_models, so "
                    "those models fall back to the app's default window. Run "
                    "apply_metadata_updates with refresh_catalog_cache=true to patch it."
                )
            else:
                notes.append(
                    f"{missing_limits} of {len(byok_catalog_entries)} BYOK catalog "
                    "entries carry no capabilities.limits. None of them belong to a "
                    "provider with a source-of-truth registry, so there is nothing to "
                    "patch — this is expected."
                )

    return {
        "db_path": path,
        "workspace": workspace,
        "providers": enriched_providers,
        "models": enriched_models,
        "selections": {
            "selected_model": selected_model,
            "reasoning_effort": reasoning_state,
            "context_tier": context_tier_state,
        },
        "catalog_cache": {
            "included": include_catalog_cache,
            "byok_entry_count": len(byok_catalog_entries),
            "byok_entries_with_limits": sum(
                1 for entry in byok_catalog_entries if entry["has_limits"]
            ),
            "byok_entries": byok_catalog_entries,
            "caches": catalog_cache_summary,
        },
        "summary": {
            "provider_count": len(enriched_providers),
            "model_count": len(enriched_models),
            "models_with_metadata": sum(
                1 for m in enriched_models if m["has_metadata"]
            ),
            "models_without_metadata": sum(
                1 for m in enriched_models if not m["has_metadata"]
            ),
            "models_supporting_reasoning": sum(
                1 for m in enriched_models if m["supports_reasoning"]
            ),
            "providers_with_supported_source": supported_kinds,
            "providers_without_supported_source": unsupported,
        },
        "notes": notes,
    }


def fetch_provider_metadata(
    provider: Optional[str] = None,
    db_path: Optional[str] = None,
    catalog_url: Optional[str] = None,
    catalog: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fetch provider source-of-truth metadata for configured BYOK models."""
    if catalog is not None:
        kinds = [provider] if provider else sorted(PROVIDER_REGISTRY)
        catalogs = {kind: catalog for kind in kinds}
        providers: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
    else:
        path = resolve_db_path(db_path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Copilot app database not found at {path}")
        conn = connect(path, readonly=True)
        try:
            providers = load_providers(conn)
            models = load_models(conn)
        finally:
            conn.close()

        kinds = sorted(
            {
                kind
                for kind in (detect_provider_kind(provider_entry) for provider_entry in providers)
                if kind is not None
            }
        )
        if provider:
            if provider not in PROVIDER_REGISTRY:
                raise ValueError(
                    f"Unsupported provider '{provider}'. Supported: {sorted(PROVIDER_REGISTRY)}"
                )
            kinds = [provider]

        catalogs = {}
        for kind in kinds:
            catalogs[kind] = fetch_catalog(kind, catalog_url=catalog_url)

    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    provider_by_id = {entry["id"]: entry for entry in providers}

    for model in models:
        provider_entry = provider_by_id.get(model["provider_id"])
        kind = detect_provider_kind(provider_entry) if provider_entry else None
        if kind is None:
            continue
        metadata = (catalogs.get(kind) or {}).get(model["model_id"])
        if metadata is None:
            missing.append(
                {
                    "model_id": model["model_id"],
                    "provider_id": model["provider_id"],
                    "provider_kind": kind,
                }
            )
            continue
        resolved.append(
            {
                "model_id": model["model_id"],
                "provider_id": model["provider_id"],
                "provider_kind": kind,
                "state_key": model["state_key"],
                "metadata": metadata,
                "derived": derive_copilot_limits(metadata),
            }
        )

    return {
        "providers_queried": kinds,
        "catalog_sizes": {kind: len(entries) for kind, entries in catalogs.items()},
        "resolved": resolved,
        "missing": missing,
        "summary": {
            "resolved": len(resolved),
            "missing": len(missing),
        },
        "notes": [
            "Read `derived` for the values this plugin writes. `metadata` is the raw "
            "provider payload; its max_output_tokens is an aggregate provider maximum "
            "and is clamped before being written."
        ],
    }


def plan_metadata_updates(
    db_path: Optional[str] = None,
    model_ids: Optional[list[str]] = None,
    set_reasoning_defaults: bool = False,
    set_context_tier: Optional[str] = None,
    catalog: Optional[dict[str, Any]] = None,
    catalog_url: Optional[str] = None,
) -> dict[str, Any]:
    """Compute a dry-run plan of metadata changes. Never writes."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn)
        models = load_models(conn)
        reasoning_state = load_app_state(conn, APP_STATE_REASONING_KEY) or {}
        context_tier_state = load_app_state(conn, APP_STATE_CONTEXT_TIER_KEY) or {}
    finally:
        conn.close()

    kinds = sorted(
        {
            kind
            for kind in (detect_provider_kind(provider) for provider in providers)
            if kind is not None
        }
    )

    if catalog is not None:
        catalogs = {kind: catalog for kind in kinds}
    else:
        catalogs = {kind: fetch_catalog(kind, catalog_url=catalog_url) for kind in kinds}

    plan = build_plan(
        providers=providers,
        models=models,
        catalogs=catalogs,
        reasoning_state=reasoning_state,
        context_tier_state=context_tier_state,
        model_ids=model_ids,
        set_reasoning_defaults=set_reasoning_defaults,
        set_context_tier=set_context_tier,
    )
    plan["db_path"] = path
    plan["dry_run"] = True
    return plan


def _backup_database(conn: sqlite3.Connection, db_path: str) -> str:
    stamp = int(time.time() * 1000)
    backup_path = f"{db_path}.byok-metadata-backup-{stamp}"
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return backup_path


def _catalog_entries(cache: Any) -> Optional[list[Any]]:
    """Return the model list from either catalog-cache shape.

    The local catalog is ``{"accountId": ..., "models": [...]}``; the cloud
    catalog is a bare JSON list. Returns ``None`` when the value is neither.
    """
    if isinstance(cache, list):
        return cache
    if isinstance(cache, dict):
        models = cache.get("models")
        if isinstance(models, list):
            return models
    return None


def _patch_catalog_cache(
    cache: Any, updates: dict[str, dict[str, Any]]
) -> tuple[Any, int]:
    """Patch BYOK entries in a cached model catalog. Returns (cache, patched).

    Accepts both catalog shapes (see :func:`_catalog_entries`) and mutates the
    entries in place, so the caller can persist ``cache`` unchanged.
    """
    models = _catalog_entries(cache)
    if models is None:
        return cache, 0

    patched = 0
    for entry in models:
        if not isinstance(entry, dict):
            continue
        provider_id = entry.get("providerId")
        if not provider_id:
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        # The app stores the full "<providerId>/<modelId>" key in `id` for BYOK
        # entries, so only prefix it when it is not already present.
        state_key = (
            entry_id
            if entry_id.startswith(f"{provider_id}/")
            else f"{provider_id}/{entry_id}"
        )
        update = updates.get(state_key)
        if update is None:
            continue

        capabilities = entry.setdefault("capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
            entry["capabilities"] = capabilities

        limits = capabilities.get("limits")
        if not isinstance(limits, dict):
            limits = {}
            capabilities["limits"] = limits
        if update.get("max_prompt_tokens") is not None:
            limits["max_prompt_tokens"] = update["max_prompt_tokens"]
        if update.get("max_output_tokens") is not None:
            limits["max_output_tokens"] = update["max_output_tokens"]
        if update.get("max_context_window_tokens") is not None:
            limits["max_context_window_tokens"] = update["max_context_window_tokens"]

        supports = capabilities.get("supports")
        if not isinstance(supports, dict):
            supports = {}
            capabilities["supports"] = supports
        efforts = update.get("supported_reasoning_efforts")
        supports["reasoningEffort"] = bool(efforts)
        if efforts:
            entry["supportedReasoningEfforts"] = list(efforts)
            default_effort = update.get("default_reasoning_effort")
            if default_effort:
                entry["defaultReasoningEffort"] = default_effort
        else:
            entry.pop("supportedReasoningEfforts", None)
            entry.pop("defaultReasoningEffort", None)
        patched += 1

    return cache, patched


def apply_metadata_updates(
    db_path: Optional[str] = None,
    model_ids: Optional[list[str]] = None,
    set_reasoning_defaults: bool = False,
    set_context_tier: Optional[str] = None,
    dry_run: bool = True,
    backup: bool = True,
    refresh_catalog_cache: bool = True,
    catalog: Optional[dict[str, Any]] = None,
    catalog_url: Optional[str] = None,
) -> dict[str, Any]:
    """Apply planned metadata updates to the Copilot app database."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn)
        models = load_models(conn)
        reasoning_state = load_app_state(conn, APP_STATE_REASONING_KEY) or {}
        context_tier_state = load_app_state(conn, APP_STATE_CONTEXT_TIER_KEY) or {}
    finally:
        conn.close()

    kinds = sorted(
        {
            kind
            for kind in (detect_provider_kind(provider) for provider in providers)
            if kind is not None
        }
    )
    if catalog is not None:
        catalogs = {kind: catalog for kind in kinds}
    else:
        catalogs = {kind: fetch_catalog(kind, catalog_url=catalog_url) for kind in kinds}

    plan = build_plan(
        providers=providers,
        models=models,
        catalogs=catalogs,
        reasoning_state=reasoning_state,
        context_tier_state=context_tier_state,
        model_ids=model_ids,
        set_reasoning_defaults=set_reasoning_defaults,
        set_context_tier=set_context_tier,
    )

    result: dict[str, Any] = {
        "db_path": path,
        "dry_run": dry_run,
        "plan": plan,
        "applied": {
            "models_updated": 0,
            "app_state_entries_updated": 0,
            "catalog_cache_entries_patched": 0,
            "catalog_caches_patched": {},
        },
        "backup_path": None,
        "notes": [],
    }

    if dry_run:
        result["notes"].append("dry_run=true — no changes were written")
        return result

    # The catalog cache is a separate channel from ``provider_models``: the app
    # reads a BYOK model's context window from the cache, so the cache can be
    # stale even when every database row is already correct. Build the patch
    # set from every resolved entry, not only the ones needing a row update.
    updates = {
        entry["state_key"]: {
            **(entry.get("proposed") or {}),
            "max_context_window_tokens": (entry.get("provider_metadata") or {}).get(
                "max_context_window_tokens"
            ),
            "default_reasoning_effort": (entry.get("provider_metadata") or {}).get(
                "default_reasoning_effort"
            ),
        }
        for entry in plan["entries"]
        if entry.get("proposed") and entry.get("provider_metadata")
    }

    # Probe the catalog caches so a stale cache still counts as work to do.
    # ``_patch_catalog_cache`` mutates in place, so probe a throwaway copy.
    cache_patch_counts: dict[str, int] = {}
    if refresh_catalog_cache and updates:
        probe = connect(path, readonly=True)
        try:
            for cache_key in CATALOG_CACHE_KEYS:
                cache = load_app_state(probe, cache_key)
                if cache is None:
                    continue
                _, patched = _patch_catalog_cache(
                    json.loads(json.dumps(cache)), updates
                )
                if patched:
                    cache_patch_counts[cache_key] = patched
        finally:
            probe.close()

    has_row_updates = any(
        entry["status"] == "update" and entry.get("proposed")
        for entry in plan["entries"]
    )
    has_app_state_changes = any(
        entry["app_state_changes"] for entry in plan["entries"]
    )
    if not has_row_updates and not has_app_state_changes and not cache_patch_counts:
        result["notes"].append("Nothing to apply — configuration already matches provider metadata")
        return result

    conn = connect(path, readonly=False)
    try:
        if backup:
            result["backup_path"] = _backup_database(conn, path)

        with conn:
            for entry in plan["entries"]:
                if entry["status"] != "update" or not entry.get("proposed"):
                    continue
                proposed = entry["proposed"]
                conn.execute(
                    """
                    UPDATE provider_models
                    SET max_prompt_tokens = COALESCE(?, max_prompt_tokens),
                        max_output_tokens = COALESCE(?, max_output_tokens),
                        supported_reasoning_efforts = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (
                        proposed["max_prompt_tokens"],
                        proposed["max_output_tokens"],
                        None
                        if proposed["supported_reasoning_efforts"] is None
                        else json.dumps(proposed["supported_reasoning_efforts"]),
                        entry["row_id"],
                    ),
                )
                result["applied"]["models_updated"] += 1

            reasoning_updates = [
                change
                for entry in plan["entries"]
                for change in entry["app_state_changes"]
                if change["key"] == APP_STATE_REASONING_KEY
            ]
            if reasoning_updates:
                merged = dict(reasoning_state)
                for change in reasoning_updates:
                    merged[change["state_key"]] = change["to"]
                conn.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (APP_STATE_REASONING_KEY, json.dumps(merged)),
                )
                result["applied"]["app_state_entries_updated"] += len(reasoning_updates)

            tier_updates = [
                change
                for entry in plan["entries"]
                for change in entry["app_state_changes"]
                if change["key"] == APP_STATE_CONTEXT_TIER_KEY
            ]
            if tier_updates:
                merged_tier = dict(context_tier_state)
                for change in tier_updates:
                    merged_tier[change["state_key"]] = change["to"]
                conn.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (APP_STATE_CONTEXT_TIER_KEY, json.dumps(merged_tier)),
                )
                result["applied"]["app_state_entries_updated"] += len(tier_updates)

            if refresh_catalog_cache:
                patched_total = 0
                for cache_key in CATALOG_CACHE_KEYS:
                    cache = load_app_state(conn, cache_key)
                    if cache is None:
                        continue
                    patched_cache, patched = _patch_catalog_cache(cache, updates)
                    if patched:
                        conn.execute(
                            """
                            INSERT INTO app_state (key, value, updated_at)
                            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                            ON CONFLICT(key) DO UPDATE SET
                                value = excluded.value,
                                updated_at = excluded.updated_at
                            """,
                            (cache_key, json.dumps(patched_cache)),
                        )
                        patched_total += patched
                        result["applied"]["catalog_caches_patched"][cache_key] = patched
                result["applied"]["catalog_cache_entries_patched"] = patched_total
                if not patched_total and updates:
                    result["notes"].append(
                        f"WARNING: refresh_catalog_cache was requested and {len(updates)} "
                        f"model(s) had provider metadata, but 0 entries in "
                        f"{' or '.join(CATALOG_CACHE_KEYS)} matched. The app may not "
                        "learn the context window for these models. Reopen the model "
                        "picker to rebuild the catalog, then re-run with "
                        "refresh_catalog_cache=true."
                    )
    finally:
        conn.close()

    result["notes"].append(
        "Restart or reopen the model picker in the GitHub Copilot app to pick up changes"
    )
    return result


def get_provider_registry() -> dict[str, Any]:
    """Return the hardcoded provider source-of-truth registry."""
    return {
        "providers": {
            kind: {
                "display_name": spec["display_name"],
                "catalog_url": spec["catalog_url"],
                "endpoints_url_template": spec.get("endpoints_url_template"),
                "match_hosts": spec.get("match_hosts", []),
                "match_names": spec.get("match_names", []),
                "docs_url": spec.get("docs_url"),
            }
            for kind, spec in PROVIDER_REGISTRY.items()
        },
        "copilot_reasoning_efforts": list(COPILOT_REASONING_EFFORTS),
        "copilot_context_tiers": list(COPILOT_CONTEXT_TIERS),
        "targets": {
            "github_copilot_app": {
                "supported": True,
                "db_path": DEFAULT_DB_PATH,
                "tables": ["model_providers", "provider_models", "app_state"],
            },
            "github_copilot_cli": {
                "supported": False,
                "reason": "CLI configuration is not yet wired up; planned for a future release",
            },
        },
    }


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_byok_config",
        "description": (
            "Reads the complete GitHub Copilot desktop app BYOK model configuration "
            "in one call: every provider (with credentials redacted), every model "
            "with its stored context-window, output-token, and reasoning-effort "
            "metadata, the user's selected reasoning effort and context tier, the "
            "cached catalog entries, and what metadata is still missing. Use this "
            "for awareness instead of locating or reading the app database."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_path": {
                    "type": "string",
                    "description": (
                        "The calling agent's workspace path. Used to identify the "
                        "project and workspace for context; BYOK configuration "
                        "itself is app-global."
                    ),
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional override for the Copilot app database path.",
                },
                "include_catalog_cache": {
                    "type": "boolean",
                    "description": (
                        "Include the cached model-catalog entries for BYOK models. "
                        "Defaults to true."
                    ),
                    "default": True,
                },
            },
        },
    },
    {
        "name": "list_byok_models",
        "description": (
            "Lists every BYOK provider and model configured in the GitHub Copilot "
            "desktop app, including current context-window, output-token, and "
            "reasoning-effort metadata plus the user's selected reasoning effort "
            "and context tier."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Optional override for the Copilot app database path.",
                }
            },
        },
    },
    {
        "name": "fetch_provider_metadata",
        "description": (
            "Fetches authoritative context-window and reasoning-effort metadata from "
            "the provider source-of-truth API (OpenRouter only for now) for every "
            "configured BYOK model, and returns the values derived for Copilot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Optional provider kind to query, e.g. 'openrouter'.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional override for the Copilot app database path.",
                },
                "catalog_url": {
                    "type": "string",
                    "description": "Optional override for the provider catalog URL.",
                },
            },
        },
    },
    {
        "name": "plan_metadata_updates",
        "description": (
            "Computes a dry-run plan comparing configured BYOK model metadata against "
            "provider source-of-truth values. Never writes to the database."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Optional override for the Copilot app database path.",
                },
                "model_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of model ids or state keys to plan for.",
                },
                "set_reasoning_defaults": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also plan reasoning-effort selections for models that have no "
                        "explicit user choice yet, using the provider default effort."
                    ),
                },
                "set_context_tier": {
                    "type": "string",
                    "enum": list(COPILOT_CONTEXT_TIERS),
                    "description": "Optionally plan a context tier for every resolved model.",
                },
                "catalog_url": {
                    "type": "string",
                    "description": "Optional override for the provider catalog URL.",
                },
            },
        },
    },
    {
        "name": "apply_metadata_updates",
        "description": (
            "Applies provider metadata to the Copilot app database. Defaults to a dry "
            "run; pass dry_run=false to write. Writes are transactional and a database "
            "backup is taken first unless backup=false."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "db_path": {
                    "type": "string",
                    "description": "Optional override for the Copilot app database path.",
                },
                "model_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of model ids or state keys to update.",
                },
                "set_reasoning_defaults": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also set reasoning-effort selections for models with no explicit "
                        "user choice, using the provider default effort."
                    ),
                },
                "set_context_tier": {
                    "type": "string",
                    "enum": list(COPILOT_CONTEXT_TIERS),
                    "description": "Optionally set a context tier for every resolved model.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true (default) nothing is written.",
                },
                "backup": {
                    "type": "boolean",
                    "default": True,
                    "description": "Take a database backup before writing.",
                },
                "refresh_catalog_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Also patch the app's cached model catalogs so the app reflects "
                        "the new limits without a restart. Defaults to true because the "
                        "app reads a BYOK model's context window from the cache, not "
                        "from provider_models."
                    ),
                },
                "catalog_url": {
                    "type": "string",
                    "description": "Optional override for the provider catalog URL.",
                },
            },
        },
    },
    {
        "name": "get_provider_registry",
        "description": (
            "Returns the hardcoded provider source-of-truth registry, the canonical "
            "Copilot reasoning-effort and context-tier vocabularies, and which Copilot "
            "targets are supported."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def handle_rpc_message(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "server/discover":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-11-25", "2024-11-05"],
                "capabilities": {"tools": {"listChanged": False}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    }
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {}) or {}

        try:
            if tool_name == "read_byok_config":
                res = read_byok_config(
                    workspace_path=tool_args.get("workspace_path"),
                    db_path=tool_args.get("db_path"),
                    include_catalog_cache=tool_args.get(
                        "include_catalog_cache", True
                    ),
                )
            elif tool_name == "list_byok_models":
                res = list_byok_models(db_path=tool_args.get("db_path"))
            elif tool_name == "fetch_provider_metadata":
                res = fetch_provider_metadata(
                    provider=tool_args.get("provider"),
                    db_path=tool_args.get("db_path"),
                    catalog_url=tool_args.get("catalog_url"),
                )
            elif tool_name == "plan_metadata_updates":
                res = plan_metadata_updates(
                    db_path=tool_args.get("db_path"),
                    model_ids=tool_args.get("model_ids"),
                    set_reasoning_defaults=tool_args.get("set_reasoning_defaults", False),
                    set_context_tier=tool_args.get("set_context_tier"),
                    catalog_url=tool_args.get("catalog_url"),
                )
            elif tool_name == "apply_metadata_updates":
                res = apply_metadata_updates(
                    db_path=tool_args.get("db_path"),
                    model_ids=tool_args.get("model_ids"),
                    set_reasoning_defaults=tool_args.get("set_reasoning_defaults", False),
                    set_context_tier=tool_args.get("set_context_tier"),
                    dry_run=tool_args.get("dry_run", True),
                    backup=tool_args.get("backup", True),
                    refresh_catalog_cache=tool_args.get("refresh_catalog_cache", True),
                    catalog_url=tool_args.get("catalog_url"),
                )
            elif tool_name == "get_provider_registry":
                res = get_provider_registry()
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method/tool '{tool_name}' not found",
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    # Single-line JSON: the harness writes this to a temp file
                    # that agents then parse, and pretty-printing forces them to
                    # hunt for the JSON boundary with grep/head.
                    "content": [{"type": "text", "text": json.dumps(res)}],
                    "structuredContent": res,
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {"type": "text", "text": f"Error executing {tool_name}: {exc}"}
                    ],
                    "isError": True,
                },
            }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }
    return None


def run_stdio_server() -> None:
    """Run JSON-RPC 2.0 stdio server."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            sys.stderr.write(f"{SERVER_NAME}: malformed JSON request: {exc}\n")
            continue

        resp = handle_rpc_message(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
