#!/usr/bin/env bash
# change-learn MCP stdio host launcher.
#
# Started by mcp-protocol-bridge.mjs. Builds the ChangeLearn.Host once
# (incremental, quiet) and execs it with --stdio. stdout is reserved for the
# MCP protocol; all diagnostics go to stderr.
#
# Host directory resolution order:
#   1. $CHANGE_LEARN_HOST_DIR
#   2. ../../hosts/ChangeLearn.Host relative to this script (source checkout)
#   3. ~/.config/change-learn/host-dir (file containing the absolute path)
set -euo pipefail

err() { echo "change-learn: $*" >&2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

host_dir=""
if [[ -n "${CHANGE_LEARN_HOST_DIR:-}" ]]; then
  host_dir="$CHANGE_LEARN_HOST_DIR"
elif [[ -f "$script_dir/../../../hosts/ChangeLearn.Host/ChangeLearn.Host.csproj" ]]; then
  host_dir="$(cd "$script_dir/../../../hosts/ChangeLearn.Host" && pwd)"
elif [[ -f "$HOME/.config/change-learn/host-dir" ]]; then
  host_dir="$(head -n1 "$HOME/.config/change-learn/host-dir")"
fi

if [[ -z "$host_dir" || ! -f "$host_dir/ChangeLearn.Host.csproj" ]]; then
  err "cannot locate ChangeLearn.Host (looked at CHANGE_LEARN_HOST_DIR, source checkout, ~/.config/change-learn/host-dir)."
  err "clone matthewcorven/corven-agent-tools, then either:"
  err "  export CHANGE_LEARN_HOST_DIR=<clone>/hosts/ChangeLearn.Host"
  err "or:"
  err "  mkdir -p ~/.config/change-learn && echo <clone>/hosts/ChangeLearn.Host > ~/.config/change-learn/host-dir"
  exit 1
fi

if ! command -v dotnet >/dev/null 2>&1; then
  err "dotnet SDK not found on PATH (requires .NET 10)."
  exit 1
fi

# Quiet incremental build; MSBuild output must not touch stdout.
dotnet build "$host_dir/ChangeLearn.Host.csproj" -c Release -v quiet --nologo 1>&2

dll="$host_dir/bin/Release/net10.0/ChangeLearn.Host.dll"
if [[ ! -f "$dll" ]]; then
  err "build succeeded but $dll not found."
  exit 1
fi

# Analyze the repo the harness launched us in, unless overridden.
export ChangeLearn__RepoPath="${CHANGE_LEARN_REPO:-$PWD}"

exec dotnet "$dll" --stdio
