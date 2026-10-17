#!/usr/bin/env bash
# Demo CLI, network-blocked. Extra args go to the demo, e.g. scripts/demo.sh --scenario crash
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/run_offline.sh" python3 -m jarvis_workforce.demo "$@"
