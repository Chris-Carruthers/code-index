#!/bin/sh
# One-command setup: build the venv, install, index your checkouts, and print
# the MCP registration command. Safe to re-run.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "codeindex needs Python 3.11+ (found: $("$PY" --version 2>&1))" >&2
  exit 1
}

[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e .

echo
echo "Indexing your local checkouts..."
./bin/codeindex index || {
  echo
  echo "No checkouts found. Tell codeindex where your repos are, then re-run:" >&2
  echo "  echo ~/path/to/repos > ~/.config/code-index/roots" >&2
  exit 1
}

echo
echo "Register with Claude Code:"
echo "  claude mcp add code-index --scope user -- $ROOT/bin/codeindex mcp"
