#!/usr/bin/env python3
"""copilot-byok-metadata MCP server.

Reads the GitHub Copilot desktop app's BYOK model configuration from
``~/.copilot/data.db``, fetches authoritative context-window and
reasoning-effort metadata from provider source-of-truth APIs, and applies
that metadata back to the app config.

Two provider sources are wired up:

* ``openrouter`` — the public model catalog plus per-endpoint limits.
* ``omlx`` — a local oMLX server's OpenAI-compatible ``/v1/models`` listing,
  enriched by ``/v1/models/status``, which reports the *effective* context
  window (per-model override, discovered model native length, or the oMLX
  app default) and the per-model generation ceiling. Reasoning efforts come
  from ``/admin/api/models`` (which publishes the exact Copilot level
  vocabulary), falling back to the coarse ``reasoning_levels`` toggle list in
  ``/v1/models/status``, and finally to no reasoning metadata at all.

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
SERVER_VERSION = "0.5.1"

DEFAULT_DB_PATH = os.path.join("~", ".copilot", "data.db")

#: Canonical Copilot ``ReasoningEffort`` union. Provider vocabularies are
#: intersected with this set, so provider-only values such as ``minimal``
#: and ``none`` are dropped.
COPILOT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Canonical Copilot ``ContextTier`` union.
COPILOT_CONTEXT_TIERS = ("default", "long_context")

#: MIME types implied by each provider-declared input modality. Only
#: ``supported_media_types`` is derived: neither provider publishes an image
#: count or image size limit, so the app applies its own defaults.
INPUT_MODALITY_MEDIA_TYPES: dict[str, tuple[str, ...]] = {
    "image": ("image/jpeg", "image/png", "image/webp", "image/gif"),
    "file": ("application/pdf",),
    "audio": ("audio/wav", "audio/mpeg"),
    "video": ("video/mp4",),
    "text": (),
}

#: oMLX model/engine types that denote a vision-language model.
OMLX_VISION_MODEL_TYPES = ("vlm",)

#: oMLX admin path that publishes per-model reasoning metadata. It sits on the
#: server root, not under the OpenAI-compatible ``/v1`` API root.
OMLX_ADMIN_MODELS_PATH = "/admin/api/models"

#: oMLX model/engine types that cannot serve chat completions.
#:
#: A reranker, a TTS engine, and a document converter appear in ``/v1/models``
#: exactly like a chat model, but they fail at request time. Writing a context
#: window for them is worse than writing nothing: the model picker then offers
#: a model that cannot answer. These types are reported as
#: ``unsupported_model_type`` and skipped by every write path unless the caller
#: explicitly opts in for debugging.
OMLX_UNSUPPORTED_MODEL_TYPES = ("reranker", "audio_tts", "markitdown")

#: oMLX model/engine types whose reasoning metadata is trustworthy.
#:
#: ``/admin/api/models`` returns the same server-wide ``reasoning_effort_options``
#: for every model, including rerankers and TTS engines, so the list is a server
#: default rather than per-model truth. Reasoning is therefore only applied to
#: types that run chat completions. This is the *same* trust gate as
#: ``OMLX_UNSUPPORTED_MODEL_TYPES``: a type is trusted only when it is not one of
#: the non-chat types, so there is one list rather than two that can drift.
OMLX_REASONING_MODEL_TYPES = ("vlm", "llm", "text")

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
#: State keys whose vision flag was deliberately skipped because the database
#: has no ``supports_vision`` column. Recording the skip lets a legacy database
#: settle instead of planning the same unstorable change on every run.
APP_STATE_VISION_SKIPPED_KEY = "copilot-byok-metadata-vision-skipped"

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

#: Hardcoded provider source-of-truth registry. Providers with a ``catalog_url``
#: use a single public catalog; providers with ``base_url_params`` query the
#: configured BYOK base URL itself (local inference servers).
PROVIDER_REGISTRY: dict[str, dict[str, Any]] = {
    "openrouter": {
        "display_name": "OpenRouter",
        "catalog_url": "https://openrouter.ai/api/v1/models",
        "endpoints_url_template": "https://openrouter.ai/api/v1/models/{model_id}/endpoints",
        "match_hosts": ["openrouter.ai"],
        "match_names": ["openrouter"],
        "docs_url": "https://openrouter.ai/docs/api-reference/list-available-models",
        "reasoning_efforts": True,
    },
    "omlx": {
        "display_name": "oMLX",
        # oMLX is a local server: the catalog lives at whatever base URL the
        # provider was configured with, so ``catalog_url`` is derived per
        # provider rather than hardcoded.
        "catalog_url": None,
        "catalog_path": "/models",
        "status_path": "/models/status",
        "base_url_params": ["base_url"],
        "match_names": ["omlx"],
        "docs_url": "https://github.com/jundot/omlx/blob/main/docs/admin-api.md",
        # The admin surface publishes Copilot-shaped levels, but only for the
        # server's main key; status levels alone have no Copilot vocabulary.
        "reasoning_efforts": "conditional",
        "reasoning_effort_fallback": (
            "/admin/api/models reasoning_effort_options -> "
            "/v1/models/status reasoning_levels (reported only, no levels "
            "written) -> none"
        ),
        "admin_models_path": OMLX_ADMIN_MODELS_PATH,
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


def load_providers(
    conn: sqlite3.Connection, redact: bool = True
) -> list[dict[str, Any]]:
    """Load configured BYOK providers.

    ``redact`` defaults to ``True`` because every tool result must show
    ``<redacted>`` credentials. Internal call sites that need to *use* the
    provider (the oMLX admin fetch) pass ``redact=False`` and must never place
    the returned ``settings`` in a tool result, plan, warning, or exception.
    """
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
                "settings": redact_settings(settings) if redact else dict(settings),
            }
        )
    return providers


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table``, or an empty set when unreadable."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def load_models(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # ``supports_vision`` post-dates the rest of the table. On a database that
    # predates it the column is projected as NULL, which callers read as
    # "unknown" — never as "text-only" — and the write path skips it. The
    # boolean lets ``build_plan`` flag a change the schema cannot store so the
    # write path can skip re-proposing it instead of rewriting forever.
    has_vision_column = "supports_vision" in table_columns(conn, "provider_models")
    vision_projection = "supports_vision" if has_vision_column else "NULL AS supports_vision"
    rows = conn.execute(
        f"""
        SELECT id, provider_id, model_id, wire_model, display_name,
               max_prompt_tokens, max_output_tokens, supported_reasoning_efforts,
               {vision_projection}
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
                "supports_vision": row["supports_vision"],
                "supports_vision_available": has_vision_column,
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


