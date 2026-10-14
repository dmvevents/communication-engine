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
#   commit-msg -> sanitize gate over the commit MESSAGE (scripts/sanitize-text.sh): the
#                 tree gate never sees message text, and messages are history once pushed
#   pre-push   -> sanitize gate + unit tests + mutation check (the full gate set)
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
# The hooks directory as git resolves it: a fresh `git init` on a host with an empty template
# has none, and in a linked worktree it lives in the common .git directory, not under $ROOT.
HOOKS="$(git rev-parse --path-format=absolute --git-path hooks)"
mkdir -p "$HOOKS"

cat > "$HOOKS/pre-commit" <<'EOF'
#!/usr/bin/env bash
R="$(git rev-parse --show-toplevel)"
exec bash "$R/scripts/sanitize-gate.sh"
EOF

cat > "$HOOKS/commit-msg" <<'EOF'
#!/usr/bin/env bash
R="$(git rev-parse --show-toplevel)"
exec bash "$R/scripts/sanitize-text.sh" "$1"
EOF

# NOTE: invoke the gates with BASH explicitly. /bin/sh is dash here, which lacks
# `set -o pipefail` — running mutation_check.sh under sh fails on line 11 and blocks the
# push for a shell-incompatibility reason rather than a real finding.
cat > "$HOOKS/pre-push" <<'EOF'
#!/usr/bin/env bash
R="$(git rev-parse --show-toplevel)"
set -e
bash "$R/scripts/sanitize-gate.sh"
python3 -m unittest discover -s "$R/tests" -q
bash "$R/tests/mutation_check.sh"
EOF

chmod +x "$HOOKS/pre-commit" "$HOOKS/commit-msg" "$HOOKS/pre-push"
echo "installed pre-commit (sanitize) + commit-msg (sanitize over the message) + pre-push (sanitize + tests + mutation check)"
