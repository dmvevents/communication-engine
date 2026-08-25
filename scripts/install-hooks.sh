#!/usr/bin/env bash
# install-hooks.sh — wire the gates into local git hooks.
#
# WHY LOCAL HOOKS MATTER HERE: GitHub Actions on this account is currently blocked
# ("recent account payments have failed or your spending limit needs to be increased"),
# so every workflow run fails in ~5s having executed ZERO steps. A control that cannot
# run is not a control, so the local hooks are the compensating control until billing is
# restored. Re-verify with: gh run list -R <repo> --limit 3
#
#   pre-commit -> sanitize gate (fast; blocks a secret from ever entering a commit)
#   pre-push   -> sanitize gate + unit tests + mutation check (the full gate set)
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"

cat > "$ROOT/.git/hooks/pre-commit" <<'EOF'
#!/usr/bin/env sh
R="$(git rev-parse --show-toplevel)"
exec "$R/scripts/sanitize-gate.sh"
EOF

cat > "$ROOT/.git/hooks/pre-push" <<'EOF'
#!/usr/bin/env sh
R="$(git rev-parse --show-toplevel)"
set -e
"$R/scripts/sanitize-gate.sh"
python3 -m unittest discover -s "$R/tests" -q
sh "$R/tests/mutation_check.sh"
EOF

chmod +x "$ROOT/.git/hooks/pre-commit" "$ROOT/.git/hooks/pre-push"
echo "installed pre-commit (sanitize) + pre-push (sanitize + tests + mutation check)"