def provider_base_url(provider: dict[str, Any]) -> str:
    """Return the provider's configured base URL without a trailing slash."""
    return (provider.get("base_url") or "").strip().rstrip("/")


def provider_api_root(base_url: str) -> str:
    """Return the ``/v1`` API root for a provider base URL.

    oMLX serves the OpenAI-compatible surface at ``<host>/v1``; users may
    configure the base URL with or without the trailing ``/v1``.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def provider_catalog_url(kind: str, provider: Optional[dict[str, Any]] = None,
                         catalog_url: Optional[str] = None) -> Optional[str]:
    """Resolve the source-of-truth catalog URL for a provider kind.

    Public catalogs come from the registry. Providers registered with
    ``base_url_params`` derive the URL from the configured provider instead,
    so every local oMLX instance is queried at its own address.
    """
    if catalog_url:
        return catalog_url
    spec = PROVIDER_REGISTRY.get(kind)
    if spec is None:
        return None
    if spec.get("base_url_params"):
        if not provider:
            return None
        base = provider_api_root(provider_base_url(provider))
        return f"{base}{spec['catalog_path']}" if base else None
    return spec.get("catalog_url")


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
    if not host and not base_url and not name:
        return None

    for kind, spec in PROVIDER_REGISTRY.items():
        for match_host in spec.get("match_hosts", []):
            if host and (host == match_host or host.endswith("." + match_host)):
                return kind
        if spec.get("base_url_params"):
            # Local servers have no meaningful host to match on, so require a
            # name match plus a usable base URL to query.
            if base_url and any(
                match_name in name for match_name in spec.get("match_names", [])
            ):
                return kind
            continue
        for match_name in spec.get("match_names", []):
            if name and match_name in name:
                return kind
    return None


def vision_media_types(input_modalities: Optional[list[Any]]) -> list[str]:
    """Map provider-declared input modalities onto Copilot media types.

    ``text`` contributes nothing, and unknown modalities are ignored rather
    than guessed at.
    """
    if not isinstance(input_modalities, list):
        return []
    media_types: list[str] = []
    for modality in input_modalities:
        key = str(modality).strip().lower()
        for media_type in INPUT_MODALITY_MEDIA_TYPES.get(key, ()):
            if media_type not in media_types:
                media_types.append(media_type)
    return media_types


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

    architecture = entry.get("architecture") or {}
    input_modalities = (
        architecture.get("input_modalities")
        if isinstance(architecture, dict)
        else None
    )
    media_types = vision_media_types(input_modalities)
    # Only an explicitly declared ``image`` modality is definitive evidence.
    # Anything else leaves vision unknown, which the write path treats as
    # "do not touch" rather than "text-only".
    vision_unknown = not isinstance(input_modalities, list)
    supports_vision = None
    vision_source = None
    if not vision_unknown:
        supports_vision = "image" in {
            str(modality).strip().lower() for modality in input_modalities
        }
        if supports_vision:
            vision_source = "openrouter.architecture"

    return {
        "model_id": entry.get("id"),
        "name": entry.get("name"),
        "max_context_window_tokens": context_length,
        "max_output_tokens": max_output_tokens,
        "supported_reasoning_efforts": efforts,
        "default_reasoning_effort": default_effort,
        "supports_reasoning": efforts is not None,
        # OpenRouter declares its effort vocabulary authoritatively: an empty
        # or absent list means the model has no efforts, not "unknown".
        "reasoning_efforts_definitive": True,
        "supports_vision": supports_vision,
        "vision_media_types": media_types,
        "vision_source": vision_source,
        # OpenRouter publishes no model-class signal, so every entry is a chat
        # model. The keys mirror the oMLX normalizer so plan code reads one
        # shape; the non-chat exclusion is oMLX-only.
        "supported_for_chat": True,
        "unsupported_model_type": None,
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
        # True only when the provider gave definitive effort evidence. ``None``
        # means "unknown", which tells the plan to propose no reasoning change
        # rather than clearing metadata that is already stored.
        "reasoning_efforts_definitive": metadata.get(
            "reasoning_efforts_definitive", True
        ),
        # Vision is tri-state: True (definitive provider evidence), False
        # (provider declared the model text-only), or None (unknown). Only True
        # is ever written; see ``build_plan``.
        "supports_vision": metadata.get("supports_vision"),
        "vision_media_types": list(metadata.get("vision_media_types") or []),
        "vision_source": metadata.get("vision_source"),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_json(
    url: str,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def provider_request_headers(provider: Optional[dict[str, Any]]) -> dict[str, str]:
    """Build auth headers for a provider query, without logging the secret.

    oMLX accepts its main API key on the inference surface, so a protected
    instance can still be queried. API keys are redacted in every tool
    response but must be sent on the wire; loopback servers usually need none.
    """
    settings = (provider or {}).get("settings") or {}
    api_key = settings.get("apiKey")
    if not isinstance(api_key, str) or not api_key or api_key == "<redacted>":
        return {}
    return {"Authorization": "Bearer " + api_key}


def is_unsupported_omlx_model_type(*declared_types: Any) -> bool:
    """Whether the model is one oMLX cannot serve chat with.

    This is the single exclusion predicate. Rerankers, TTS engines, and
    document converters are advertised by ``/v1/models`` exactly like chat
    models, so every metadata decision — plan status, reasoning trust, vision
    — consults this one function instead of keeping its own list.
    """
    return omlx_unsupported_model_type(*declared_types) is not None


def omlx_unsupported_model_type(*declared_types: Any) -> Optional[str]:
    """Return the declared type that makes the model non-chat, if any."""
    for value in declared_types:
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in OMLX_UNSUPPORTED_MODEL_TYPES:
            return normalized
    return None


def is_omlx_reasoning_model_type(*declared_types: Any) -> bool:
    """Whether any declared oMLX model/engine type may carry reasoning.

    Kept as a standalone predicate so the trust gate lives in exactly one
    place. An absent type is *not* trusted: the admin endpoint reports a
    server-wide option list, so an unidentified model must not inherit it.
    A non-chat type (see :func:`is_unsupported_omlx_model_type`) never is.
    """
    if is_unsupported_omlx_model_type(*declared_types):
        return False
    for value in declared_types:
        if value and str(value).strip().lower() in OMLX_REASONING_MODEL_TYPES:
            return True
    return False


def _copilot_efforts(raw_efforts: Any) -> Optional[list[str]]:
    """Intersect a provider effort list with the Copilot vocabulary."""
    if not isinstance(raw_efforts, list):
        return None
    efforts = [
        str(item) for item in raw_efforts if str(item) in COPILOT_REASONING_EFFORTS
    ]
    seen: set[str] = set()
    efforts = [item for item in efforts if not (item in seen or seen.add(item))]
    return efforts or None


def normalize_omlx_model(
    entry: dict[str, Any],
    status_entry: Optional[dict[str, Any]] = None,
    admin_entry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize one oMLX model listing entry into Copilot metadata terms.

    ``/v1/models`` reports ``max_model_len`` (the effective context window) and
    ``/v1/models/status`` repeats it as ``max_context_window`` plus the
    per-model generation ceiling ``max_tokens``. The status entry wins when
    both are present because it reflects runtime overrides, and its
    ``model_context_length`` is kept as the discovered native length for
    diagnostics.

    Reasoning is best-effort. ``/admin/api/models`` publishes Copilot-shaped
    ``reasoning_effort_options`` and is preferred. When the admin surface is
    unavailable (sub keys get a 401) the coarse ``reasoning_levels`` toggle
    list from ``/v1/models/status`` is reported for diagnostics only: it has
    no Copilot level vocabulary, so no efforts are written. Both surfaces are
    gated on the model type so server-wide defaults never leak onto rerankers
    or TTS engines.
    """
    status_entry = status_entry or {}
    admin_entry = admin_entry or {}
    context_window = status_entry.get("max_context_window")
    if context_window is None:
        context_window = status_entry.get("model_context_length")
    if context_window is None:
        context_window = entry.get("max_model_len")
    if context_window is None:
        context_window = entry.get("context_length")

    max_output = status_entry.get("max_tokens")

    model_type = (
        status_entry.get("model_type")
        or entry.get("model_type")
        or admin_entry.get("model_type")
    )
    engine_type = (
        status_entry.get("engine_type")
        or entry.get("engine_type")
        or admin_entry.get("engine_type")
    )
    # Every surface that might declare a type, in one precedence order. The
    # exclusion predicate and the reasoning gate both read from this list, so
    # "reranker", "audio_tts", and "markitdown" are recognized whichever
    # endpoint happened to publish them.
    type_sources = (
        status_entry.get("model_type"),
        status_entry.get("engine_type"),
        entry.get("model_type"),
        entry.get("engine_type"),
        admin_entry.get("model_type"),
        admin_entry.get("engine_type"),
    )
    non_chat_type = omlx_unsupported_model_type(*type_sources)
    non_chat_model_type = non_chat_type is not None
    # oMLX declares the model class rather than an input-modality list, so a
    # ``vlm`` type is the only definitive vision signal. Absent both fields,
    # vision stays unknown. A non-chat model is short-circuited first: it is
    # never offered image input no matter what its type fields claim.
    declared_types = [value for value in (model_type, engine_type) if value]
    supports_vision: Optional[bool] = None
    vision_source = None
    if non_chat_model_type:
        supports_vision = False
    elif declared_types:
        supports_vision = any(
            str(value).strip().lower() in OMLX_VISION_MODEL_TYPES
            for value in declared_types
        )
        if supports_vision:
            vision_source = "omlx.model_type"

    efforts: Optional[list[str]] = None
    default_effort: Optional[str] = None
    # True only when the provider gave definitive reasoning evidence. Without
    # it (no admin access, unknown/untrusted model type) the plan must propose
    # no reasoning change at all rather than clearing what is already stored.
    efforts_known = False
    reasoning_levels: Optional[list[str]] = None
    thinking_default = None
    if is_omlx_reasoning_model_type(*type_sources):
        efforts = _copilot_efforts(admin_entry.get("reasoning_effort_options"))
        if efforts is not None:
            efforts_known = True
            candidate = admin_entry.get("reasoning_effort_default")
            if candidate is not None:
                candidate = str(candidate)
                default_effort = candidate if candidate in efforts else None

        raw_levels = status_entry.get("reasoning_levels")
        if raw_levels is None:
            raw_levels = admin_entry.get("reasoning_levels")
        if isinstance(raw_levels, list) and raw_levels:
            reasoning_levels = [str(item) for item in raw_levels]

        thinking_default = status_entry.get("thinking_default")
        if thinking_default is None:
            thinking_default = admin_entry.get("thinking_default")

    return {
        "model_id": entry.get("id") or status_entry.get("id"),
        "name": entry.get("id") or status_entry.get("id"),
        "max_context_window_tokens": context_window,
        "max_output_tokens": max_output,
        "supported_reasoning_efforts": efforts,
        "default_reasoning_effort": default_effort,
        "supports_reasoning": efforts is not None,
        "reasoning_efforts_definitive": efforts_known,
        "reasoning_levels": reasoning_levels,
        "thinking_default": thinking_default,
        "supports_vision": supports_vision,
        "vision_media_types": (
            list(INPUT_MODALITY_MEDIA_TYPES["image"]) if supports_vision else []
        ),
        "vision_source": vision_source,
        "context_source": (
            "status.max_context_window"
            if status_entry.get("max_context_window") is not None
            else "status.model_context_length"
            if status_entry.get("model_context_length") is not None
            else "models.max_model_len"
            if entry.get("max_model_len") is not None
            else "unresolved"
        ),
        "native_context_window_tokens": status_entry.get("model_context_length"),
        "model_type": model_type or engine_type,
        # ``supported_for_chat`` is the plan-side view of the exclusion
        # predicate: the model is still *reported*, it is just never written.
        "supported_for_chat": not non_chat_model_type,
        "unsupported_model_type": non_chat_type,
        "loaded": status_entry.get("loaded"),
    }


