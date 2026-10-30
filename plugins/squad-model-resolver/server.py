#!/usr/bin/env python3
"""squad-model-resolver MCP server.

Deterministic model resolver for Squad agent sessions:
- Resolves provider-qualified references (e.g. openrouter://z-ai/glm-5.3)
- Resolves built-in references with configurable fallback chains
- Applies Squad config layering (config override -> session directive -> charter -> auto)
- Provides post-spawn verification
- Read-only on .squad
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_FALLBACK_CHAINS = {
    "premium": [
        "gpt-6.1-sol",
        "gpt-6-sol",
        "gpt-5.6-sol",
        "claude-opus-5.5",
        "claude-opus-5",
        "claude-opus-4.8",
        "claude-sonnet-5.5",
    ],
    "standard": [
        "claude-sonnet-5.5",
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "gpt-5.6-sol",
        "claude-fable-5.1",
        "claude-fable-5",
    ],
    "fast": [
        "gpt-6-luna",
        "gpt-5.6-luna",
        "claude-haiku-5.5",
        "claude-haiku-4.5",
    ],
}


def parse_reference(reference: str) -> tuple[Optional[str], str]:
    """Parse reference into (provider, model_path).

    Provider-qualified: scheme://path -> (scheme, path)
    Unqualified: path -> (None, path)
    """
    if "://" in reference:
        provider, path = reference.split("://", 1)
        return provider.strip(), path.strip()
    return None, reference.strip()


def load_user_registry(config_path: Optional[str] = None) -> dict[str, str]:
    """Load user-level provider registry from ~/.config/squad-model-resolver/providers.json.

    Can be overridden via SQUAD_MODEL_RESOLVER_CONFIG environment variable or parameter.
    """
    path = config_path or os.environ.get("SQUAD_MODEL_RESOLVER_CONFIG")
    if not path:
        path = os.path.expanduser("~/.config/squad-model-resolver/providers.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                if "providers" in data and isinstance(data["providers"], dict):
                    return {str(k): str(v) for k, v in data["providers"].items()}
                return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}
    except Exception as exc:
        sys.stderr.write(f"squad-model-resolver: error loading config from {path}: {exc}\n")
    return {}


def get_effective_registry(user_reg: dict[str, str], caller_reg: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """Merge caller registry with authoritative user registry.

    User registry entries CANNOT be overwritten or forged by the caller.
    """
    effective: dict[str, str] = {}
    if caller_reg and isinstance(caller_reg, dict):
        if "providers" in caller_reg and isinstance(caller_reg["providers"], dict):
            effective.update({str(k): str(v) for k, v in caller_reg["providers"].items()})
        else:
            effective.update({str(k): str(v) for k, v in caller_reg.items() if isinstance(v, (str, int))})
    effective.update(user_reg)
    return effective


def resolve_model(
    reference: str,
    catalog: list[str],
    provider_registry: Optional[dict[str, Any]] = None,
    fallback_chain: Optional[list[str]] = None,
    max_fallbacks: int = 3,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a model reference deterministically against the catalog."""
    provider, path = parse_reference(reference)
    user_reg = load_user_registry(config_path)
    effective_reg = get_effective_registry(user_reg, provider_registry)

    # 1. Provider-qualified references: scheme://path
    if provider is not None:
        # Match exact model path
        path_matches = [
            c for c in catalog
            if c == path or c.endswith("/" + path)
        ]

        if not path_matches:
            return {
                "status": "unresolvable",
                "model_id": None,
                "provider": provider,
                "path": path,
                "candidates": [],
                "reason": f"No model matching path '{path}' found in catalog; provider-qualified references never fall back.",
            }

        if len(path_matches) > 1:
            return {
                "status": "ambiguous",
                "model_id": None,
                "provider": provider,
                "path": path,
                "candidates": path_matches,
                "reason": f"Multiple exact-path matches for '{path}' found in catalog: {path_matches}.",
            }

        candidate = path_matches[0]
        if candidate == path:
            candidate_prefix = ""
        else:
            candidate_prefix = candidate[:-len("/" + path)]

        expected_prefix = effective_reg.get(provider)
        if not expected_prefix:
            return {
                "status": "unresolvable",
                "model_id": None,
                "provider": provider,
                "path": path,
                "candidates": [],
                "reason": (
                    f"Candidate '{candidate}' matches path '{path}', but provider '{provider}' has no registry metadata "
                    f"tying it to prefix '{candidate_prefix}'. Never infer a provider from a prefix without registry metadata."
                ),
            }

        if candidate_prefix != expected_prefix:
            return {
                "status": "unresolvable",
                "model_id": None,
                "provider": provider,
                "path": path,
                "candidates": [],
                "reason": (
                    f"Candidate prefix '{candidate_prefix}' does not match registered prefix '{expected_prefix}' "
                    f"for provider '{provider}'."
                ),
            }

        return {
            "status": "resolved",
            "model_id": candidate,
            "provider": provider,
            "path": path,
            "candidates": [candidate],
            "reason": f"Resolved '{reference}' to '{candidate}' via provider '{provider}' and registry prefix '{expected_prefix}'.",
        }

    # 2. Built-in (unqualified) references
    normalized_ref = path
    if normalized_ref.lower() == "auto":
        normalized_ref = "claude-sonnet-5.5"

    if normalized_ref in catalog:
        return {
            "status": "resolved",
            "model_id": normalized_ref,
            "provider": None,
            "path": path,
            "candidates": [normalized_ref],
            "reason": f"Resolved built-in model '{normalized_ref}' directly from catalog.",
        }

    # Fallback chain for built-in models
    chain: list[str] = []
    if fallback_chain is not None:
        chain = list(fallback_chain)
    else:
        # Determine chain from defaults
        for tier_name, tier_chain in DEFAULT_FALLBACK_CHAINS.items():
            if normalized_ref in tier_chain:
                idx = tier_chain.index(normalized_ref)
                chain = tier_chain[idx + 1:]
                break
        if not chain:
            chain = list(DEFAULT_FALLBACK_CHAINS["standard"])

    candidates_tested = 0
    for fallback_model in chain:
        if candidates_tested >= max_fallbacks:
            break
        candidates_tested += 1
        if fallback_model in catalog:
            return {
                "status": "resolved",
                "model_id": fallback_model,
                "provider": None,
                "path": path,
                "candidates": [fallback_model],
                "reason": f"Fallback from '{reference}' to '{fallback_model}' (retry {candidates_tested}).",
            }

    if fallback_chain is not None and not fallback_chain:
        return {
            "status": "unresolvable",
            "model_id": None,
            "provider": None,
            "path": path,
            "candidates": [],
            "reason": f"Built-in model '{reference}' not found in catalog and fallback chain is empty.",
        }

    # Exhausted retries -> platform default fallback (omit model parameter)
    return {
        "status": "resolved",
        "model_id": None,
        "provider": None,
        "path": path,
        "candidates": [],
        "reason": f"Fallback chain for '{reference}' exhausted after {candidates_tested} retries; using platform default (omit model).",
    }


