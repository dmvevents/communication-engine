#!/usr/bin/env bash
# install-hooks.sh — wire the sanitize gate into local git hooks (pre-commit + pre-push).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
for hook in pre-commit pre-push; do
  cat > "$ROOT/.git/hooks/$hook" <<'EOF'
#!/usr/bin/env sh
exec "$(git rev-parse --show-toplevel)/scripts/sanitize-gate.sh"
EOF
  chmod +x "$ROOT/.git/hooks/$hook"
  echo "installed $hook hook"
done