def normalize_provider_model(
    kind: str,
    entry: dict[str, Any],
    status: Optional[dict[str, Any]] = None,
    admin: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Dispatch a raw catalog entry to the provider-specific normalizer."""
    if kind == "omlx":
        model_id = str(entry.get("id"))
        status_entry = (status or {}).get(model_id)
        admin_entry = (admin or {}).get(model_id)
        return normalize_omlx_model(entry, status_entry, admin_entry)
    return normalize_openrouter_model(entry)


def fetch_omlx_admin_models(base_url: str, api_key: Optional[str]) -> dict[str, dict[str, Any]]:
    """Fetch ``/admin/api/models`` and index it by model id.

    The admin surface publishes the Copilot-shaped reasoning vocabulary, but it
    only accepts the oMLX main key: a sub key gets a 401. Every failure mode
    (401/404, connection failure, malformed payload) is silent and returns an
    empty mapping so callers fall back to ``/v1/models/status``. The key is
    passed in by the caller from unredacted provider settings; it is never
    logged, never returned, and never included in an error message — it only
    travels in the request header.
    """
    root = (base_url or "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    if not root:
        return {}

    settings: dict[str, Any] = {"apiKey": api_key}
    headers = provider_request_headers({"settings": settings})
    try:
        payload = http_get_json(f"{root}{OMLX_ADMIN_MODELS_PATH}", headers=headers)
    except (OSError, ValueError):
        return {}

    entries = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return {}

    admin: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            admin[str(entry["id"])] = entry
    return admin


def fetch_catalog(
    kind: str,
    catalog_url: Optional[str] = None,
    provider: Optional[dict[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
    """Fetch and normalize a provider catalog, keyed by model id."""
    spec = PROVIDER_REGISTRY.get(kind)
    if spec is None:
        raise ValueError(f"Unsupported provider kind '{kind}'")

    url = provider_catalog_url(kind, provider=provider, catalog_url=catalog_url)
    if not url:
        raise ValueError(
            f"No catalog URL could be resolved for provider kind '{kind}'"
        )
    headers = provider_request_headers(provider)
    payload = http_get_json(url, headers=headers)

    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"Unexpected catalog payload from {url}")

    status: dict[str, dict[str, Any]] = {}
    status_path = spec.get("status_path")
    if status_path:
        if url.endswith(spec["catalog_path"]):
            status_url = url[: -len(spec["catalog_path"])] + status_path
        elif provider:
            status_url = f"{provider_api_root(provider_base_url(provider))}{status_path}"
        else:
            status_url = None
        status_payload = None
        try:
            if status_url:
                status_payload = http_get_json(status_url, headers=headers)
        except (OSError, ValueError):
            # The status endpoint is an enrichment; a server that only speaks
            # the OpenAI surface still yields usable ``max_model_len`` values.
            status_payload = None
        if isinstance(status_payload, dict):
            for status_entry in status_payload.get("models") or []:
                if isinstance(status_entry, dict) and status_entry.get("id"):
                    status[str(status_entry["id"])] = status_entry

    catalog: dict[str, dict[str, Any]] = {}
    admin: dict[str, dict[str, Any]] = {}
    if spec.get("admin_models_path") and provider:
        settings = provider.get("settings") or {}
        admin = fetch_omlx_admin_models(
            provider_base_url(provider), settings.get("apiKey")
        )
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        catalog[str(entry["id"])] = normalize_provider_model(kind, entry, status, admin)
    return catalog


def collect_catalogs(
    providers: list[dict[str, Any]],
    kinds: list[str],
    catalog_url: Optional[str] = None,
    catalog: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    """Fetch every needed provider catalog.

    Public providers contribute one catalog keyed by provider kind. Providers
    registered with ``base_url_params`` (local inference servers) contribute
    one catalog per *provider* instead, keyed by provider id, because two
    configured servers can expose different model sets under the same kind.

    An unreachable local server is reported as a warning rather than raising:
    a stopped oMLX instance must not block metadata repair for other providers.
    """
    catalogs: dict[str, dict[str, dict[str, Any]]] = {}
    warnings: list[str] = []

    for kind in kinds:
        spec = PROVIDER_REGISTRY.get(kind)
        if spec is None:
            continue
        if catalog is not None:
            catalogs[kind] = catalog
            continue
        if not spec.get("base_url_params"):
            catalogs[kind] = fetch_catalog(kind, catalog_url=catalog_url)
            continue

        by_base: dict[str, dict[str, dict[str, Any]]] = {}
        for provider in providers:
            if detect_provider_kind(provider) != kind:
                continue
            base = provider_base_url(provider)
            if not base:
                continue
            if base not in by_base:
                try:
                    by_base[base] = fetch_catalog(
                        kind, provider=provider, catalog_url=catalog_url
                    )
                except (OSError, ValueError) as exc:
                    by_base[base] = {}
                    warnings.append(
                        f"Could not query {spec['display_name']} at "
                        f"{provider_api_root(base)}: {exc}. Models on this provider "
                        "will be reported as unresolved."
                    )
            catalogs[provider["id"]] = by_base[base]

    return catalogs, warnings


def catalog_for_model(
    catalogs: dict[str, dict[str, dict[str, Any]]],
    provider_id: str,
    kind: str,
) -> dict[str, dict[str, Any]]:
    """Look up the catalog that applies to one model's provider."""
    return catalogs.get(provider_id) or catalogs.get(kind) or {}


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
    include_unsupported_model_types: bool = False,
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
                "supports_vision": model.get("supports_vision"),
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

        catalog = catalog_for_model(catalogs, model["provider_id"], kind)
        metadata = catalog.get(model["model_id"])
        if metadata is None:
            entry["status"] = "unresolved"
            entry["reason"] = f"Model id not present in the {kind} catalog"
            entries.append(entry)
            continue

        # The provider declared a model class that cannot serve chat (a
        # reranker, a TTS engine, a document converter). Report it with its
        # reason but plan nothing: a context window on such a model is worse
        # than no metadata, because the picker then offers a model that fails
        # at request time. This branch runs before any metadata derivation so
        # reasoning and vision never even get a chance to propose a write.
        unsupported_model_type = metadata.get("unsupported_model_type")
        if unsupported_model_type and not include_unsupported_model_types:
            entry["status"] = "unsupported_model_type"
            entry["reason"] = (
                f"Model type '{unsupported_model_type}' cannot serve chat "
                "completions, so no metadata is written for it. Pass "
                "include_unsupported_model_types=true to plan it anyway for "
                "debugging."
            )
            entry["provider_metadata"] = {
                "model_type": metadata.get("model_type"),
                "unsupported_model_type": unsupported_model_type,
            }
            entries.append(entry)
            continue

        proposed = derive_copilot_limits(metadata)
        entry["proposed"] = proposed
        entry["provider_metadata"] = {
            "max_context_window_tokens": metadata.get("max_context_window_tokens"),
            "provider_max_output_tokens": metadata.get("max_output_tokens"),
            "supported_reasoning_efforts": metadata.get("supported_reasoning_efforts"),
            "default_reasoning_effort": metadata.get("default_reasoning_effort"),
            "reasoning_efforts_definitive": metadata.get(
                "reasoning_efforts_definitive", True
            ),
            "context_source": metadata.get("context_source"),
            "native_context_window_tokens": metadata.get(
                "native_context_window_tokens"
            ),
            "model_type": metadata.get("model_type"),
            "supports_vision": metadata.get("supports_vision"),
            "vision_media_types": metadata.get("vision_media_types") or [],
            "vision_source": metadata.get("vision_source"),
        }
        # oMLX's coarse toggle list is diagnostic only; omit it for providers
        # that never publish one rather than widening their output shape.
        if metadata.get("reasoning_levels") is not None:
            entry["provider_metadata"]["reasoning_levels"] = metadata[
                "reasoning_levels"
            ]
        if metadata.get("thinking_default") is not None:
            entry["provider_metadata"]["thinking_default"] = metadata[
                "thinking_default"
            ]

        changes: list[str] = []
        if proposed["max_prompt_tokens"] is not None and (
            proposed["max_prompt_tokens"] != model["max_prompt_tokens"]
        ):
            changes.append("max_prompt_tokens")
        if proposed["max_output_tokens"] is not None and (
            proposed["max_output_tokens"] != model["max_output_tokens"]
        ):
            changes.append("max_output_tokens")
        efforts_indeterminate = not proposed.get("reasoning_efforts_definitive", True)
        if not efforts_indeterminate and not _efforts_equal(
            model["supported_reasoning_efforts"], proposed["supported_reasoning_efforts"]
        ):
            changes.append("supported_reasoning_efforts")
        # Only an affirmative, definitive vision signal is ever proposed. A
        # text-only or unknown provider result must not clear an existing flag:
        # the column defaults to 0, so writing it back is a no-op at best, and
        # a wrong 1 makes the app offer image input to a model that rejects it.
        vision_available = model.get("supports_vision_available", True)
        if (
            proposed["supports_vision"] is True
            and model.get("supports_vision") != 1
        ):
            changes.append("supports_vision")

        entry["changes"] = changes
        # Set when the only reason the row looks stale is a vision flag the
        # schema cannot store; the write path uses it to avoid a no-op rewrite.
        entry["vision_unavailable"] = (
            not vision_available and "supports_vision" in changes
        )

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
        # Fifth bucket. The four above keep their meaning, so a consumer that
        # sums the original four still sees the same numbers as before.
        "unsupported_model_type": sum(
            1 for entry in entries if entry["status"] == "unsupported_model_type"
        ),
        # NOT a bucket: a strict subset of ``update``. Entries with at least
        # one row-changing column. Rows later suppressed by the legacy-vision
        # set are still counted here, since build_plan cannot see that set —
        # ``models_updated`` is the authoritative count of actual writes.
        # Do not add this to the bucket sum above.
        "update_row": sum(
            1 for entry in entries if entry["status"] == "update" and entry["changes"]
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
                "catalog_url": provider_catalog_url(
                    detect_provider_kind(provider), provider=provider
                ),
            }
            for provider in providers
        ],
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


