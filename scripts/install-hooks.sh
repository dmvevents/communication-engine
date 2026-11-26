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
#   commit-msg -> sanitize gate over the commit MESSAGE (scripts/sanitize-text.sh): the
#                 tree gate never sees message text, and messages are history once pushed
#   pre-push   -> sanitize gate + unit tests + mutation check (the full gate set)
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
# This clone's own hooks directory, in the common .git directory: a fresh `git init` on a host
# with an empty template has none, and a linked worktree keeps it there, not under $ROOT. Never
# the directory core.hooksPath names: that one is often shared by every repository on the host,
# and these hooks would then run this repository's gates in all of them.
HOOKS="$(git rev-parse --path-format=absolute --git-common-dir)/hooks"
mkdir -p "$HOOKS"
if ! [ "$(git rev-parse --path-format=absolute --git-path hooks)" -ef "$HOOKS" ]; then
  echo "install-hooks: WARNING core.hooksPath ($(git config --get core.hooksPath)) points git at another" \
       "hooks directory, so the hooks installed in $HOOKS do not run in this clone until it points" \
       "here: git config core.hooksPath \"$HOOKS\"" >&2
fi

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
# the loopback-only network guard: a test that reaches a third host fails loudly
PYTHONPATH="$R/tests/netguard" python3 -m unittest discover -s "$R/tests" -q
PYTHONPATH="$R/tests/netguard" bash "$R/tests/mutation_check.sh"
EOF

chmod +x "$HOOKS/pre-commit" "$HOOKS/commit-msg" "$HOOKS/pre-push"
echo "installed pre-commit (sanitize) + commit-msg (sanitize over the message) + pre-push (sanitize + tests + mutation check)"
