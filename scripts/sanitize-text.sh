#!/usr/bin/env bash
# sanitize-text.sh — the sanitize gate for TEXT that never lands in the tree: commit messages,
# pull-request titles and bodies, issue bodies. The gate scans filesystem targets, so the text is
# written to a private temporary directory (mode 0700, removed at exit) and scanned there with the
# same pattern classes and the same local denylist. The commit-msg hook that
# scripts/install-hooks.sh installs runs this on every commit message before the commit exists.
#
#   sanitize-text.sh [FILE]             scan FILE (default: stdin)
#   sanitize-text.sh --commits RANGE    scan every commit message in a git range, one file per
#                                       commit, so a finding names the commit (e.g. origin/main..HEAD)
#   sanitize-text.sh --pr N [N ...]     scan the title and body of pull requests N... (needs gh)
#
# Exit: 0 clean, 1 findings (the gate prints each with its line), 2 usage or nothing to scan —
# an empty scan is an error, never a pass.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="$HERE/sanitize-gate.sh"
T="$(mktemp -d)"
trap 'rm -rf "${T:?}"' EXIT

case "${1:-}" in
  --commits)
    [ -n "${2:-}" ] || { echo "usage: sanitize-text.sh --commits RANGE" >&2; exit 2; }
    for sha in $(git rev-list "$2"); do
      git log -1 --format=%B "$sha" > "$T/commit-${sha:0:12}.txt"
    done
    ;;
  --pr)
    shift
    [ "$#" -ge 1 ] || { echo "usage: sanitize-text.sh --pr N [N ...]" >&2; exit 2; }
    for n in "$@"; do
      gh pr view "$n" --json title,body --jq '.title + "\n\n" + .body' > "$T/pr-$n.md"
    done
    ;;
  -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  --*)       echo "sanitize-text: unknown option $1" >&2; exit 2 ;;
  "")        cat > "$T/text.txt" ;;
  *)         cp -- "$1" "$T/text.txt" ;;
esac

[ -n "$(ls -A "$T")" ] || { echo "sanitize-text: nothing to scan" >&2; exit 2; }
[ ! -e "$T/text.txt" ] || [ -s "$T/text.txt" ] || { echo "sanitize-text: empty text — nothing to scan" >&2; exit 2; }
rc=0
bash "$GATE" "$T" || rc=$?
exit "$rc"
