#!/usr/bin/env bash
# Full test suite, network-blocked.
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/run_offline.sh" python3 -m unittest discover -s tests "$@"
