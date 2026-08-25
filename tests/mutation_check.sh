#!/usr/bin/env bash
# mutation_check.sh — prove the test suite has TEETH (gate G4, requirements R5/R7).
#
# A green test suite proves nothing on its own: this repo's own sanitize gate was green in
# CI while it passed on an empty file list. So for each load-bearing property we DELETE the
# property in a throwaway copy of the repo and require the suite to go RED. A mutation that
# survives (suite still green) is a defect in the tests, and fails this script.
#
# Usage: tests/mutation_check.sh
# Exit 0 = every mutation was caught. Exit 1 = at least one survived.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0

run_mutation() {   # $1=label  $2=file  $3=python-replace-expr  $4=test-module
  local label="$1" file="$2" expr="$3" mod="$4"
  local tmp; tmp="$(mktemp -d)"
  cp -r "$ROOT"/core "$ROOT"/tests "$tmp"/ 2>/dev/null

  if ! python3 - "$tmp/$file" <<PY
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = $expr
if old not in s:
    sys.exit("MUTATION TARGET NOT FOUND: " + old[:60])
p.write_text(s.replace(old, new, 1))
PY
  then
    echo "  [ERROR] $label — could not apply mutation (target text moved?)"
    FAIL=$((FAIL+1)); rm -rf "$tmp"; return
  fi

  # The suite MUST fail now. Inverted logic on purpose.
  if (cd "$tmp" && python3 -m unittest "tests.$mod" >/dev/null 2>&1); then
    echo "  [SURVIVED] $label — suite still GREEN with the property removed. Test has no teeth."
    FAIL=$((FAIL+1))
  else
    echo "  [caught]   $label"
    PASS=$((PASS+1))
  fi
  rm -rf "$tmp"
}

echo "mutation_check: removing load-bearing properties one at a time"

# R5 — schema pinning. If validation is skipped, a renamed field silently stores nothing.
run_mutation "store: skip schema validation on ingest" \
  "core/store.py" \
  "('        for m in batch:\n            self.validate(m)', '        for m in batch:\n            pass')" \
  "test_store"

# R5 — missing-field detection specifically.
run_mutation "store: stop rejecting missing required fields" \
  "core/store.py" \
  "('        missing = [f for f in REQUIRED_FIELDS if f not in msg or msg[f] is None]', '        missing = []')" \
  "test_store"

# R5 — unknown-field (drift the other way).
run_mutation "store: stop rejecting unknown fields" \
  "core/store.py" \
  "('        unknown = [k for k in msg if k not in MESSAGE_FIELDS]', '        unknown = []')" \
  "test_store"

# R9 — idempotency. INSERT instead of INSERT OR REPLACE duplicates on re-poll.
run_mutation "store: non-idempotent insert" \
  "core/store.py" \
  "('\"INSERT OR REPLACE INTO messages \"', '\"INSERT INTO messages \"')" \
  "test_store"

# R9 — validate-before-write ordering (a bad batch must not half-land).
run_mutation "store: validate per-row during the write instead of up front" \
  "core/store.py" \
  "('        batch = list(messages)\n        for m in batch:\n            self.validate(m)', '        batch = list(messages)')" \
  "test_store"

# R8/R7 — THE vacuous-pass guard. Empty oracle must never report parity.
run_mutation "parity: allow an empty oracle to pass" \
  "core/parity.py" \
  "('    if not oracle_ts:', '    if False:')" \
  "test_parity"

# R8 — miss detection.
run_mutation "parity: stop reporting missed messages" \
  "core/parity.py" \
  "('        missed=oracle_ts - candidate_ts,', '        missed=set(),')" \
  "test_parity"

# R8 — extra detection.
run_mutation "parity: stop reporting extra messages" \
  "core/parity.py" \
  "('        extra=candidate_ts - oracle_ts,', '        extra=set(),')" \
  "test_parity"

# R8 — cursor divergence must fail parity.
run_mutation "parity: ignore cursor divergence in the verdict" \
  "core/parity.py" \
  "('return not self.missed and not self.extra and not self.cursor_divergent', 'return not self.missed and not self.extra')" \
  "test_parity"

# R8 — a schema mismatch must not look like an empty channel.
run_mutation "parity: swallow query errors and return an empty set" \
  "core/parity.py" \
  "('        raise ParityError(f\"query failed on {db_path} ({table}.{ts_col}): {ex}\") from ex', '        rows = []')" \
  "test_parity"

echo
echo "mutation_check: caught=$PASS survived/error=$FAIL"
[ "$FAIL" -eq 0 ] && { echo "PASS — every removed property turned the suite red"; exit 0; }
echo "FAIL — a property can be removed without any test noticing" >&2
exit 1