def read_squad_config_models(repo_path: str, agent_name: str) -> tuple[Optional[str], Optional[str]]:
    """Read agentModelOverrides and defaultModel from .squad/config.json."""
    config_file = Path(repo_path) / ".squad" / "config.json"
    if not config_file.is_file():
        return None, None
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        sys.stderr.write(f"squad-model-resolver: error reading {config_file}: {exc}\n")
        return None, None

    agent_override = None
    default_model = None
    if isinstance(data, dict):
        overrides = data.get("agentModelOverrides")
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                if k.lower() == agent_name.lower() and isinstance(v, str) and v.strip() and v.strip().lower() != "auto":
                    agent_override = v.strip()
                    break
        def_m = data.get("defaultModel")
        if isinstance(def_m, str) and def_m.strip() and def_m.strip().lower() != "auto":
            default_model = def_m.strip()
    return agent_override, default_model


def read_agent_charter_preferred_model(repo_path: str, agent_name: str) -> Optional[str]:
    """Read ## Model -> Preferred field from agent's charter.md."""
    agents_dir = Path(repo_path) / ".squad" / "agents"
    if not agents_dir.is_dir():
        return None
    target_dir = None
    for entry in agents_dir.iterdir():
        if entry.is_dir() and entry.name.lower() == agent_name.lower():
            target_dir = entry
            break
    if not target_dir:
        return None
    charter_file = target_dir / "charter.md"
    if not charter_file.is_file():
        return None
    try:
        content = charter_file.read_text(encoding="utf-8")
    except Exception as exc:
        sys.stderr.write(f"squad-model-resolver: error reading {charter_file}: {exc}\n")
        return None

    model_section_match = re.search(r"##\s*Model\b(.*?)(?=\n##|\Z)", content, re.DOTALL | re.IGNORECASE)
    if not model_section_match:
        return None
    section_text = model_section_match.group(1)
    pref_match = re.search(r"\*{0,2}Preferred:\*{0,2}\s*`?([^`\n\r]+)`?", section_text, re.IGNORECASE)
    if pref_match:
        val = pref_match.group(1).strip()
        if val and val.lower() != "auto":
            return val
    return None


