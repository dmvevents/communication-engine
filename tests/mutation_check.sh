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

# ---------------------------------------------------------------------------
# G2 — the send path. Every one of these mutations, if it survived, would either
# double-message a customer in Anton's name or silently drop a reply.
# ---------------------------------------------------------------------------

# R2 — dedupe. Without it, a retry re-sends an already-delivered message.
run_mutation "outbox: drop the already-delivered dedupe" \
  "core/outbox.py" \
  "('        if row and row[\"state\"] in (VERIFIED, COMMITTED):', '        if False:')" \
  "test_outbox_faults"

# R1 — recovery must PROVE prior delivery by read-back before re-sending. This is the
# exact 24-auto-reconcile bug: crash after send, then blindly re-send.
run_mutation "outbox: recovery re-sends without proving prior delivery" \
  "core/outbox.py" \
  "('            if self.adapter.read_back(target, key):', '            if False:')" \
  "test_outbox_faults"

# R1 — read-back verification before claiming success.
run_mutation "outbox: skip read-back verification" \
  "core/outbox.py" \
  "('        if not self.adapter.read_back(target, key):', '        if False:')" \
  "test_outbox_faults"

# R10 — default DENY. A target absent from the policy map must not be sendable.
run_mutation "outbox: default policy becomes direct instead of never" \
  "core/outbox.py" \
  "('        return self.policies.get(target, \"never\")', '        return self.policies.get(target, \"direct\")')" \
  "test_outbox_faults"

# R10 — a staged target must never reach the adapter (the operator gate).
run_mutation "outbox: staged target falls through to the adapter" \
  "core/outbox.py" \
  "('            return {\"key\": key, \"receipt\": None, \"state\": STAGED, \"staged\": True}', '            pass')" \
  "test_outbox_faults"

# ---------------------------------------------------------------------------
# G4 — checks that cannot silently no-op.
# ---------------------------------------------------------------------------

# R7 — a PASS with zero evidence is the vacuous pass.
run_mutation "checks: allow PASS having inspected nothing" \
  "core/checks.py" \
  "('        if inspected <= 0:', '        if False:')" \
  "test_checks"

# R5 — a check returning None must become a FAIL, not silence.
run_mutation "checks: stop converting a None verdict into a failure" \
  "core/checks.py" \
  "('            if v is None:', '            if False:')" \
  "test_checks"

# R5 — a check returning junk must become a FAIL.
run_mutation "checks: stop rejecting non-Verdict returns" \
  "core/checks.py" \
  "('            if not isinstance(v, Verdict):', '            if False:')" \
  "test_checks"

# R6 — a watcher whose source vanished is unhealthy (the F-1 zombie class).
run_mutation "checks: treat a missing watcher source as healthy" \
  "core/checks.py" \
  "('    if not exists:', '    if False:')" \
  "test_checks"

# The action-only-log trap: silence must read as health for action-only sources.
run_mutation "checks: judge an action-only log by its age" \
  "core/checks.py" \
  "('    if action_only_log:', '    if False:')" \
  "test_checks"

# ---------------------------------------------------------------------------
# G3 — the owed-work edge.
# ---------------------------------------------------------------------------

# R3 — backoff must never suppress owed work (the 8h17m idle).
run_mutation "owed: let backoff suppress unattended owed work" \
  "core/owed.py" \
  "('        if self.unattended():', '        if False:')" \
  "test_owed"

# R4 — a driver STRING is not evidence; liveness is.
run_mutation "owed: trust the driver field without checking liveness" \
  "core/owed.py" \
  "('            if not row[\"driver\"] or not self.driver_alive(row[\"driver\"]):', '            if not row[\"driver\"]:')" \
  "test_owed"

# ---------------------------------------------------------------------------
# G9 — classification. A false EXEC-REQUEST can make the fleet start a cluster run
# because a colleague used the word "testing" in passing.
# ---------------------------------------------------------------------------

# R15 — word boundaries. This is the mutation that reintroduces the measured 34% rate.
run_mutation "classify: match keywords as substrings again" \
  "core/classify.py" \
  "('        pattern = r\"\\\\b\" + r\"\\\\s+\".join(re.escape(w) for w in n.split()) + r\"\\\\b\"', '        pattern = re.escape(n)')" \
  "test_classify"

# R15 — an exec verb alone must not be an instruction.
run_mutation "classify: treat a bare exec verb as an order" \
  "core/classify.py" \
  "('    if exec_hits and (directive or _starts_imperative(t, tax.exec_verbs)):', '    if exec_hits:')" \
  "test_classify"

# R15 — a commitment ask must always reach a human, even alongside a work request.
run_mutation "classify: let exec outrank a commitment ask" \
  "core/classify.py" \
  "('    if commit_hits:', '    if False:')" \
  "test_classify"

# ---------------------------------------------------------------------------
# G10 — audit integrity. The incumbent log inflates by 45%.
# ---------------------------------------------------------------------------

# R16 — one row per distinct message, however many times it is seen.
run_mutation "journal: append a new row on every sighting" \
  "core/journal.py" \
  "('        if existing is None:', '        if True:')" \
  "test_journal"

# R16 — a bare re-sighting must not erase a recorded classification.
run_mutation "journal: let a later null overwrite the classification" \
  "core/journal.py" \
  "('            \"kind=COALESCE(?, kind), reason=COALESCE(?, reason), routed=COALESCE(?, routed) \"', '            \"kind=?, reason=?, routed=? \"')" \
  "test_journal"

echo
echo "mutation_check: caught=$PASS survived/error=$FAIL"
[ "$FAIL" -eq 0 ] && { echo "PASS — every removed property turned the suite red"; exit 0; }
echo "FAIL — a property can be removed without any test noticing" >&2
exit 1
