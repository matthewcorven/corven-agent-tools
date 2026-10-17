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

# Keep stdio-host builds out of the source checkout's obj directory. A harness
# may start more than one server at once, or a developer may build the host at
# the same time; Static Web Assets caches use exclusive locks in obj.
build_root="${TMPDIR:-/tmp}/change-learn-host-build"
build_lock="$build_root/.lock"
mkdir -p "$build_root"

missing_owner_attempts=0
while ! mkdir "$build_lock" 2>/dev/null; do
  if [[ -f "$build_lock/pid" ]]; then
    lock_owner="$(head -n1 "$build_lock/pid" 2>/dev/null || true)"
    if [[ "$lock_owner" =~ ^[0-9]+$ ]] && ! kill -0 "$lock_owner" 2>/dev/null; then
      rm -f "$build_lock/pid"
      rmdir "$build_lock" 2>/dev/null || true
      continue
    fi
    missing_owner_attempts=0
  else
    missing_owner_attempts=$((missing_owner_attempts + 1))
    if (( missing_owner_attempts >= 20 )); then
      rmdir "$build_lock" 2>/dev/null || true
      missing_owner_attempts=0
      continue
    fi
  fi
  sleep 0.1
done
printf '%s\n' "$$" > "$build_lock/pid"

release_build_lock() {
  rm -f "$build_lock/pid"
  rmdir "$build_lock" 2>/dev/null || true
}
trap release_build_lock EXIT
trap 'release_build_lock; exit 130' HUP INT TERM

# Quiet incremental build; MSBuild output must not touch stdout.
dotnet build "$host_dir/ChangeLearn.Host.csproj" -c Release -v quiet --nologo \
  -p:ChangeLearnStdioBuild=true \
  -p:ChangeLearnStdioIntermediatePath="$build_root/obj/" 1>&2

dll="$host_dir/bin/Release/net10.0/ChangeLearn.Host.dll"
if [[ ! -f "$dll" ]]; then
  err "build succeeded but $dll not found."
  exit 1
fi

# Analyze the repo the harness launched us in, unless overridden.
export ChangeLearn__RepoPath="${CHANGE_LEARN_REPO:-$PWD}"
# The skill scripts live beside this launcher in the installed plugin, not in
# the consumer repository that the MCP host analyzes.
export ChangeLearn__SkillScriptsPath="${CHANGE_LEARN_SKILL_SCRIPTS_PATH:-$script_dir/../skills/change-learn/scripts}"

exec dotnet "$dll" --stdio