def resolve_for_agent(
    repo_path: str,
    agent_name: str,
    catalog: list[str],
    session_directive: Optional[str] = None,
    provider_registry: Optional[dict[str, Any]] = None,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve model for a specific Squad agent following precedence layers:

    config override -> session directive -> charter -> config default -> auto
    """
    agent_override, default_model = read_squad_config_models(repo_path, agent_name)

    chosen_reference: str = ""
    layer_won: str = ""

    if agent_override:
        chosen_reference = agent_override
        layer_won = "config override"
    elif session_directive and session_directive.strip() and session_directive.strip().lower() != "auto":
        chosen_reference = session_directive.strip()
        layer_won = "session directive"
    else:
        charter_pref = read_agent_charter_preferred_model(repo_path, agent_name)
        if charter_pref:
            chosen_reference = charter_pref
            layer_won = "charter"
        elif default_model:
            chosen_reference = default_model
            layer_won = "config default"
        else:
            chosen_reference = "auto"
            layer_won = "auto"

    res = resolve_model(
        reference=chosen_reference,
        catalog=catalog,
        provider_registry=provider_registry,
        config_path=config_path,
    )
    res["layer_won"] = layer_won
    res["agent_name"] = agent_name
    res["reference"] = chosen_reference
    return res


def verify_spawn(
    selected_model_id: Optional[str],
    reported_model_id: Optional[str],
    has_provider_metadata: bool = False,
) -> dict[str, Any]:
    """Verify spawned session model ID against selected model ID."""
    match = selected_model_id == reported_model_id
    if match:
        status = "match"
        if has_provider_metadata:
            provenance_verified = True
            msg = f"Model ID '{selected_model_id}' matches. Provider provenance verified via catalog metadata."
        else:
            provenance_verified = False
            msg = (
                f"Model ID '{selected_model_id}' matches. Verification proves that the selected model ID was used, "
                "but does not prove provider provenance when catalog lacks metadata."
            )
    else:
        status = "mismatch"
        provenance_verified = False
        msg = f"Model ID mismatch: selected '{selected_model_id}' but spawned session reported '{reported_model_id}'."

    return {
        "match": match,
        "status": status,
        "selected_model_id": selected_model_id,
        "reported_model_id": reported_model_id,
        "provenance_verified": provenance_verified,
        "message": msg,
    }


# ---------------------------------------------------------------------------
# MCP Protocol Handlers
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "resolve_model",
        "description": "Deterministically resolves a model reference (built-in or provider-qualified like openrouter://...) against the harness model catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Model reference string, e.g. 'openrouter://z-ai/glm-5.3' or 'claude-sonnet-5.5'.",
                },
                "catalog": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of available model IDs exposed by the harness.",
                },
                "provider_registry": {
                    "type": "object",
                    "description": "Optional mapping of provider names to ID prefixes.",
                },
                "fallback_chain": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional custom fallback chain for built-in models.",
                },
                "max_fallbacks": {
                    "type": "integer",
                    "default": 3,
                    "description": "Maximum number of fallback retries before falling back to platform default.",
                },
            },
            "required": ["reference", "catalog"],
        },
    },
    {
        "name": "resolve_for_agent",
        "description": "Resolves target model for a Squad agent by evaluating .squad/config.json, session directives, and agent charters in strict layer precedence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repository root containing .squad.",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Name of the agent, e.g. 'lead', 'tester', etc.",
                },
                "catalog": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of available model IDs exposed by the harness.",
                },
                "session_directive": {
                    "type": "string",
                    "description": "Optional session-level model directive.",
                },
                "provider_registry": {
                    "type": "object",
                    "description": "Optional mapping of provider names to ID prefixes.",
                },
            },
            "required": ["repo_path", "agent_name", "catalog"],
        },
    },
    {
        "name": "verify_spawn",
        "description": "Verifies that the spawned session reported the model ID selected before spawn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selected_model_id": {
                    "type": ["string", "null"],
                    "description": "Model ID passed to the spawn task tool (or null for platform default).",
                },
                "reported_model_id": {
                    "type": ["string", "null"],
                    "description": "Model ID reported by the spawned session.",
                },
                "has_provider_metadata": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether the catalog contains independent provider metadata.",
                },
            },
            "required": ["selected_model_id", "reported_model_id"],
        },
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
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "squad-model-resolver",
                    "version": "0.1.0",
                },
            },
        }

    if method == "server/discover":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-11-25", "2024-11-05"],
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "squad-model-resolver",
                        "version": "0.1.0",
                    },
                },
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {},
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": TOOLS,
            },
        }

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})

        try:
            if tool_name == "resolve_model":
                res = resolve_model(
                    reference=tool_args.get("reference", ""),
                    catalog=tool_args.get("catalog", []),
                    provider_registry=tool_args.get("provider_registry"),
                    fallback_chain=tool_args.get("fallback_chain"),
                    max_fallbacks=tool_args.get("max_fallbacks", 3),
                )
            elif tool_name == "resolve_for_agent":
                res = resolve_for_agent(
                    repo_path=tool_args.get("repo_path", ""),
                    agent_name=tool_args.get("agent_name", ""),
                    catalog=tool_args.get("catalog", []),
                    session_directive=tool_args.get("session_directive"),
                    provider_registry=tool_args.get("provider_registry"),
                )
            elif tool_name == "verify_spawn":
                res = verify_spawn(
                    selected_model_id=tool_args.get("selected_model_id"),
                    reported_model_id=tool_args.get("reported_model_id"),
                    has_provider_metadata=tool_args.get("has_provider_metadata", False),
                )
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
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(res, indent=2),
                        }
                    ],
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
                        {
                            "type": "text",
                            "text": f"Error executing {tool_name}: {exc}",
                        }
                    ],
                    "isError": True,
                },
            }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not found",
            },
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
            sys.stderr.write(f"squad-model-resolver: malformed JSON request: {exc}\n")
            continue

        resp = handle_rpc_message(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
