#!/usr/bin/env sh
# scripts/dashboard-serve.sh — serve the operator dashboard on LOOPBACK ONLY.
#
# The address flag is load-bearing, not a default: streamlit's own default binds every
# interface, and this UI renders journal text and staged drafts. A remote operator
# tunnels in instead of the port opening up:
#
#     ssh -L 8502:127.0.0.1:8502 <host>     then browse http://127.0.0.1:8502
#
# tests/test_dashboard.py asserts the loopback bind and tests/mutation_check.sh
# proves that assertion has teeth. COMMS_SETTINGS (default ./settings.json) and
# DASHBOARD_PORT (default 8502) pass through from the environment.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# python3 -m, not the `streamlit` console script: any environment that can import
# streamlit can run this, including installs that never put the entry point on PATH
# (found live — this host imports streamlit fine and has no `streamlit` binary).
exec python3 -m streamlit run "$HERE/dashboard.py" \
  --server.address 127.0.0.1 \
  --server.port "${DASHBOARD_PORT:-8502}" \
  --server.headless true \
  --browser.gatherUsageStats false