#: The original three metadata columns. ``missing_metadata`` and both
#: ``has_metadata`` meanings are defined by this list; vision is not one of
#: them because it is best-effort.
_METADATA_FIELDS = (
    "max_prompt_tokens",
    "max_output_tokens",
    "supported_reasoning_efforts",
)


def _catalog_entry_has_vision(entry: dict[str, Any]) -> bool:
    """Whether one cached catalog entry advertises vision support.

    The patch writes ``capabilities.supports.vision = true``; older or
    unpatched entries leave the key absent. Only an explicit ``True`` counts,
    matching the write path's "never write false" rule.
    """
    capabilities = entry.get("capabilities") or {}
    supports = capabilities.get("supports") or {}
    return isinstance(supports, dict) and supports.get("vision") is True


def _catalog_entry_has_limits(entry: dict[str, Any]) -> bool:
    capabilities = entry.get("capabilities") or {}
    limits = capabilities.get("limits")
    return isinstance(limits, dict) and bool(limits)


def _catalog_entry_vision_media_types(entry: dict[str, Any]) -> list[str]:
    capabilities = entry.get("capabilities") or {}
    limits = capabilities.get("limits") or {}
    vision = limits.get("vision") if isinstance(limits, dict) else None
    if not isinstance(vision, dict):
        return []
    return [str(item) for item in vision.get("supported_media_types") or []]


