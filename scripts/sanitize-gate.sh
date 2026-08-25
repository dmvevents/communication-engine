#!/usr/bin/env bash
# sanitize-gate.sh — secret scan that must PASS before any commit/push leaves this repo.
#
# Design rules:
#   1. The repo NEVER contains a concrete secret — not even as a scan pattern.
#      Generic pattern CLASSES live here; host-specific literals (chat ids,
#      account ids, real channel ids) live in a LOCAL denylist file that is
#      gitignored and never committed.
#   2. The gate scans every tracked file, including itself. Patterns are
#      written so their own source text cannot match them.
#   3. `--self-test` proves the gate works by planting synthetic secrets
#      (built by string concatenation so this file never contains them) and
#      requiring every class to be caught, plus a clean file to pass.
#
# Usage:
#   scripts/sanitize-gate.sh              # scan tracked files (or CWD if not a repo)
#   scripts/sanitize-gate.sh --self-test  # prove the gate catches every class
#   scripts/sanitize-gate.sh <dir>        # scan an arbitrary directory
#
# Local denylist (host-specific literals, one extended-regex per line):
#   $SANITIZE_DENYLIST_FILE, or .sanitize-denylist.local (gitignored), or
#   ~/.config/communication-engine/denylist.local
#
# Exit: 0 clean, 1 findings, 2 usage/internal error.
set -uo pipefail

# ---- pattern classes (ERE). Each is (name, regex). ------------------------
# NOTE: numeric-only identifiers (Telegram chat ids, phone numbers) are too
# false-positive-prone to scan generically (epoch timestamps are 10 digits).
# They MUST be covered by the local denylist on any host that has them.
CLASSES=(
  "slack-token|xox[abprs]-[0-9A-Za-z-]{8,}"
  "slack-app-token|xapp-[0-9]-[0-9A-Za-z-]{8,}"
  "slack-id|\b[CDGUWT][0-9][0-9A-Z]{8,10}\b"
  "telegram-bot-token|\b[0-9]{8,10}:AA[0-9A-Za-z_-]{30,}\b"
  "aws-access-key|\b(AKIA|ASIA)[0-9A-Z]{16}\b"
  "aws-account-id|\b[0-9]{12}\b"
  "private-key|BEGIN [A-Z ]*PRIVATE KEY"
  "assigned-secret|(SECRET|TOKEN|PASSWD|PASSWORD|API_KEY)=[\"']?[A-Za-z0-9+/=_-]{16,}"
  "json-secret|\"(token|bot_token|secret|password|api_key|hmac_secret)\"[[:space:]]*:[[:space:]]*\"[A-Za-z0-9+/=_-]{16,}\""
  "ipv4|\b([0-9]{1,3}\.){3}[0-9]{1,3}\b"
)
# ipv4 allowlist: loopback + all-interfaces are architecture, not secrets.
IPV4_ALLOW='127\.0\.0\.1|0\.0\.0\.0'

list_files() {
  local root="$1"
  # An explicit directory argument means "scan exactly this tree" — walk the
  # filesystem. Consulting git there would honor a PARENT repo's ignore rules
  # and silently skip the very files the caller asked about.
  if [ "${EXPLICIT_TARGET:-0}" = "1" ]; then
    find "$root" -type f -not -path '*/.git/*' 2>/dev/null
  elif git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # tracked AND untracked-but-not-ignored: a secret in a not-yet-added file
    # must be caught before it can ever be staged
    git -C "$root" ls-files -z --cached --others --exclude-standard \
      | while IFS= read -r -d '' f; do printf '%s\n' "$root/$f"; done
  else
    find "$root" -type f -not -path '*/.git/*' 2>/dev/null
  fi
}

scan() {
  local root="$1" findings=0 name regex hits
  local files; files="$(list_files "$root")"
  # An empty scan is an ERROR, never a pass — a gate that silently checks
  # nothing is the exact defect class this repo exists to prevent.
  [ -n "$files" ] || { echo "sanitize-gate: ERROR — nothing to scan under $root" >&2; return 2; }

  for entry in "${CLASSES[@]}"; do
    name="${entry%%|*}"; regex="${entry#*|}"
    hits="$(printf '%s\n' "$files" | xargs -d '\n' grep -HnE "$regex" -- 2>/dev/null || true)"
    if [ "$name" = "ipv4" ] && [ -n "$hits" ]; then
      hits="$(printf '%s\n' "$hits" | grep -vE "$IPV4_ALLOW" || true)"
    fi
    if [ -n "$hits" ]; then
      findings=1
      printf '%s\n' "$hits" | sed "s|^|[${name}] |"
    fi
  done

  # host-local denylist (concrete literals live OUTSIDE the repo)
  local dl=""
  if [ -n "${SANITIZE_DENYLIST_FILE:-}" ] && [ -f "${SANITIZE_DENYLIST_FILE}" ]; then
    dl="$SANITIZE_DENYLIST_FILE"
  elif [ -f "$root/.sanitize-denylist.local" ]; then
    dl="$root/.sanitize-denylist.local"
  elif [ -f "$HOME/.config/communication-engine/denylist.local" ]; then
    dl="$HOME/.config/communication-engine/denylist.local"
  fi
  if [ -n "$dl" ]; then
    hits="$(printf '%s\n' "$files" | xargs -d '\n' grep -HnEf "$dl" -- 2>/dev/null || true)"
    if [ -n "$hits" ]; then
      findings=1
      printf '%s\n' "$hits" | sed 's|^|[local-denylist] |'
    fi
  else
    echo "sanitize-gate: note — no local denylist found (host-specific literals unchecked)" >&2
  fi

  return "$findings"
}

