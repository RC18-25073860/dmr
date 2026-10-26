#!/bin/bash
# Source from Bash project scripts. All writable runtime paths stay under the project directory.
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Please run this script with bash; zsh sourcing is not supported.\n' >&2
  return 1 2>/dev/null || exit 1
fi
DMR_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DMR_PROJECT_ROOT"
mkdir -p .runtime/tmp .runtime/cache .runtime/pip-cache .runtime/npm-cache .runtime/pycache logs artifacts/stack-migration
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$DMR_PROJECT_ROOT/.runtime/pycache"
export TMPDIR="$DMR_PROJECT_ROOT/.runtime/tmp"
export XDG_CACHE_HOME="$DMR_PROJECT_ROOT/.runtime/cache"
export PIP_CACHE_DIR="$DMR_PROJECT_ROOT/.runtime/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export npm_config_cache="$DMR_PROJECT_ROOT/.runtime/npm-cache"
export npm_config_update_notifier=false
export npm_config_audit=false
export npm_config_fund=false
export npm_config_userconfig="$DMR_PROJECT_ROOT/.npmrc"
export npm_config_devdir="$DMR_PROJECT_ROOT/.runtime/node-gyp"
export npm_config_ignore_scripts=true