def _catalog_entry_has_metadata(entry: dict[str, Any]) -> bool:
    """Whether the cached entry carries any provider-derived metadata."""
    return _catalog_entry_has_limits(entry) or _catalog_entry_has_vision(entry)


def _list_model_entry(
    model: dict[str, Any],
    provider: dict[str, Any],
    reasoning_effort: Optional[str],
    context_tier: Optional[str],
    metadata: Optional[dict[str, Any]],
    *,
    require_all_metadata: bool = True,
) -> dict[str, Any]:
    """Build one read-surface model entry, shared by both read tools.

    ``list_byok_models`` historically counts a model as having metadata when
    any of the three columns is populated; ``read_byok_config`` requires all
    three. ``require_all_metadata`` preserves both meanings.
    """
    efforts = model["supported_reasoning_efforts"]
    # ``missing_metadata`` deliberately stays the three original columns.
    # Vision is best-effort: a model without it is unknown, not incomplete.
    missing = [field for field in _METADATA_FIELDS if model[field] is None]
    metadata = metadata or {}
    # ``unsupported_model_type`` is reported only. A non-chat model is still
    # missing the same three columns, so neither ``missing_metadata`` nor
    # ``has_metadata`` may depend on it.
    unsupported_model_type = metadata.get("unsupported_model_type")
    # Provider metadata wins when the caller supplied it: a stored ``0`` is the
    # column default rather than a claim that the model is text-only. Without
    # metadata the stored value is all there is, and NULL means unknown.
    vision = metadata.get("supports_vision")
    if vision is None:
        vision = model.get("supports_vision")
    return {
        **model,
        "provider_name": provider.get("name"),
        "provider_kind": detect_provider_kind(provider) if provider else None,
        "reasoning_effort": reasoning_effort,
        "context_tier": context_tier,
        "has_metadata": (
            len(missing) < 3
            if require_all_metadata
            else any(model[field] is not None for field in _METADATA_FIELDS)
        ),
        "missing_metadata": missing,
        "supports_reasoning": bool(efforts),
        # Vision is tri-state: True, False, or None/absent for unknown.
        "supports_vision": None if vision is None else bool(vision),
        "vision_media_types": list(metadata.get("vision_media_types") or []),
        "vision_source": metadata.get("vision_source"),
        "model_type": metadata.get("model_type"),
        "supported_for_chat": metadata.get("supported_for_chat"),
        "unsupported_model_type": unsupported_model_type,
    }