self_test() {
  local tmp; tmp="$(mktemp -d)" || return 2
  trap 'rm -rf "$tmp"' RETURN

  # Planted secrets are CONCATENATED so this script's own source never
  # contains a matchable literal.
  local a b c
  a="xox"; b="b-0123456789"; c="abcdefXYZ"
  printf 'slack_token = %s\n' "${a}${b}${c}" > "$tmp/p1.txt"
  a="xapp"; b="-1-A0123456789-abcdef"
  printf '%s\n' "${a}${b}" > "$tmp/p2.txt"
  a="C0"; b="FAKEFAKE12"
  printf 'channel: %s\n' "${a}${b}" > "$tmp/p3.txt"
  a="98765432"; b=":AA"; c="abcdefghijklmnopqrstuvwxyz012345"
  printf 'tg = %s\n' "${a}${b}${c}" > "$tmp/p4.txt"
  a="AKIA"; b="ABCDEFGHIJKLMNOP"
  printf 'key=%s\n' "${a}${b}" > "$tmp/p5.txt"
  a="123456"; b="789012"
  printf 'account %s\n' "${a}${b}" > "$tmp/p6.txt"
  a="-----BEGIN RSA "; b="PRIVATE KEY-----"
  printf '%s%s\n' "$a" "$b" > "$tmp/p7.txt"
  a="API_KEY="; b="abcdefghijklmnop1234"
  printf '%s%s\n' "$a" "$b" > "$tmp/p8.txt"
  a='"token": "'; b='abcdefghijklmnop1234"'
  printf '{%s%s}\n' "$a" "$b" > "$tmp/p9.txt"
  a="203.0."; b="113.7"
  printf 'host %s%s\n' "$a" "$b" > "$tmp/p10.txt"

  local out planted_classes=(slack-token slack-app-token slack-id telegram-bot-token \
                             aws-access-key aws-account-id private-key assigned-secret \
                             json-secret ipv4)
  out="$(scan "$tmp" 2>/dev/null)"
  local rc=$? missing=0
  if [ "$rc" -eq 0 ]; then
    echo "SELF-TEST FAIL: scan returned clean on planted secrets" >&2
    return 1
  fi
  for cl in "${planted_classes[@]}"; do
    if ! printf '%s\n' "$out" | grep -q "^\[${cl}\]"; then
      echo "SELF-TEST FAIL: class '${cl}' not caught" >&2
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || return 1

  # clean corpus must pass
  local clean; clean="$(mktemp -d)" || return 2
  printf 'channel: C_EXAMPLE_CHANNEL\nauth: env:SLACK_ENV_REF\nbind: 127.0.0.1\n' > "$clean/ok.txt"
  if ! scan "$clean" >/dev/null 2>&1; then
    echo "SELF-TEST FAIL: clean corpus was flagged" >&2
    rm -rf "$clean"
    return 1
  fi
  rm -rf "$clean"
  echo "SELF-TEST PASS: all ${#planted_classes[@]} classes caught, clean corpus passed"
  return 0
}

case "${1:-}" in
  --self-test) self_test; exit $? ;;
  -h|--help)   sed -n '2,22p' "$0"; exit 0 ;;
  "")          ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)" ;;
  *)           ROOT="$1"; EXPLICIT_TARGET=1; export EXPLICIT_TARGET
               [ -d "$ROOT" ] || { echo "sanitize-gate: no such dir: $ROOT" >&2; exit 2; } ;;
esac

scan "$ROOT"
case $? in
  0) echo "sanitize-gate: PASS (no findings under $ROOT)"; exit 0 ;;
  1) echo "sanitize-gate: FAIL — findings above must be removed or placeholder-ized before push" >&2; exit 1 ;;
  *) echo "sanitize-gate: ERROR — scan could not run (see above)" >&2; exit 2 ;;
esac
