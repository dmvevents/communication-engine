#!/usr/bin/env bash
# install-hooks.sh — wire the gates into local git hooks.
#
# WHY LOCAL HOOKS MATTER HERE: hosted CI can be blocked for reasons outside the
# repository, and a blocked GitHub Actions run fails in ~5s having executed ZERO steps
# while still listing as completed. A control that cannot run is not a control, so the
# local hooks are the compensating control whatever CI does.
# Re-verify CI with: gh run list -R <repo> --limit 3
#
#   pre-commit -> sanitize gate (fast; blocks a secret from ever entering a commit)
#   pre-push   -> sanitize gate + unit tests + mutation check (the full gate set)
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"

cat > "$ROOT/.git/hooks/pre-commit" <<'EOF'
#!/usr/bin/env bash
R="$(git rev-parse --show-toplevel)"
exec bash "$R/scripts/sanitize-gate.sh"
EOF

# NOTE: invoke the gates with BASH explicitly. /bin/sh is dash here, which lacks
# `set -o pipefail` — running mutation_check.sh under sh fails on line 11 and blocks the
# push for a shell-incompatibility reason rather than a real finding.
cat > "$ROOT/.git/hooks/pre-push" <<'EOF'
#!/usr/bin/env bash
R="$(git rev-parse --show-toplevel)"
set -e
bash "$R/scripts/sanitize-gate.sh"
python3 -m unittest discover -s "$R/tests" -q
bash "$R/tests/mutation_check.sh"
EOF

chmod +x "$ROOT/.git/hooks/pre-commit" "$ROOT/.git/hooks/pre-push"
echo "installed pre-commit (sanitize) + pre-push (sanitize + tests + mutation check)"
