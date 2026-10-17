#!/usr/bin/env bash
# Run a command with networking blocked.
#   Layer 1 (if available): empty network namespace via `unshare -rn` (no root needed). Only a down loopback
#           exists, so nothing - Python, curl, subprocesses, C code - can reach any host.
#   Layer 2 (always): Python socket guard (scripts/offline/sitecustomize.py) for the whole Python process tree.
# If unshare is unavailable (macOS, restricted containers/CI), only layer 2 applies; this is printed on stderr.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
export PYTHONPATH="$HERE/offline:$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" JARVIS_OFFLINE=1
cd "$ROOT"
if command -v unshare >/dev/null 2>&1 && unshare -rn true 2>/dev/null; then
  echo "[run_offline] layers: netns (unshare -rn) + python socket guard" >&2
  exec unshare -rn env JARVIS_NETNS=1 "$@"
else
  echo "[run_offline] layers: python socket guard only (unshare unavailable)" >&2
  exec env JARVIS_NETNS=0 "$@"
fi