def _catalog_cache_summary(
    cache: Any, entries: list[Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Summarize one cached catalog and collect its BYOK entries."""
    byok_entries = [
        entry for entry in entries if isinstance(entry, dict) and entry.get("providerId")
    ]
    summary: dict[str, Any] = {
        "present": True,
        "shape": "list" if isinstance(cache, list) else "object",
        "entry_count": len(entries),
        "byok_entry_count": len(byok_entries),
        "byok_entries_with_limits": sum(
            1 for entry in byok_entries if _catalog_entry_has_limits(entry)
        ),
        "byok_entries_with_vision": sum(
            1 for entry in byok_entries if _catalog_entry_has_vision(entry)
        ),
        "byok_entries_with_reasoning_efforts": sum(
            1 for entry in byok_entries if entry.get("supportedReasoningEfforts")
        ),
        "byok_entries_with_metadata": sum(
            1 for entry in byok_entries if _catalog_entry_has_metadata(entry)
        ),
    }
    catalog_entries: list[dict[str, Any]] = []
    for entry in byok_entries:
        capabilities = entry.get("capabilities") or {}
        limits = capabilities.get("limits") or {}
        catalog_entries.append(
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "providerId": entry.get("providerId"),
                "max_context_window_tokens": limits.get("max_context_window_tokens"),
                "max_prompt_tokens": limits.get("max_prompt_tokens"),
                "max_output_tokens": limits.get("max_output_tokens"),
                "has_limits": _catalog_entry_has_limits(entry),
                "supports_vision": _catalog_entry_has_vision(entry),
                "vision_media_types": _catalog_entry_vision_media_types(entry),
                "supportedReasoningEfforts": entry.get("supportedReasoningEfforts"),
                "defaultReasoningEffort": entry.get("defaultReasoningEffort"),
            }
        )
    return summary, catalog_entries


def list_byok_models(db_path: Optional[str] = None) -> dict[str, Any]:
    """List configured BYOK providers and models with their current metadata.

    ``model_type`` and ``supported_for_chat`` come from provider metadata,
    which the database does not store. This read path makes no network calls,
    so without a catalog both are ``None`` — unknown, never excluded.
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
    finally:
        conn.close()

    provider_by_id = {provider["id"]: provider for provider in providers}
    enriched = [
        _list_model_entry(
            model,
            provider_by_id.get(model["provider_id"], {}),
            reasoning_state.get(model["state_key"]),
            context_tier_state.get(model["state_key"]),
            None,
            require_all_metadata=False,
        )
        for model in models
    ]

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


def _catalog_metadata_for(
    model: dict[str, Any], catalog: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Look one model's normalized provider metadata out of a flat catalog."""
    if not isinstance(catalog, dict):
        return {}
    metadata = catalog.get(model["model_id"])
    return metadata if isinstance(metadata, dict) else {}


def read_byok_config(
    workspace_path: Optional[str] = None,
    db_path: Optional[str] = None,
    include_catalog_cache: bool = True,
    catalog: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return a complete, self-contained snapshot of the BYOK model config.

    This is the awareness entry point: a consuming agent gets every provider,
    every model, the metadata currently stored for each, the user's selected
    reasoning effort and context tier, and what is still missing — without
    locating or reading the app database itself.

    ``model_type`` and ``supported_for_chat`` come from provider metadata,
    which the database does not store. Pass ``catalog`` (a ``{model_id:
    metadata}`` mapping, the same shape ``plan_metadata_updates`` accepts) to
    surface them; without it both are ``None``, meaning unknown rather than
    excluded. This keeps the read path free of network calls.
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
        # Without a catalog the type is unknown (``None``), never "excluded".
        metadata = _catalog_metadata_for(model, catalog) if catalog else {}
        entry = _list_model_entry(
            model,
            provider,
            reasoning_state.get(model["state_key"]),
            context_tier_state.get(model["state_key"]),
            metadata,
        )
        kind = entry["provider_kind"]
        enriched_models.append(
            {**entry, "provider_supported": kind is not None}
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
                "catalog_url": provider_catalog_url(kind, provider=provider),
                "derives_reasoning_efforts": bool(spec)
                and spec.get("reasoning_efforts") is True,
                "reasoning_efforts_mode": (
                    spec.get("reasoning_efforts") if spec else None
                ),
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
        summary, catalog_entries = _catalog_cache_summary(cache, entries)
        catalog_cache_summary[cache_key] = summary
        if cache_key == APP_STATE_CATALOG_KEY:
            byok_catalog_entries.extend(catalog_entries)

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
            "byok_entries_with_vision": sum(
                1 for entry in byok_catalog_entries if entry["supports_vision"]
            ),
            "byok_entries_with_metadata": sum(
                1
                for entry in byok_catalog_entries
                if entry["has_limits"] or entry["supports_vision"]
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
            "models_with_vision": sum(
                1 for m in enriched_models if m["supports_vision"] is True
            ),
            "models_unsupported_model_type": sum(
                1 for m in enriched_models if m["unsupported_model_type"]
            ),
            "providers_with_supported_reasoning": sum(
                1
                for p in enriched_providers
                if p["reasoning_efforts_mode"] is True
            ),
            "providers_with_conditional_reasoning": sum(
                1
                for p in enriched_providers
                if p["reasoning_efforts_mode"] == "conditional"
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
    warnings: list[str] = []
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
            # Raw settings only so the oMLX admin fetch can authenticate; this
            # provider list never reaches a tool result or the plan.
            providers = load_providers(conn, redact=False)
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

        catalogs, warnings = collect_catalogs(
            providers, kinds, catalog_url=catalog_url
        )

    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    provider_by_id = {entry["id"]: entry for entry in providers}

    for model in models:
        provider_entry = provider_by_id.get(model["provider_id"])
        kind = detect_provider_kind(provider_entry) if provider_entry else None
        if kind is None:
            continue
        metadata = catalog_for_model(catalogs, model["provider_id"], kind).get(
            model["model_id"]
        )
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
        "catalog_sizes": {key: len(entries) for key, entries in catalogs.items()},
        "resolved": resolved,
        "missing": missing,
        "summary": {
            "resolved": len(resolved),
            "missing": len(missing),
        },
        "warnings": warnings,
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
    include_unsupported_model_types: bool = False,
) -> dict[str, Any]:
    """Compute a dry-run plan of metadata changes. Never writes."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn, redact=False)
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
        warnings: list[str] = []
    else:
        catalogs, warnings = collect_catalogs(
            providers, kinds, catalog_url=catalog_url
        )

    plan = build_plan(
        providers=providers,
        models=models,
        catalogs=catalogs,
        reasoning_state=reasoning_state,
        context_tier_state=context_tier_state,
        model_ids=model_ids,
        set_reasoning_defaults=set_reasoning_defaults,
        set_context_tier=set_context_tier,
        include_unsupported_model_types=include_unsupported_model_types,
    )
    plan["db_path"] = path
    plan["dry_run"] = True
    plan["warnings"] = warnings
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


class _CatalogPatch(tuple):
    """``(cache, patched)`` result that also exposes the match count.

    Unpacking as two values keeps existing callers unchanged, while
    ``.matched`` distinguishes "found the entry" from "changed the entry".
    """

    def __new__(cls, cache: Any, patched: int, matched: int) -> "_CatalogPatch":
        result = super().__new__(cls, (cache, patched))
        result.patched = patched
        result.matched = matched
        return result


def _patch_catalog_cache(
    cache: Any, updates: dict[str, dict[str, Any]]
) -> _CatalogPatch:
    """Patch BYOK entries in a cached model catalog.

    The result unpacks as ``(cache, patched)`` for existing callers, with
    ``matched`` available as a named attribute. ``patched`` counts entries
    whose cache representation actually changed; ``matched`` counts entries the
    update set found, whether or not they needed a change. The caller reports
    ``patched`` as work done and warns on ``matched == 0`` so an already
    in-sync cache is not mistaken for an unmatched one.

    Accepts both catalog shapes (see :func:`_catalog_entries`) and mutates the
    entries in place, so the caller can persist ``cache`` unchanged.
    """
    models = _catalog_entries(cache)
    if models is None:
        return _CatalogPatch(cache, 0, 0)

    patched = 0
    matched = 0
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
        matched += 1

        capabilities = entry.setdefault("capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
            entry["capabilities"] = capabilities

        limits = capabilities.get("limits")
        if not isinstance(limits, dict):
            limits = {}
            capabilities["limits"] = limits
        changed = False
        if update.get("max_prompt_tokens") is not None:
            if limits.get("max_prompt_tokens") != update["max_prompt_tokens"]:
                limits["max_prompt_tokens"] = update["max_prompt_tokens"]
                changed = True
        if update.get("max_output_tokens") is not None:
            if limits.get("max_output_tokens") != update["max_output_tokens"]:
                limits["max_output_tokens"] = update["max_output_tokens"]
                changed = True
        if update.get("max_context_window_tokens") is not None:
            if (
                limits.get("max_context_window_tokens")
                != update["max_context_window_tokens"]
            ):
                limits["max_context_window_tokens"] = update[
                    "max_context_window_tokens"
                ]
                changed = True

        supports = capabilities.get("supports")
        if not isinstance(supports, dict):
            supports = {}
            capabilities["supports"] = supports
        if update.get("patch_reasoning", True):
            efforts = update.get("supported_reasoning_efforts")
            if supports.get("reasoningEffort") != bool(efforts):
                supports["reasoningEffort"] = bool(efforts)
                changed = True
            if efforts:
                if entry.get("supportedReasoningEfforts") != list(efforts):
                    entry["supportedReasoningEfforts"] = list(efforts)
                    changed = True
                default_effort = update.get("default_reasoning_effort")
                if default_effort:
                    if entry.get("defaultReasoningEffort") != default_effort:
                        entry["defaultReasoningEffort"] = default_effort
                        changed = True
            else:
                for stale_key in ("supportedReasoningEfforts", "defaultReasoningEffort"):
                    if stale_key in entry:
                        entry.pop(stale_key, None)
                        changed = True

        # Vision is written only from definitive provider evidence. A model the
        # provider does not declare as vision-capable keeps whatever the cache
        # already says, because absence of evidence is not evidence of absence.
        if update.get("supports_vision") is True:
            if supports.get("vision") is not True:
                supports["vision"] = True
                changed = True
            media_types = update.get("vision_media_types") or []
            if media_types:
                vision = limits.get("vision")
                if not isinstance(vision, dict):
                    vision = {}
                    limits["vision"] = vision
                if list(vision.get("supported_media_types") or []) != list(media_types):
                    vision["supported_media_types"] = list(media_types)
                    changed = True

        if changed:
            patched += 1

    return _CatalogPatch(cache, patched, matched)


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
    include_unsupported_model_types: bool = False,
) -> dict[str, Any]:
    """Apply planned metadata updates to the Copilot app database."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Copilot app database not found at {path}")

    conn = connect(path, readonly=True)
    try:
        providers = load_providers(conn, redact=False)
        models = load_models(conn)
        reasoning_state = load_app_state(conn, APP_STATE_REASONING_KEY) or {}
        context_tier_state = load_app_state(conn, APP_STATE_CONTEXT_TIER_KEY) or {}
        skipped_vision = set(load_app_state(conn, APP_STATE_VISION_SKIPPED_KEY) or [])
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
        warnings: list[str] = []
    else:
        catalogs, warnings = collect_catalogs(
            providers, kinds, catalog_url=catalog_url
        )

    plan = build_plan(
        providers=providers,
        models=models,
        catalogs=catalogs,
        reasoning_state=reasoning_state,
        context_tier_state=context_tier_state,
        model_ids=model_ids,
        set_reasoning_defaults=set_reasoning_defaults,
        set_context_tier=set_context_tier,
        include_unsupported_model_types=include_unsupported_model_types,
    )

    result: dict[str, Any] = {
        "db_path": path,
        "dry_run": dry_run,
        "plan": plan,
        "warnings": warnings,
        "applied": {
            "models_updated": 0,
            "app_state_entries_updated": 0,
            "catalog_cache_entries_patched": 0,
            "catalog_caches_patched": {},
            "vision_flags_written": 0,
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
            # With no definitive reasoning evidence the row is untouched, so
            # the cache must be left alone too: passing an explicit ``False``
            # here would wipe an existing effort list on a single 401 run.
            "patch_reasoning": (entry.get("proposed") or {}).get(
                "reasoning_efforts_definitive", True
            ),
        }
        for entry in plan["entries"]
        if entry.get("proposed")
        and entry.get("provider_metadata")
        # Belt and braces: a non-chat model is never written, even if a future
        # change gives it a ``proposed`` block. The plan status is the gate.
        and entry["status"] != "unsupported_model_type"
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
                patched = _patch_catalog_cache(
                    json.loads(json.dumps(cache)), updates
                ).patched
                if patched:
                    cache_patch_counts[cache_key] = patched
        finally:
            probe.close()

    # A row whose only planned change is a vision flag the schema cannot store
    # is already as correct as it can be. The first run records those rows in
    # app state; later runs skip them so a legacy database settles instead of
    # counting a no-op rewrite (and a backup) on every run.
    #
    # An entry can need work (status == "update") without needing a row write:
    # set_context_tier / set_reasoning_defaults are app_state-only.
    row_updates = [
        entry
        for entry in plan["entries"]
        if entry["status"] == "update" and entry.get("proposed") and entry["changes"]
    ]
    vision_only_updates = [
        entry
        for entry in row_updates
        if entry.get("vision_unavailable")
        and set(entry.get("changes") or []) <= {"supports_vision"}
    ]
    already_skipped = [
        entry for entry in vision_only_updates if entry["state_key"] in skipped_vision
    ]
    effective_row_updates = [
        entry for entry in row_updates if entry not in already_skipped
    ]
    has_app_state_changes = any(
        entry["app_state_changes"] for entry in plan["entries"]
    )
    if not effective_row_updates and not has_app_state_changes and not cache_patch_counts:
        result["notes"].append("Nothing to apply — configuration already matches provider metadata")
        return result

    conn = connect(path, readonly=False)
    try:
        if backup:
            result["backup_path"] = _backup_database(conn, path)

        # A database created before ``supports_vision`` existed still gets every
        # other column written; only the vision assignment is dropped.
        has_vision_column = "supports_vision" in table_columns(conn, "provider_models")

        with conn:
            for entry in effective_row_updates:
                proposed = entry["proposed"]
                vision_flag = (
                    1 if proposed.get("supports_vision") is True else None
                )
                vision_assignment = (
                    ",\n                        supports_vision = COALESCE(?, supports_vision)"
                    if has_vision_column and vision_flag is not None
                    else ""
                )
                params: tuple[Any, ...] = (
                    proposed["max_prompt_tokens"],
                    proposed["max_output_tokens"],
                )
                # With no definitive reasoning evidence the column keeps its
                # value; only an authoritative provider list (or an explicit
                # empty one, for providers that declare efforts themselves)
                # may rewrite it.
                reasoning_assignment = ""
                if proposed.get("reasoning_efforts_definitive", True):
                    reasoning_assignment = (
                        ",\n                        supported_reasoning_efforts = ?"
                    )
                    params += (
                        None
                        if proposed["supported_reasoning_efforts"] is None
                        else json.dumps(proposed["supported_reasoning_efforts"]),
                    )
                if vision_assignment:
                    params += (vision_flag,)
                params += (entry["row_id"],)
                conn.execute(
                    f"""
                    UPDATE provider_models
                    SET max_prompt_tokens = COALESCE(?, max_prompt_tokens),
                        max_output_tokens = COALESCE(?, max_output_tokens)
                        {reasoning_assignment}{vision_assignment},
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    params,
                )
                result["applied"]["models_updated"] += 1
                if vision_assignment and "supports_vision" in entry["changes"]:
                    result["applied"]["vision_flags_written"] += 1

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

            if vision_only_updates:
                merged_skipped = set(skipped_vision)
                merged_skipped.update(
                    entry["state_key"] for entry in vision_only_updates
                )
                conn.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (APP_STATE_VISION_SKIPPED_KEY, json.dumps(sorted(merged_skipped))),
                )
                result["applied"]["app_state_entries_updated"] += 1

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
                # Track matches separately from changes: an in-sync cache
                # matches every entry but patches none, so the change count
                # cannot tell "found the entries" from "found nothing".
                matched_total = 0
                for cache_key in CATALOG_CACHE_KEYS:
                    cache = load_app_state(conn, cache_key)
                    if cache is None:
                        continue
                    matched_total += _patch_catalog_cache(
                        json.loads(json.dumps(cache)), updates
                    ).matched
                if not matched_total and updates:
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
                "catalog_url": spec.get("catalog_url"),
                "catalog_path": spec.get("catalog_path"),
                "status_path": spec.get("status_path"),
                "derives_catalog_url_from": spec.get("base_url_params", []),
                "endpoints_url_template": spec.get("endpoints_url_template"),
                "match_hosts": spec.get("match_hosts", []),
                "match_names": spec.get("match_names", []),
                "reasoning_efforts": spec.get("reasoning_efforts"),
                "reasoning_effort_fallback": spec.get("reasoning_effort_fallback"),
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
                "catalog": {
                    "type": "object",
                    "description": (
                        "Optional provider metadata mapping (model_id to normalized "
                        "metadata) used to report each model's model_type and "
                        "supported_for_chat. Without it both are null, meaning "
                        "unknown rather than excluded."
                    ),
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
            "Fetches authoritative context-window metadata from each provider's "
            "source of truth (OpenRouter's catalog, or a local oMLX server's "
            "/v1/models and /v1/models/status) for every configured BYOK model, "
            "and returns the values derived for Copilot. Reasoning-effort metadata "
            "is only derived for providers that advertise a level set, so oMLX "
            "models report none."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional provider kind to query: 'openrouter' or 'omlx'."
                    ),
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
                "include_unsupported_model_types": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Plan models whose declared oMLX type cannot serve chat "
                        "(reranker, audio_tts, markitdown). Off by default: those "
                        "models are reported as unsupported_model_type and never "
                        "written. Enable only to debug a misclassified model."
                    ),
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
                "include_unsupported_model_types": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Write models whose declared oMLX type cannot serve chat "
                        "(reranker, audio_tts, markitdown). Off by default: those "
                        "models are reported but never written, because a context "
                        "window on a non-chat model makes the picker offer a model "
                        "that fails at request time."
                    ),
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
                    catalog=tool_args.get("catalog"),
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
                    include_unsupported_model_types=tool_args.get(
                        "include_unsupported_model_types", False
                    ),
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
                    include_unsupported_model_types=tool_args.get(
                        "include_unsupported_model_types", False
                    ),
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
