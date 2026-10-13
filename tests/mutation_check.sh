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
  # channels/ ships code too (the fake adapter is contract-tested), and docs/ + scripts/
  # are tested like code (R21: the docs are shipped interface), so mutate against them all.
  # The root files matter too: with settings.example.json absent, test_portability was RED
  # in this sandbox before any mutation applied, so every mutation targeting it was
  # vacuously "caught" (found while landing ENH-19, which needs README.md here for the
  # same reason). A sandbox that cannot go green cannot prove a mutation turned it red.
  cp -r "$ROOT"/core "$ROOT"/tests "$ROOT"/channels "$ROOT"/docs "$ROOT"/scripts "$tmp"/ 2>/dev/null
  cp "$ROOT"/README.md "$ROOT"/settings.example.json "$tmp"/ 2>/dev/null

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
  "('        return not self.unexplained and not self.cursor_divergent', '        return not self.unexplained')" \
  "test_parity"

# R8 — the verdict IS the unexplained set. Ignoring it passes every divergence.
run_mutation "parity: verdict ignores unexplained divergence classes" \
  "core/parity.py" \
  "('        return not self.unexplained and not self.cursor_divergent', '        return not self.cursor_divergent')" \
  "test_parity"

# R8/R25 — THE safety property of classification. If a caller can accept ENGINE_LOST,
# the entire taxonomy becomes a way to configure a real message loss into a green run.
run_mutation "parity: ENGINE_LOST becomes waivable by configuration" \
  "core/parity.py" \
  "('    forbidden = accepted & NEVER_ACCEPTABLE', '    forbidden = frozenset()')" \
  "test_parity"

# R25 — fail-closed when the platform is silent. Without a snapshot, a message we lost
# and a message someone deleted are indistinguishable, and the safe reading is 'ours'.
run_mutation "parity: fail OPEN when no platform snapshot is supplied" \
  "core/parity.py" \
  "('        elif not served_known or ts in served:', '        elif served_known and ts in served:')" \
  "test_parity"

# R25 — an empty snapshot would relabel every genuine loss as an upstream deletion:
# the vacuous-pass bug wearing a new hat.
run_mutation "parity: an empty platform snapshot excuses every loss" \
  "core/parity.py" \
  "('    if served_known and not served:', '    if False:')" \
  "test_parity"

# R25 — the coverage window must come from the CURSOR, not from the engine's own newest
# row: inferring it from the candidate lets a lost newest message define itself out of
# the window and be waved through as NOT_YET_POLLED.
run_mutation "parity: coverage window inferred from the newest stored row" \
  "core/parity.py" \
  "('    through = _fl(covered_through) if covered_through is not None else None', '    through = max(map(_fl, candidate_ts)) if candidate_ts else None')" \
  "test_parity"

# R25 — a message the PLATFORM serves and the engine lacks is lost whether or not the
# incumbent happened to catch it. Narrowing the universe to the oracle hides that.
run_mutation "parity: platform rows the oracle never had are not checked for loss" \
  "core/parity.py" \
  "('    for ts in (served | oracle_ts) - candidate_ts:', '    for ts in oracle_ts - candidate_ts:')" \
  "test_parity"

# ENH-14 — sole-witness rows are where a foreign-channel ingest bug lands. Filing them
# as 'the incumbent missed it' blames the oracle for our own bug.
run_mutation "parity: sole-witness extras blamed on the oracle" \
  "core/parity.py" \
  "('            classified[ts] = ENGINE_ONLY', '            classified[ts] = ORACLE_MISSED')" \
  "test_parity"

# R25 — an unorderable ts must not float to 0.0, which would place it below every
# retention floor and earn a benign class for free.
run_mutation "parity: unparseable timestamp silently sorts as zero" \
  "core/parity.py" \
  "('    except (TypeError, ValueError) as ex:', '    except (TypeError, ValueError) as ex:\n        return 0.0\n    except KeyboardInterrupt as ex:')" \
  "test_parity"

# R25 — a broken snapshot file must not degrade to 'no snapshot': that silently flips the
# run to fail-closed and hides that the snapshot step itself is broken.
run_mutation "parity: a malformed platform snapshot degrades to no snapshot" \
  "core/parity.py" \
  "('        raise ParityError(f\"platform snapshot {path} is not valid JSON: {ex}\") from ex', '        return set()')" \
  "test_parity"

# ENH-27 — an adapter that CANNOT supply a snapshot must be declared, or its permanently
# fail-closed parity runs read as read-path defects (the R8 misreading, re-armed).
run_mutation "parity: an adapter without retrievable_ts declares nothing" \
  "core/parity.py" \
  "('    if callable(getattr(adapter, \"retrievable_ts\", None)):', '    if True:')" \
  "test_parity"

# ENH-27 — the declaration must name WHICH adapter owns the gap; anonymous, it cannot
# route the operator to the capable sibling (or to the fact none exists, on Telegram).
run_mutation "parity: the declaration stops naming the adapter" \
  "core/parity.py" \
  "('adapter {adapter_name!r} cannot supply', 'this adapter cannot supply')" \
  "test_parity"

# ENH-27 — and WHICH capability is missing: 'cannot supply a snapshot' alone tells an
# adapter author nothing about what to implement.
run_mutation "parity: the declaration stops naming the missing capability" \
  "core/parity.py" \
  "('retrievable_ts (a push surface', 'a history call (a push surface')" \
  "test_parity"

# ENH-27 — a declaration the summary never prints declared nothing to anybody.
run_mutation "parity: the summary swallows the capability declaration" \
  "core/parity.py" \
  "('            if self.snapshot_unavailable:', '            if False:')" \
  "test_parity"

# R26 — the differ ACCEPTS the UNRETRIEVABLE class, so its growth is invisible there.
# These are the guards that keep the blindness from moving into retention.py.
run_mutation "retention: an empty previous snapshot reports 'nothing deleted'" \
  "core/retention.py" \
  "('    if not previous_served:', '    if False:')" \
  "test_retention"

run_mutation "retention: an empty current snapshot reported as a mass deletion" \
  "core/retention.py" \
  "('    if not current_served:', '    if False:')" \
  "test_retention"

run_mutation "retention: deletions counted for rows we never stored" \
  "core/retention.py" \
  "('    return (set(stored_ts) & previous_served) - current_served', '    return previous_served - current_served')" \
  "test_retention"

# R26 — the instant a row vanished is a fact about the past; a re-run must not move it.
run_mutation "retention: tombstone instant rewritten on every re-run" \
  "core/retention.py" \
  "('                \"INSERT OR IGNORE INTO tombstones (channel_id, ts, detected_at) \"', '                \"INSERT OR REPLACE INTO tombstones (channel_id, ts, detected_at) \"')" \
  "test_retention"

run_mutation "checks: a deletion burst over budget passes anyway" \
  "core/checks.py" \
  "('    if newly_unretrievable > budget:', '    if False:')" \
  "test_retention"

run_mutation "checks: an undefined deletion budget treated as satisfied" \
  "core/checks.py" \
  "('    if budget is None:\n        return Verdict.failed(\n            name, f\"no deletion budget defined for {name}', '    if False:\n        return Verdict.failed(\n            name, f\"no deletion budget defined for {name}')" \
  "test_retention"

run_mutation "retention: a malformed snapshot degrades to an empty one" \
  "core/retention.py" \
  "('        raise RetentionError(f\"snapshot {path} is not valid JSON: {ex}\") from ex', '        return set()')" \
  "test_retention"

# R8 — a schema mismatch must not look like an empty channel.
run_mutation "parity: swallow query errors and return an empty set" \
  "core/parity.py" \
  "('        raise ParityError(f\"query failed on {db_path} ({table}.{ts_col}): {ex}\") from ex', '        rows = []')" \
  "test_parity"

# ---------------------------------------------------------------------------
# G2 — the send path. Every one of these mutations, if it survived, would either
# double-message a customer in Anton's name or silently drop a reply.
# ---------------------------------------------------------------------------

# R2 — dedupe. Without it, a retry is never answered with the delivered receipt.
run_mutation "outbox: drop the already-delivered dedupe" \
  "core/outbox.py" \
  "('            if row[\"state\"] in (VERIFIED, COMMITTED):', '            if False:')" \
  "test_outbox_faults"

# R1 — recovery must PROVE prior delivery by read-back before re-sending. This is the
# exact 24-auto-reconcile bug: crash after send, then blindly re-send.
run_mutation "outbox: recovery re-sends without proving prior delivery" \
  "core/outbox.py" \
  "('                if self._read_back(target, key, row[\"thread_id\"], row[\"text\"],\\n                                   since=row[\"created_at\"]):', '                if False:')" \
  "test_outbox_faults"

# R1 — read-back verification before claiming success.
run_mutation "outbox: skip read-back verification" \
  "core/outbox.py" \
  "('        if not proven:', '        if False:')" \
  "test_outbox_faults"

# R10 — default DENY. A target absent from the policy map must not be sendable.
run_mutation "outbox: default policy becomes direct instead of never" \
  "core/outbox.py" \
  "('        policy = self.policies.get(target, \"never\")', '        policy = self.policies.get(target, \"direct\")')" \
  "test_outbox_faults"

# R10 — a staged target must never reach the adapter (the operator gate).
run_mutation "outbox: staged target falls through to the adapter" \
  "core/outbox.py" \
  "('            return {\"key\": key, \"receipt\": None, \"state\": STAGED, \"staged\": True}', '            pass')" \
  "test_outbox_faults"

# R2 — the INTENT insert IS the claim between concurrent senders. A plain INSERT turns
# the SELECT→INSERT race into an IntegrityError crash instead of a clean dedupe.
run_mutation "outbox: INTENT insert stops arbitrating concurrent claims" \
  "core/outbox.py" \
  "('INSERT OR IGNORE INTO outbox', 'INSERT INTO outbox')" \
  "test_outbox_faults"

# R2 — a row another sender holds in flight must never fall through to the adapter.
# This is the 3-deliveries-from-6-senders race the loop caught live (fire=13).
run_mutation "outbox: an in-flight row falls through to a second live send" \
  "core/outbox.py" \
  "('            return {\"key\": key, \"receipt\": None, \"state\": row[\"state\"],\n                    \"in_flight\": True}', '            pass')" \
  "test_outbox_faults"

# ---------------------------------------------------------------------------
# ENH-11 — the state-machine property sweep. The first mutation is the proof the
# sweep earns its keep: recovery marking VERIFIED before it re-sends LOSES the
# message when recovery itself dies right after that commit — and it passes
# test_outbox_faults in full, because no hand-enumerated seam ever crashes
# recovery. Only the nested crash sweep reaches it.
# ---------------------------------------------------------------------------

# R1 — a recovery crash between a premature VERIFIED and the re-send silently drops
# the reply (VERIFIED rows are never resumed).
run_mutation "outbox: recovery marks VERIFIED before it re-sends (loss under a recovery crash)" \
  "core/outbox.py" \
  "('                else:\\n                    # recovery is a burst source too', '                else:\\n                    self._write(key, state=VERIFIED)\\n                    # recovery is a burst source too')" \
  "test_outbox_statemachine"

# R1 — the duplicate direction, under the sweep instead of the named seams.
run_mutation "outbox: recovery re-sends blind — the property sweep sees the duplicate" \
  "core/outbox.py" \
  "('                if self._read_back(target, key, row[\"thread_id\"], row[\"text\"],\\n                                   since=row[\"created_at\"]):', '                if False:')" \
  "test_outbox_statemachine"

# R2 — liveness half of exactly-once: a retry that is never told 'delivered' retries
# forever; the sweep asserts the dedupe receipt on every terminal check.
run_mutation "outbox: delivered rows stop reporting dedupe to a retry" \
  "core/outbox.py" \
  "('            if row[\"state\"] in (VERIFIED, COMMITTED):', '            if False:')" \
  "test_outbox_statemachine"

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
  "('            \"kind=COALESCE(?, kind), reason=COALESCE(?, reason), routed=COALESCE(?, routed), \"', '            \"kind=?, reason=?, routed=?, \"')" \
  "test_journal"

# ---------------------------------------------------------------------------
# R22 — the audit link. A classification that cannot be traced from its journal row back
# to the cues that produced it can be neither audited nor disputed; the taxonomy may have
# changed by the time anyone asks.
# ---------------------------------------------------------------------------

# R22 — the decision must NAME its cues (kind and behaviour unchanged; only evidence lost).
run_mutation "classify: an exec decision stops naming the cues behind it" \
  "core/classify.py" \
  "('                              exec_hits + directive)', '                              [])')" \
  "test_classify"

# R22 — the cues must reach the journal row at all.
run_mutation "journal: the cues never reach the journal row" \
  "core/journal.py" \
  "('        mjson = None if matched is None else json.dumps(list(matched))', '        mjson = None')" \
  "test_journal"

# R22 — a bare re-sighting must not erase the recorded cues (same disease as R16).
run_mutation "journal: let a later null erase the recorded cues" \
  "core/journal.py" \
  "('            \"matched=COALESCE(?, matched) \"', '            \"matched=? \"')" \
  "test_journal"

# R22 — each revision keeps the cues of ITS decision; the dispute over an answered v1
# needs v1's evidence, not the live row's.
run_mutation "journal: a revision loses its own decision's cues" \
  "core/journal.py" \
  "('            self._add_revision(channel, ts, rev, text, kind, reason, mjson, now)', '            self._add_revision(channel, ts, rev, text, kind, reason, None, now)')" \
  "test_journal"

# R22 — a pre-audit journal.db must be migrated, not refused (an audit trail destroyed to
# improve auditability) and not silently written past.
run_mutation "journal: skip the migration for pre-audit databases" \
  "core/journal.py" \
  "('            if \\\"matched\\\" not in cols:', '            if False:')" \
  "test_journal"

# R22 — the caller boundary where the cues actually died: first-poll journaled kind and
# reason but dropped matched, so every adopter's audit trail started broken.
run_mutation "first-poll: the classification's cues are dropped at the journal call" \
  "scripts/first-poll.py" \
  "('                          matched=c.matched, ambiguous=c.ambiguous)', '                          ambiguous=c.ambiguous)')" \
  "test_docs"

# ---------------------------------------------------------------------------
# G8 — adoptability. Each of these, if it survived, would let a newcomer either
# post as someone by accident or start half-configured.
# ---------------------------------------------------------------------------

# R19 — deny by default. A channel with no policy must be read-only.
run_mutation "config: default a channel with no policy to direct" \
  "core/config.py" \
  "('                reply_policy=ch.get(\"reply_policy\", \"never\"),   # DEFAULT DENY', '                reply_policy=ch.get(\"reply_policy\", \"direct\"),')" \
  "test_portability"

# R17 — a missing env var must fail at LOAD, not at first send.
run_mutation "config: tolerate a missing environment variable" \
  "core/config.py" \
  "('    if name not in env:', '    if False:')" \
  "test_portability"

# R17 — a literal credential in config must be refused.
run_mutation "config: accept a literal credential" \
  "core/config.py" \
  "('        if shape.match(value):', '        if False:')" \
  "test_portability"

# R17 — an unknown adapter must fail loudly, not leave an inert instance.
run_mutation "config: silently accept an unknown adapter" \
  "core/config.py" \
  "('        if adapter not in discovered:', '        if False:')" \
  "test_portability"

# ---------------------------------------------------------------------------
# G5 — extensibility. R11's measured evidence: the incumbent's second channel type was a
# hand-mirrored COPY of its first. Each of these, if it survived, would mean a new channel
# type once again requires editing core/.
# ---------------------------------------------------------------------------

# R11 — THE defect reintroduced: a hardcoded whitelist in core refuses any type it has not
# heard of. Note the tuple includes 'fake', so only the invented-name landing test has
# teeth against this — the shipped-adapter tests alone would stay green.
run_mutation "config: hardcode an adapter whitelist back into core" \
  "core/config.py" \
  "('        if adapter not in discovered:', '        if adapter not in (\"slack\", \"telegram\", \"email\", \"fake\"):')" \
  "test_extensibility"

# R11 — discovery must demand the adapter.py entry point; a docs-only directory offered as
# a channel type is the silently-inert-instance class again.
run_mutation "config: discover any directory as a channel type" \
  "core/config.py" \
  "('            entry = d / \"adapter.py\"\n            if entry.is_file():', '            entry = d / \"adapter.py\"\n            if d.is_dir():')" \
  "test_extensibility"

# R11 — an adapter module without the pinned entry class must fail at load, not return
# None and explode at first send.
run_mutation "config: return None for an adapter with no entry class" \
  "core/config.py" \
  "('    if cls is None:', '    if False:')" \
  "test_extensibility"

# R18 — relative paths must resolve against the config dir so one file works anywhere.
run_mutation "config: ignore the base dir when resolving paths" \
  "core/config.py" \
  "('    return p if p.is_absolute() else (base / p)', '    return p')" \
  "test_portability"

# R23 — an EDIT must be detected by content, not swallowed as a duplicate.
run_mutation "journal: treat an edited message as a plain re-sighting" \
  "core/journal.py" \
  "('        edited = bool(text) and existing[\"text_hash\"] not in (None, h)', '        edited = False')" \
  "test_journal"

# R23 — a bare re-sighting must not be mistaken for an edit-to-empty.
run_mutation "journal: treat a bodyless re-sighting as an edit" \
  "core/journal.py" \
  "('        edited = bool(text) and existing[\"text_hash\"] not in (None, h)', '        edited = existing[\"text_hash\"] != h')" \
  "test_journal"

# ---------------------------------------------------------------------------
# G11 — edge-triggered escalation. Each of these, if it survived, is a way for the
# once-a-minute probe to become 1,440 identical alerts/day (or to lose an alert outright).
# ---------------------------------------------------------------------------

# R20 — THE requirement: a repeated level must not re-notify.
run_mutation "escalate: notify on every level, not just edges" \
  "core/escalate.py" \
  "('        if row is not None and row[\"state\"] == state:', '        if False:')" \
  "test_escalate"

# R20 — the cron shape: every poll is a fresh process, so in-memory state dedupes nothing.
run_mutation "escalate: keep edge state in memory instead of on disk" \
  "core/escalate.py" \
  "('        self.conn = sqlite3.connect(str(db_path))', '        self.conn = sqlite3.connect(\":memory:\")')" \
  "test_escalate"

# R20 — commit-before-notify: a notify that crashes would mark the edge reported and the
# alert is lost forever (the inverse of the outbox dual-write, same disease).
run_mutation "escalate: commit the edge before the notification is delivered" \
  "core/escalate.py" \
  "('        self.notify(msg)', '        self._commit(name, state, detail, now)\n        self.notify(msg)')" \
  "test_escalate"

# R20 — the recovery edge is half the acceptance: silence on recovery leaves the operator
# investigating an outage that already ended.
run_mutation "escalate: record recovery silently instead of announcing it" \
  "core/escalate.py" \
  "('        if row is None and ok:', '        if ok:')" \
  "test_escalate"

# ---------------------------------------------------------------------------
# ENH-1 — rate-limit back-off keyed (instance, method), honouring Retry-After.
# Slack scopes a 429 to one method on one workspace; each of these mutations, if it
# survived, would either silence sends because reads were too fast, invent limits the
# platform never stated, or turn a 429 into a dropped customer reply.
# ---------------------------------------------------------------------------

# ENH-1 — a read 429 must not pause sends: key collapsed to the instance alone.
run_mutation "ratelimit: back-off keyed per instance only (a read 429 pauses sends)" \
  "core/ratelimit.py" \
  "('        return (instance, method)', '        return (instance,)')" \
  "test_ratelimit"

# ENH-1 — one workspace's 429 must not pause another's: key collapsed to the method.
run_mutation "ratelimit: back-off keyed per method only (crosses instances)" \
  "core/ratelimit.py" \
  "('        return (instance, method)', '        return (method,)')" \
  "test_ratelimit"

# ENH-1 — Retry-After is the platform's number EXACTLY, never a padded guess.
run_mutation "ratelimit: Retry-After padded instead of honoured exactly" \
  "core/ratelimit.py" \
  "('            self._clock() + _seconds(retry_after))', '            self._clock() + _seconds(retry_after) + 1.0)')" \
  "test_ratelimit"

# ENH-1 — honouring means actually waiting: a call that skips the hold hammers the
# platform and escalates the very 429 it just received.
run_mutation "ratelimit: guarded call skips the wait and hammers the platform" \
  "core/ratelimit.py" \
  "('                self._sleep(delay)', '                pass')" \
  "test_ratelimit"

# ENH-1 — THE drop: exhausted retries must SURFACE the 429, never return None. A None
# here is a customer reply that silently ceased to exist.
run_mutation "ratelimit: exhausted retries return None instead of surfacing the 429" \
  "core/ratelimit.py" \
  "('        raise last', '        return None')" \
  "test_ratelimit"

# ENH-1 — junk Retry-After must fail at the boundary; a NaN admitted into the clock
# arithmetic compares false against everything and the hold never engages.
run_mutation "ratelimit: junk Retry-After admitted into the clock arithmetic" \
  "core/ratelimit.py" \
  "('    if not math.isfinite(s) or s < 0:', '    if False:')" \
  "test_ratelimit"

# R21 — the acceptance is that the limits section NAMES what the engine does not do.
# Deleting it must go red, or "honest limits" is a heading, not a property.
run_mutation "docs: honest-limits section deleted from the quickstart" \
  "docs/QUICKSTART.md" \
  "('## Honest limits', '## Notes')" \
  "test_docs"

# R21 — the quickstart's own stated target is a first successful poll; a quickstart that
# no longer walks the adopter to one has lost its purpose without losing its title.
run_mutation "docs: quickstart first-poll step deleted" \
  "docs/QUICKSTART.md" \
  "('python3 scripts/first-poll.py --config settings.json --seed-demo', 'python3 -m unittest discover -s tests -q')" \
  "test_docs"

# R21 — the taxonomy-placement sentence exists because a real adopter put "taxonomy" at
# the top level and the loader silently ignored it. Reintroducing the vagueness must go red.
run_mutation "docs: taxonomy placement guidance made vague again" \
  "docs/QUICKSTART.md" \
  "('**per-instance**', 'flexible')" \
  "test_docs"

# R21 — a runbook that cites a method that no longer exists teaches an adopter a lie at
# 2am. The citation extractor must catch API drift.
run_mutation "docs: runbook cites a recovery API that does not exist" \
  "docs/RUNBOOK.md" \
  "('outbox.recover()', 'outbox.recover_all()')" \
  "test_docs"

# R21 — the journal row is the proof the first poll happened; a first-poll that prints
# OK without journaling is the incumbent's inert health check wearing a new name.
run_mutation "first-poll: a polled message is no longer journaled" \
  "scripts/first-poll.py" \
  "('    return journal.record(', '    return object() or journal.record(')" \
  "test_docs"

# R17 — the quickstart must reach the adopter's REAL workspace, not stop at the fake
# dry-run: without this step, "adopt by config alone" is a claim the docs never cash.
run_mutation "docs: real-poll step deleted from the quickstart" \
  "docs/QUICKSTART.md" \
  "('## 7. First real poll — your workspace, read-only', '## 7. Notes')" \
  "test_docs"

# R17 — the auth example must show every key the adapter refuses to start without, in
# env-reference form; dropping one leaves the adopter with a load-time refusal the doc
# never prepared them for.
run_mutation "docs: real-poll auth example loses the channels key" \
  "docs/QUICKSTART.md" \
  "('\"token\": \"env:MY_SLACK_TOKEN\", \"channels\": \"env:MY_SLACK_CHANNELS\"', '\"token\": \"env:MY_SLACK_TOKEN\"')" \
  "test_docs"

# R17 — the two-place channel rule is the one SILENT failure on the real-poll path (an
# id in one list but not the other polls nothing and looks successful). Vagueness here
# is how the live bring-up would have read as "engine works, channel is just quiet".
run_mutation "docs: two-place channel rule made vague again" \
  "docs/QUICKSTART.md" \
  "('**two places**', 'the config')" \
  "test_docs"

# ENH-23 — the adopter-measured drift, verbatim: step 6's example vocabulary once had no
# word in common with the shipped demo text, so 'confirm the kind moved' was unfollowable.
run_mutation "docs: step-6 example vocabulary drifts off the demo text again" \
  "docs/QUICKSTART.md" \
  "('\"exec_verbs\": [\"review\", \"provision\", \"deploy\", \"roll back\"]', '\"exec_verbs\": [\"provision\", \"deploy\", \"roll back\"]')" \
  "test_docs"

# ENH-23 — the other half of the same re-run: without fresh state there is no fresh demo
# message ('0 polled' by construction — the RUNBOOK documents that as CORRECT), so a step 6
# that drops the fresh-state instruction contradicts the runbook and observes nothing.
run_mutation "docs: step-6 fresh-state instruction deleted" \
  "docs/QUICKSTART.md" \
  "('Delete the state directory you\nconfigured', 'Start over whenever convenient')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-18 / ENH-31 — the slack adapter's transport default-denies: READ_METHODS plus the
# single WRITE_METHODS entry (chat.postMessage, reachable only through core/outbox at policy
# `staged`, under the aws-do-agent harness grant of 2026-10-13; this host's token was granted
# read-only on 2026-08-26 and its channels' policy stays `never`). Each of these mutations,
# if it survived, would let an unlisted write reach the workspace, silently lose polled
# history, or blind the engine's (instance, method) back-off.
# ---------------------------------------------------------------------------

# THE transport property: the deny-by-default allowlist. With it gone, chat.update,
# chat.delete, reactions — any Web API write — is one _api call away, in whoever's name the
# token carries.
run_mutation "slack: transport allowlist deleted (write methods reach the wire)" \
  "channels/slack/adapter.py" \
  "('        if method not in READ_METHODS and method not in WRITE_METHODS:', '        if False:')" \
  "test_slack_adapter"

# ENH-18 — gap-free polling: stopping at page one silently loses every older message
# in the window, and the cursor then advances PAST them — unrecoverable.
run_mutation "slack: pagination stops after the first page (history silently truncated)" \
  "channels/slack/adapter.py" \
  "('            if not payload.get(\"has_more\"):', '            if True:')" \
  "test_slack_adapter"

# ENH-18 — the wait is the platform's number EXACTLY (ENH-1); a guessed constant
# either hammers the platform or invents a limit it never stated.
run_mutation "slack: Retry-After header discarded (a guess replaces the platform's wait)" \
  "channels/slack/adapter.py" \
  "('            ex = RateLimited(_header(headers, \"Retry-After\", \"1\"), method=method)', '            ex = RateLimited(\"1\", method=method)')" \
  "test_slack_adapter"

# ENH-18 — an unlabelled 429 collapses the engine's keyed back-off to global scope:
# a read 429 would then pause every other method on the workspace.
run_mutation "slack: 429 stripped of its method (keyed back-off collapses to global)" \
  "channels/slack/adapter.py" \
  "('            ex = RateLimited(_header(headers, \"Retry-After\", \"1\"), method=method)', '            ex = RateLimited(_header(headers, \"Retry-After\", \"1\"))')" \
  "test_slack_adapter"

# ENH-18 — contract rule 5: a health check that cannot fail is the incumbent's inert
# check wearing a new name (docs/PROVENANCE.md — the defect this repo exists to kill).
run_mutation "slack: health reports auth_ok on a failed auth.test" \
  "channels/slack/adapter.py" \
  "('            return {\"reachable\": True, \"auth_ok\": False,', '            return {\"reachable\": True, \"auth_ok\": True,')" \
  "test_slack_adapter"

# ---------------------------------------------------------------------------
# ENH-13 — per-channel send pacing at <=1 message/second. chat.postMessage allows
# ~1 msg/sec PER CHANNEL (docs.slack.dev/apis/web-api/rate-limits); each of these
# mutations, if it survived, would let a burst of replies trade the cheap local wait
# for a platform 429 — the seam the whole exactly-once ladder exists to survive.
# ---------------------------------------------------------------------------

# ENH-13 — THE property: the pacer must actually wait out the interval.
run_mutation "outbox: pacer never waits (a burst hits the platform unspaced)" \
  "core/outbox.py" \
  "('            if wait > 0:', '            if False:')" \
  "test_outbox_pacing"

# ENH-13 — the platform scopes the limit per channel; a global hold would let one
# busy channel silence every other one (the disease ENH-1 killed for methods).
run_mutation "outbox: pace state read globally instead of per channel" \
  "core/outbox.py" \
  "('        last = self._pace_last.get(target)', '        last = max(self._pace_last.values(), default=None)')" \
  "test_outbox_pacing"

# ENH-13 — the 1/sec default IS the floor; callers do not know to ask for it.
run_mutation "outbox: default send interval below the platform floor" \
  "core/outbox.py" \
  "('send_interval: float = 1.0', 'send_interval: float = 0.0')" \
  "test_outbox_pacing"

# ENH-13 — the live send path must pace, not just the helper existing.
run_mutation "outbox: the live send path skips the pacer" \
  "core/outbox.py" \
  "('        self._pace(target)\n        receipt = self._deliver(target, text, key, thread_id)', '        receipt = self._deliver(target, text, key, thread_id)')" \
  "test_outbox_pacing"

# ENH-13 — recovery is a burst source too: N undelivered rows for one channel
# re-sent back-to-back would 429 exactly like the live path.
run_mutation "outbox: recovery re-sends a burst unpaced" \
  "core/outbox.py" \
  "('                    self._pace(target)\\n                    # Placement comes from the ROW', '                    # Placement comes from the ROW')" \
  "test_outbox_pacing"

# ENH-13 — attempts must be recorded or the hold never engages; recording at the
# ATTEMPT (not the success) is what makes a retry after a 429 wait out the budget
# the failed attempt already consumed.
run_mutation "outbox: attempts are never recorded (the hold never engages)" \
  "core/outbox.py" \
  "('        self._pace_last[target] = self._clock()', '        pass')" \
  "test_outbox_pacing"

# ---------------------------------------------------------------------------
# ENH-14 — Socket Mode ingestion: push for latency, poll for truth. Each of these
# mutations, if it survived, is a way for the push path to go silently deaf, or for
# the parity watch that legitimizes it to stop telling the truth.
# ---------------------------------------------------------------------------

# ENH-14 — THE acceptance: the platform cycles connections and says so with a
# disconnect frame; ignoring it keeps a dead socket and push goes silently deaf.
run_mutation "socket: disconnect frames ignored (the connection is never re-established)" \
  "channels/slack_socket/adapter.py" \
  "('            if obj.get(\"type\") == \"disconnect\":', '            if False:')" \
  "test_socket_adapter"

# ENH-14 — Socket Mode tickets are single-use: a reconnect that replays the dead
# connection's url is refused by the platform, permanently.
run_mutation "socket: reconnect replays the dead connection's single-use url" \
  "channels/slack_socket/adapter.py" \
  "('        url = self._connections_open(timeout=timeout)', '        url = getattr(self, \"_last_url\", None) or self._connections_open(timeout=timeout)')" \
  "test_socket_adapter"

# ENH-14 — unacked envelopes are re-delivered and then DROPPED by the platform;
# the ack is what keeps push delivery alive at all.
run_mutation "socket: envelopes never acknowledged (the platform re-delivers, then drops)" \
  "channels/slack_socket/adapter.py" \
  "('            self._conn.send_text(json.dumps({\"envelope_id\": envelope_id}))', '            pass')" \
  "test_socket_adapter"

# ENH-14 — a foreign-channel ingest shows up as permanent 'extra' in the parity
# watch: the differ compares exactly the watched channels.
run_mutation "socket: unwatched channels ingested (foreign rows poison parity)" \
  "channels/slack_socket/adapter.py" \
  "('        if channel not in self._watched:', '        if False:')" \
  "test_socket_adapter"

# ENH-14 — message_changed/message_deleted are event-only wrappers history never
# shows as rows; ingesting one is a divergence the poll side can never confirm away.
run_mutation "socket: ephemeral edit wrappers ingested as new rows (permanent divergence)" \
  "channels/slack_socket/adapter.py" \
  "('        if event.get(\"subtype\") in EPHEMERAL_SUBTYPES:', '        if False:')" \
  "test_socket_adapter"

# ENH-14 — RFC 6455 §5.3: a compliant server MUST drop the connection on an unmasked
# client frame — in production the link dies invisibly on the first ack.
run_mutation "socket: client frames sent unmasked (a compliant server drops the link)" \
  "channels/slack_socket/adapter.py" \
  "('        self._sock.sendall(_encode_frame(OP_TEXT, text.encode(), os.urandom(4)))', '        self._sock.sendall(_encode_frame(OP_TEXT, text.encode(), None))')" \
  "test_socket_adapter"

# ENH-14 — THE parity acceptance: a watch that reports a divergence but exits clean
# legitimizes a push path that is losing messages.
run_mutation "parity-watch: a divergence is reported but the run exits clean" \
  "scripts/push-poll-parity.py" \
  "('            if not report.ok:', '            if False:')" \
  "test_push_poll_parity"

# ENH-14 — without the settled-window rule every fresh message is a false alarm,
# and a watch that cries wolf gets muted — which is how a real loss slips through.
run_mutation "parity-watch: unsettled push rows compared before poll catches up (false alarms)" \
  "scripts/push-poll-parity.py" \
  "('        push_ts = {t for t in push_ts if float(t) <= watermark}', '        pass')" \
  "test_push_poll_parity"

# ENH-14/R8 — an empty poll oracle downgraded to a pass is the vacuous-pass defect
# this repo exists to kill, wearing yet another new name.
run_mutation "parity-watch: an empty poll oracle downgraded to a clean pass" \
  "scripts/push-poll-parity.py" \
  "('        return 2', '        return 0')" \
  "test_push_poll_parity"

# ---------------------------------------------------------------------------
# ENH-19 — the front-door docs must not UNDERSTATE reality. Measured at the non-author
# adoption run: core/README.md cited six modules that never existed while omitting the
# ten that do, and README.md gated its quickstart on "once phase 1 lands" — so a
# newcomer concluded there was nothing to adopt. The two docs that drifted were exactly
# the two without citation tests.
# ---------------------------------------------------------------------------

# ENH-19 — a phantom row in the core module table must go red (the six-phantoms defect).
run_mutation "docs: core README grows a row for a module that does not exist" \
  "core/README.md" \
  "('|---|---|', '|---|---|\n| \`engine.py\` | poll scheduler |')" \
  "test_docs"

# ENH-19 — the reverse direction: a shipped module must not vanish from its own README.
run_mutation "docs: core README drops an implemented module from the table" \
  "core/README.md" \
  "('\`parity.py\`', '\`parity\`')" \
  "test_docs"

# ENH-19 — the quickstart must be reachable from the front door; the adoption run showed
# a newcomer never finds docs/QUICKSTART.md unless README.md points at it. The whole
# markdown link is replaced (label + target) so no occurrence survives the mutation.
run_mutation "docs: front-door link to the quickstart deleted" \
  "README.md" \
  "('[\`docs/QUICKSTART.md\`](docs/QUICKSTART.md)', '[\`docs/RUNBOOK.md\`](docs/RUNBOOK.md)')" \
  "test_docs"

# ENH-19 — the exact false framing the adopter tripped over, reintroduced verbatim.
run_mutation "docs: quickstart re-gated on a phase that already shipped" \
  "README.md" \
  "('## Quickstart', '## Quickstart (once phase 1 lands)')" \
  "test_docs"

# ENH-19 — the front door must name every shipped adapter (reality-coupled, like the
# honest-limits claim). Relies on slack_socket being cited exactly once in README.md.
run_mutation "docs: a shipped adapter vanishes from the front door" \
  "README.md" \
  "('| \`slack_socket\` |', '| \`socket\` |')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-2 — the detection-latency SLO (push-vs-poll arrival delta, poll as ground
# truth). The incumbent's "12.1min -> ~1min" headline measured its own cron cadence;
# these properties are what make the replacement metric honest.
# ---------------------------------------------------------------------------

# ENH-2 — no arrival stamps, no latency: ingest silently stops recording and every
# later SLO judgement is over data that does not exist.
run_mutation "slo: ingest stops recording first arrivals" \
  "core/store.py" \
  "('            [(m[\"channel_type\"], m[\"channel_id\"], str(m[\"ts\"]), now) for m in batch])', '            [])')" \
  "test_store"

# ENH-2 — first-write-wins is the whole measurement: a re-poll that moves the stamp
# rewrites history toward zero latency.
run_mutation "slo: a re-ingest moves the first arrival" \
  "core/store.py" \
  "('\"INSERT OR IGNORE INTO arrivals \"', '\"INSERT OR REPLACE INTO arrivals \"')" \
  "test_store"

# ENH-2 — THE acceptance: a poll-confirmed message push never delivered must fail,
# not vanish into the percentiles.
run_mutation "slo: a poll-confirmed miss vanishes from the verdict" \
  "core/slo.py" \
  "('    missed = {ts for ts in truth if ts not in push_msgs}', '    missed = set()')" \
  "test_slo"

# ENH-2 — judging only the median lets a slow tail hide; the tail is where a
# degraded socket shows first.
run_mutation "slo: the p90 budget is no longer judged" \
  "core/slo.py" \
  "('        return (self.detect_p50_s > self.slo_p50_s\n                or self.detect_p90_s > self.slo_p90_s)', '        return self.detect_p50_s > self.slo_p50_s')" \
  "test_slo"

# ENH-2/R8 — the vacuous pass again: a judge that measured nothing must not bless
# the push path.
run_mutation "slo: an unmeasurable comparison passes" \
  "core/slo.py" \
  "('    if not missed and not detect_s:', '    if False:')" \
  "test_slo"

# ENH-2/R8 — the other empty case: no ground truth at all must name the truth side
# as broken, not fall through to a generic (or absent) refusal.
run_mutation "slo: an empty ground truth loses its diagnosis" \
  "core/slo.py" \
  "('    if not truth:', '    if False:')" \
  "test_slo"

# ENH-2 — nearest-rank means rounding the rank UP; a floor drops p90-of-4 from the
# max to the 3rd value, exactly the drift that flatters a slow tail.
run_mutation "slo: percentile rank floored instead of ceiled" \
  "core/slo.py" \
  "('    k = -(-(len(s) * p) // 100)', '    k = (len(s) * p) // 100')" \
  "test_slo"

# ENH-2 — the headline metric itself: push's lead over the truth poll must be the
# real delta, not a flattering constant.
run_mutation "slo: the push-lead delta is fabricated" \
  "core/slo.py" \
  "('        lead_s.append(poll_arr[ts] - push_at)', '        lead_s.append(0.0)')" \
  "test_slo"

# ---------------------------------------------------------------------------
# ENH-20 — every config mistake fails AT LOAD as a ConfigError naming the offending
# KEY. The adoption test drove 17 deliberate mistakes through load: 14 were clean
# ConfigErrors, and these three escaped as raw tracebacks or misdirection.
# ---------------------------------------------------------------------------

# ENH-20 — an unreadable channels_dir (the /etc case) must not escape as a raw
# PermissionError mid-walk: a traceback names an errno, never the key to fix.
run_mutation "config: an unreadable channels_dir escapes as a raw PermissionError" \
  "core/config.py" \
  "('    except OSError as ex:', '    except () as ex:')" \
  "test_portability"

# ENH-20 — a state_dir pointing at an existing FILE must fail at load, not as
# ensure_dirs()'s bare FileExistsError at first write (the module's own stated rule).
run_mutation "config: a state_dir that is a file loads clean and explodes at first write" \
  "core/config.py" \
  "('        if d.exists() and not d.is_dir():', '        if False:')" \
  "test_portability"

# ENH-20 — nothing discovered is a channels_dir fault; 'unknown adapter ... (none)'
# sends the adopter hunting a typo in a name that was never the problem.
run_mutation "config: an empty discovery blames the adapter name again" \
  "core/config.py" \
  "('            if not discovered:', '            if False:')" \
  "test_portability"

# ---------------------------------------------------------------------------
# ENH-21 — QUICKSTART step 2 is `cp settings.example.json settings.json`, and the shipped
# example used to LEAD with an instance whose adapter had no adapter.py, so copy-then-run
# failed 100% of the time (only step-5 prose stood between the adopter and the failure).
# Each of these, if it survived, re-ships an example whose documented copy step produces
# an unloadable config.
# ---------------------------------------------------------------------------

# ENH-21 — THE defect verbatim: the first instance names a contract stub again.
run_mutation "example: first instance is an undiscovered stub adapter again" \
  "settings.example.json" \
  "('\"adapter\": \"fake\"', '\"adapter\": \"telegram\"')" \
  "test_portability"

# ENH-21 — slot 0 is what the step-5 dry run keeps, at a point where no credential
# exists yet; a real adapter there loads clean and then hits the network at first poll.
run_mutation "example: first instance demands credentials for the no-network dry run" \
  "settings.example.json" \
  "('\"adapter\": \"fake\"', '\"adapter\": \"slack\"')" \
  "test_portability"

# ENH-21 — the whole-file property: EVERY instance must name a shipped adapter, or the
# copy step is unloadable however many env vars the adopter exports. (The mutant name
# was "telegram" until ENH-25 shipped it and the mutation went stale-green — the
# instrument, not the property; any name with no channels/<name>/adapter.py works.)
run_mutation "example: a later instance names an unshipped adapter" \
  "settings.example.json" \
  "('\"adapter\": \"slack\"', '\"adapter\": \"outlook\"')" \
  "test_portability"

# ---------------------------------------------------------------------------
# ENH-3 — thread awareness. `thread_id` was declared in the store schema, the normalized
# message and the adapter contract, and read by NOTHING: every reply flattened to the main
# channel. Each mutation below re-flattens one link in the chain — and a flattened reply is
# a PUBLIC post in a channel whose policy may say 'never', so a survivor here is a
# customer-visible defect, not a cosmetic one.
# ---------------------------------------------------------------------------

# ENH-3 — THE acceptance policy: 'answer in thread, never the main channel'. Ignoring the
# scope collapses it back to one policy for both placements.
run_mutation "outbox: policy resolution ignores the placement scope" \
  "core/outbox.py" \
  "('            return policy.get(scope) or \"never\"      # a None value is deny, not direct', '            return policy.get(\"channel\", policy.get(\"thread\", \"never\"))')" \
  "test_outbox_threads"

# ENH-3 — default DENY has to hold PER SCOPE too: a scope absent from a scoped policy
# must be refused, not inherit the permissive sibling.
run_mutation "outbox: an unlisted scope falls back to a permissive sibling" \
  "core/outbox.py" \
  "('            return policy.get(scope) or \"never\"      # a None value is deny, not direct', '            return policy.get(scope) or next(iter(policy.values()), \"never\")')" \
  "test_outbox_threads"

# ENH-3 — the send itself: dropping thread_id on the way to the adapter posts the reply
# top-level while reporting success.
run_mutation "outbox: the thread is dropped on the way to the adapter" \
  "core/outbox.py" \
  "('        receipt = self._deliver(target, text, key, thread_id)', '        receipt = self._deliver(target, text, key, None)')" \
  "test_outbox_threads"

# ENH-3 — an adapter that cannot thread must make the send FAIL, not silently become a
# top-level post in a channel whose policy may be 'never'.
run_mutation "outbox: a threadless adapter gets the reply posted top-level instead" \
  "core/outbox.py" \
  "('        if blocked is not None:', '        if False:')" \
  "test_outbox_threads"

# ENH-3 — the capability question must be asked of the SIGNATURE. Interpreting a TypeError
# instead relabels a bug inside a working adapter as \"this adapter cannot thread\".
run_mutation "outbox: adapter capability inferred from a raised TypeError again" \
  "core/outbox.py" \
  "('        return self.adapter.send(target, text, key=key, thread_id=thread_id)', '        try:\n            return self.adapter.send(target, text, key=key, thread_id=thread_id)\n        except TypeError as ex:\n            raise SendBlocked(str(ex)) from ex')" \
  "test_outbox_threads"

# ENH-3 — recovery must take placement from the ROW. Resuming with None flattens a thread
# reply into the channel after a crash, breaking the policy retroactively and in public.
run_mutation "outbox: recovery resumes a thread reply top-level" \
  "core/outbox.py" \
  "('                receipt = self._deliver(target, row[\"text\"], key, row[\"thread_id\"])', '                receipt = self._deliver(target, row[\"text\"], key, None)')" \
  "test_outbox_threads"

# ENH-3 — placement is part of a reply's IDENTITY. Without it the in-thread and the
# top-level answer to the same trigger share a key, so the second one is deduped away.
run_mutation "outbox: placement drops out of the idempotency key" \
  "core/outbox.py" \
  "('    if thread_id is not None:\n        h.update(b\"\\\\x00\"); h.update(str(thread_id).encode())', '    pass')" \
  "test_outbox_threads"

# ENH-3 — the record. A staged draft and a recovered row both need to know WHERE they go;
# not persisting it makes the outbox unable to answer which placement was used.
run_mutation "outbox: the row stops recording which scope was used" \
  "core/outbox.py" \
  "('                 policy, scope, thread_id, time.time(), time.time()))', '                 policy, \"channel\", None, time.time(), time.time()))')" \
  "test_outbox_threads"

# ENH-3 — an adopter's existing outbox.db predates the thread columns; CREATE TABLE IF NOT
# EXISTS cannot add them, so without the migration every read of an old row raises.
run_mutation "outbox: a pre-thread outbox.db is no longer migrated" \
  "core/outbox.py" \
  "('        for col, ddl in _MIGRATIONS:\n            if col not in have:\n                self.conn.execute(ddl)', '        pass')" \
  "test_outbox_threads"

# ENH-3 — config is where an adopter expresses the policy; ignoring the key means the
# documented setting silently does nothing, the worst possible outcome for a placement rule.
run_mutation "config: thread_reply_policy is parsed and then ignored" \
  "core/config.py" \
  "('        if self.thread_reply_policy is None:\n            return self.reply_policy', '        if True:\n            return self.reply_policy')" \
  "test_outbox_threads"

# ENH-3 — a typo'd placement policy must fail AT LOAD (ENH-20 discipline), not resolve to
# a scope that is never permitted and look like a silent read-only channel.
run_mutation "config: an invalid thread_reply_policy loads clean" \
  "core/config.py" \
  "('            if value not in VALID_POLICIES:', '            if value not in VALID_POLICIES and key == \"reply_policy\":')" \
  "test_outbox_threads"

# ENH-3 — the reference adapter is what adapter authors copy. One that accepts thread_id
# and drops it posts in the main channel and reports success — the exact silent flattening.
run_mutation "fake adapter: accepts a thread and posts top-level anyway" \
  "channels/fake/adapter.py" \
  "('        self.delivered.append((channel_id, text, key, thread_id))', '        self.delivered.append((channel_id, text, key, None))')" \
  "test_outbox_threads"

# ENH-3/R21 — a policy an adopter cannot find is a policy they cannot express. The
# quickstart's own example is loaded through the real loader, so these three re-create
# each way the doc could stop teaching it.

# ENH-3 — copied as-is, this example would post top-level in a channel the adopter had
# just been told stays read-only.
run_mutation "docs: the thread-policy example promotes the main channel too" \
  "docs/QUICKSTART.md" \
  "('\"reply_policy\": \"never\", \"thread_reply_policy\": \"direct\"', '\"reply_policy\": \"direct\", \"thread_reply_policy\": \"direct\"')" \
  "test_docs"

# ENH-3 — the step-2 table is the checklist an adopter edits against; a key that lives
# only in later prose is a key most adopters never see.
run_mutation "docs: the placement key vanishes from the field table" \
  "docs/QUICKSTART.md" \
  "('| \`instances[].channels[].thread_reply_policy\`', '| \`instances[].channels[].some_other_key\`')" \
  "test_docs"

# ENH-3 — per-scope deny-by-default is the non-obvious half: an adopter who sets only
# thread_reply_policy would otherwise assume the main channel inherited it.
run_mutation "docs: per-scope deny-by-default stops being stated" \
  "docs/QUICKSTART.md" \
  "('Each scope is deny-by-default on its own: naming\none placement does not promote the other. ', '')" \
  "test_docs"

# ENH-3 — the operator's turn: an unscoped policy_for() on a thread-scoped channel reports
# the opposite of the truth, and this section is where a refused reply gets debugged.
run_mutation "docs: the runbook diagnostic drops the placement argument" \
  "docs/RUNBOOK.md" \
  "('outbox.policy_for(\"C_YOUR_CHANNEL\", \"thread\")', 'outbox.policy_for(\"C_YOUR_CHANNEL\")')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-4 — attachments are content. The live system downloads screenshots and treats
# them as the message; an engine that keeps only text classified an image-only message
# as an empty STATEMENT — acknowledged and forgotten. Each of these, if it survived,
# re-drops the attachment at one boundary of the pipeline (adapter → store →
# classifier), and any single boundary is enough to lose the content end to end.
# ---------------------------------------------------------------------------

# ENH-4 — THE acceptance: no text + an attachment must be an explicit decision,
# never the empty-message fall-through.
run_mutation "classify: an image-only message is an empty STATEMENT again" \
  "core/classify.py" \
  "('    if not t:\n        if atts:', '    if not t:\n        if False:')" \
  "test_classify"

# ENH-4 — the polling adapter is where a screenshot first exists engine-side;
# dropping it here loses it for every downstream consumer at once.
run_mutation "slack: uploads dropped at normalization" \
  "channels/slack/adapter.py" \
  "('            \"attachments\": _attachments(m),', '            \"attachments\": [],')" \
  "test_slack_adapter"

# ENH-4 — the push path must keep the same upload, or parity holds on ts while the
# two stores disagree about what the message contained.
run_mutation "socket: uploads dropped at normalization" \
  "channels/slack_socket/adapter.py" \
  "('            \"attachments\": _attachments(event),', '            \"attachments\": [],')" \
  "test_socket_adapter"

# ENH-4 — the store must persist what the adapter normalized; binding NULL for
# everything is the silent drop moved one layer down (and erases the None-vs-[]
# distinction that separates legacy rows from attachment-free ones).
run_mutation "store: attachments silently dropped at persistence" \
  "core/store.py" \
  "('    return None if atts is None else json.dumps(atts)', '    return None')" \
  "test_store"

# ENH-4 — the pinned contract must ADMIT the field: without it, validate() refuses
# every attachment-carrying message and the whole batch is lost at ingest.
run_mutation "store: the pinned contract no longer admits attachments" \
  "core/store.py" \
  "('OPTIONAL_FIELDS = (\"sender_name\", \"thread_id\", \"raw\", \"attachments\")', 'OPTIONAL_FIELDS = (\"sender_name\", \"thread_id\", \"raw\")')" \
  "test_store"

# ENH-4 — an adopter's pre-attachment messages.db must be migrated in place (the
# journal's R22 cues-column lesson): unmigrated, every ingest crashes on the missing
# column, which destroys the audit trail in order to improve it.
run_mutation "store: a pre-attachment messages.db is no longer migrated" \
  "core/store.py" \
  "('        if \"attachments\" not in cols:', '        if False:')" \
  "test_store"

# ENH-4 — the caller boundary where the R22 cues once died, same seam: first-poll
# classifies the text and drops the attachments on the way to the classifier.
run_mutation "first-poll: attachments never reach the classifier" \
  "scripts/first-poll.py" \
  "('    c = classify(msg[\"text\"], taxonomy, attachments=msg.get(\"attachments\"))', '    c = classify(msg[\"text\"], taxonomy)')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-15 — the May-2025 distribution-model limit. Since 2025-05-29 the platform
# throttles conversations.history/replies for newly-created, commercially-distributed,
# non-Marketplace apps; internal customer-built apps are exempt (our target case). The
# property is purely documentary, so each mutation below re-creates one way the warning
# could rot: an adopter who learns this AFTER choosing a distribution model finds the
# poll path throttled and blames the engine (state/RESEARCH-INGESTION.md finding 2).
# ---------------------------------------------------------------------------

# ENH-15 — the limit must name the exact methods it throttles; "some read methods" gives
# the adopter nothing to check their own call pattern against.
run_mutation "docs: the distribution-model limit loses the methods it throttles" \
  "docs/QUICKSTART.md" \
  "('sharply reduced limits on \`conversations.history\` and\n  \`conversations.replies\`', 'sharply reduced limits on some read methods')" \
  "test_docs"

# ENH-15 — the exemption IS the decision input: without it every internal adopter — the
# quickstart's target case — reads the limit as applying to them.
run_mutation "docs: the internal-app exemption deleted from honest limits" \
  "docs/QUICKSTART.md" \
  "('Internal customer-built apps are **exempt**', 'Most apps are unaffected')" \
  "test_docs"

# ENH-15 — the risk half of the acceptance: unwarned, a commercially-redistributed
# poller's throttling reads as an engine defect.
run_mutation "docs: the distribution-model risk made vague again" \
  "docs/QUICKSTART.md" \
  "('the poller is throttled by the platform, not by the engine', 'the poller may run slower')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-16 — app_rate_limited as a first-class loss signal. The Events API ceiling is
# 30,000 deliveries/app/workspace/hour; overflow raises an EVENT, not an error. Each
# mutation below re-creates one way the engine could go back to losing messages while
# looking healthy: the admission ignored, health passing over it, recovery without
# poll evidence, or the completeness-poll trigger going quiet.
# ---------------------------------------------------------------------------

# ENH-16 — the defect verbatim: the enveloped admission falls through to _maybe_ingest
# (no `event` key), gets acked, and is forgotten.
run_mutation "socket: an app_rate_limited envelope is acked and forgotten again" \
  "channels/slack_socket/adapter.py" \
  "('        if payload.get(\"type\") == \"app_rate_limited\":', '        if False:')" \
  "test_socket_adapter"

# ENH-16 — the defensive bare-frame path: a loss admission outside the envelope wrapper
# must not become noise.
run_mutation "socket: a bare app_rate_limited frame is noise again" \
  "channels/slack_socket/adapter.py" \
  "('        if kind == \"app_rate_limited\":', '        if False:')" \
  "test_socket_adapter"

# ENH-16 — envelope re-delivery replays the same admission; without the dedupe one
# dropped minute inflates into a pile of phantom gaps.
run_mutation "socket: repeated admissions pile up as distinct gaps" \
  "channels/slack_socket/adapter.py" \
  "('        if any(g[\"start\"] == start for g in self._gaps):', '        if False:')" \
  "test_socket_adapter"

# ENH-16 — THE acceptance property: an unrecovered gap must FAIL health, not ride along
# in the detail string (that is exactly \"logged and forgotten\").
run_mutation "socket: a delivery gap can no longer fail health" \
  "channels/slack_socket/adapter.py" \
  "('        complete = not self._gaps', '        complete = True')" \
  "test_socket_adapter"

# ENH-16 — recovery must demand EVIDENCE: a poll that completed before the window
# ended cannot have covered it, so clearing on any mark_recovered call is forgetting
# with extra steps.
run_mutation "socket: gap recovery stops demanding a poll past the window" \
  "channels/slack_socket/adapter.py" \
  "('        keep = [g for g in self._gaps\n                if g[\"end\"] is not None and g[\"end\"] > completed_at]', '        keep = []')" \
  "test_socket_adapter"

# ENH-16 — the trigger half of the acceptance: an open gap must demand the
# completeness poll NOW (R3: backoff never defers recovery of an admitted loss).
run_mutation "socket: recovery_due stops firing the completeness poll" \
  "channels/slack_socket/adapter.py" \
  "('        return bool(self._gaps)', '        return False')" \
  "test_socket_adapter"

# ENH-16 — the registry side: an admitted gap fed to the checks framework must produce
# a FAIL verdict, or the watchdog layer summarizes over the loss.
run_mutation "checks: an admitted delivery gap passes the registry" \
  "core/checks.py" \
  "('    if gaps:', '    if False:')" \
  "test_checks"

# ---------------------------------------------------------------------------
# ENH-17 — a key the loader does not read is REFUSED, never silently ignored.
# The R21 adoption run planted a top-level "taxonomy", the load succeeded, and
# the adopter believed the classifier retuned when nothing had changed.
# ---------------------------------------------------------------------------

# ENH-17 — THE defect verbatim: every unknown key everywhere is accepted again.
run_mutation "config: unknown keys are silently ignored at every level" \
  "core/config.py" \
  "('    unknown = sorted(k for k in obj if k not in known and not k.startswith(\"_\"))', '    unknown = []')" \
  "test_portability"

# ENH-17 — each call site removed one at a time: a level that stops being checked
# is exactly the level the next inert key lands on.
run_mutation "config: the top level stops being checked (the planted taxonomy loads)" \
  "core/config.py" \
  "('    _refuse_unknown(raw, _TOP_KEYS, \"top level\")', '    pass')" \
  "test_portability"

run_mutation "config: the engine block stops being checked" \
  "core/config.py" \
  "('    _refuse_unknown(eng, _ENGINE_KEYS, \"engine\")', '    pass')" \
  "test_portability"

run_mutation "config: instance objects stop being checked (telegram 'chats' loads)" \
  "core/config.py" \
  "('        _refuse_unknown(spec, _INSTANCE_KEYS, f\"instance {name!r}\")', '        pass')" \
  "test_portability"

run_mutation "config: channel objects stop being checked (a typo'd reply_policy means silent DENY)" \
  "core/config.py" \
  "('            _refuse_unknown(ch, _CHANNEL_KEYS,', '            (lambda *a: None)(ch, _CHANNEL_KEYS,')" \
  "test_portability"

# ENH-17 — the refusal must keep saying where the right-key-wrong-level key lives;
# rejecting the adopter's guess without the pointer restarts the hunt.
run_mutation "config: the wrong-level refusal stops naming the key's real home" \
  "core/config.py" \
  "('    hints = \"\".join(f\" ({k!r} is read per-instance: instances[].{k})\"', '    hints = \"\".join(\"\"')" \
  "test_portability"

# ---------------------------------------------------------------------------
# ENH-5 — the doctor preflight command. Its one job is refusing to look healthy
# when the configuration is not; each mutation below is a way for it to hand an
# adopter a clean bill of health over a broken setup — the vacuous-pass disease
# in a tool that exists specifically to kill the stack-trace-at-first-poll
# experience.
# ---------------------------------------------------------------------------

# ENH-5 — a refused config must never exit healthy; the refusal exit code IS the
# contract scripts build on (same 0/1/2 convention as the SLO judge).
run_mutation "doctor: a refused config exits as if healthy" \
  "core/doctor.py" \
  "('        return 2', '        return 0')" \
  "test_doctor"

# ENH-5 — health() can FAIL (contract rule 5); a doctor that shrugs it off is the
# incumbent's inert watchdog handed to every adopter.
run_mutation "doctor: an unreachable adapter reads healthy" \
  "core/doctor.py" \
  "('    if not h[\"reachable\"]:', '    if False:')" \
  "test_doctor"

run_mutation "doctor: platform-refused credentials read healthy" \
  "core/doctor.py" \
  "('    if not h[\"auth_ok\"]:', '    if False:')" \
  "test_doctor"

# ENH-5 — a junk health dict still fails without this guard, but as a raw KeyError:
# the exact errno-grade message ENH-5 exists to replace with a fix instruction.
run_mutation "doctor: a health surface missing the contract keys loses its named refusal" \
  "core/doctor.py" \
  "('    if not isinstance(h, dict) or not {\"reachable\", \"auth_ok\"} <= set(h):', '    if False:')" \
  "test_doctor"

# ENH-5/ENH-16 — an admitted delivery gap is a loss signal, never a detail string.
run_mutation "doctor: an admitted delivery gap reads healthy" \
  "core/doctor.py" \
  "('    if h.get(\"complete\") is False:', '    if False:')" \
  "test_doctor"

# ENH-27 — the doctor is the preflight where the snapshot-capability gap gets said;
# a doctor that never registers the check leaves the first parity run to say it as
# unexplained ENGINE_LOST rows.
run_mutation "doctor: the snapshot capability check is never registered" \
  "core/doctor.py" \
  "('        reg.add(f\"{inst.name}:parity-snapshot\",', '        _ = (f\"{inst.name}:parity-snapshot\",')" \
  "test_doctor"

# ENH-27 — a doctor that reports every adapter as snapshot-capable un-declares the gap
# while keeping the check name in the output.
run_mutation "doctor: every adapter reads as snapshot-capable" \
  "core/doctor.py" \
  "('    declaration = snapshot_declaration(probe.adapter(), inst.adapter)', '    declaration = None')" \
  "test_doctor"

# ENH-5 — 'each channel is readable' is proven by the live poll; swallowing the
# poll's failure certifies channels nobody read.
run_mutation "doctor: a failed preflight poll no longer fails the channels" \
  "core/doctor.py" \
  "('        if self._poll_err is not None:\n            raise self._poll_err', '        if False:\n            raise self._poll_err')" \
  "test_doctor"

# ENH-5 — the two-place footgun (measured, fire=11): an id in config but not in the
# adapter's own watch set polls nothing and looks successful.
run_mutation "doctor: the watch-set cross-check stops firing (two-place footgun)" \
  "core/doctor.py" \
  "('    if watched is not None and ch.id not in watched:', '    if False:')" \
  "test_doctor"

# ENH-5 — an adapter that cannot read has nothing to preflight; passing it certifies
# a watcher that will never watch.
run_mutation "doctor: a read-incapable adapter passes the preflight" \
  "core/doctor.py" \
  "('    if not caps.get(\"read\"):', '    if False:')" \
  "test_doctor"

# ENH-5 — the acceptance says PRINTS: a doctor that resolves the policy and shows
# nothing leaves the adopter reading the config file again.
run_mutation "doctor: the per-channel policy lines vanish from the output" \
  "core/doctor.py" \
  "('            label = f\" [{ch.label}]\" if ch.label else \"\"', '            continue')" \
  "test_doctor"

# ENH-5 — each placement printed truthfully (ENH-3): printing the channel policy for
# the thread column reports 'answer in thread only' as fully read-only.
run_mutation "doctor: the thread placement prints the channel policy" \
  "core/doctor.py" \
  "(\"                 f\\\"thread={_effective_policy(inst, ch.id, 'thread')}\\\")\", \"                 f\\\"thread={_effective_policy(inst, ch.id, 'channel')}\\\")\")" \
  "test_doctor"

# ENH-5 — THE property: a failing verdict must fail the run. With this gone every
# other check is decoration.
run_mutation "doctor: a failing check no longer fails the exit code" \
  "core/doctor.py" \
  "('    if bad:', '    if False:')" \
  "test_doctor"

# ENH-5 — no vacuous pass: an instance watching nothing has zero evidence of health.
run_mutation "doctor: an instance watching no channels passes" \
  "core/doctor.py" \
  "('        if not inst.channels:', '        if False:')" \
  "test_doctor"

# ENH-5 — the doctor names WHICH references resolved, never their values; echoing a
# token turns a diagnostic paste into a leak.
run_mutation "doctor: resolved credential VALUES printed instead of reference names" \
  "core/doctor.py" \
  "('    names = _env_ref_names(config_path)', '    names = [f\"{k}={v}\" for i in cfg.instances for k, v in sorted(i.auth.items())]')" \
  "test_doctor"

# ENH-5 — the defect from this command's first live run, verbatim: a text scan reports
# the example's _note prose ('env:NAME') as a resolved credential.
run_mutation "doctor: prose mentions of env: reported as resolved credentials again" \
  "core/doctor.py" \
  "('    names = _env_ref_names(config_path)', '    names = sorted(set(__import__(\"re\").findall(r\"env:([A-Za-z_][A-Za-z0-9_]*)\", config_path.read_text())))')" \
  "test_doctor"

# ENH-5 — the probe reuses the engine's own cursor (bounded work on a live install);
# ignoring it re-reads full history on every preflight.
run_mutation "doctor: the probe ignores the engine's persisted cursor" \
  "core/doctor.py" \
  "('        probe = _Probe(cfg.channels_dir, inst, _persisted_cursor(cfg, inst))', '        probe = _Probe(cfg.channels_dir, inst, None)')" \
  "test_doctor"

# ENH-5/R21 — a preflight nobody is told about diagnoses nothing: both docs must keep
# showing the command at the moment the adopter needs it.
run_mutation "docs: the runbook's first-move preflight deleted" \
  "docs/RUNBOOK.md" \
  "('python3 -m core.doctor --config settings.json', 'python3 -m unittest discover -s tests -q')" \
  "test_docs"

run_mutation "docs: the quickstart's preflight line deleted" \
  "docs/QUICKSTART.md" \
  "('python3 -m core.doctor --config settings.json  # preflight BEFORE the first poll', '# preflight removed')" \
  "test_docs"

# ENH-6 — the single-instance guard is why cron overlap REFUSES instead of racing;
# unguarded, two loops double-classify and race the cursor on one state directory.
run_mutation "schedule: run() no longer acquires the single-instance guard" \
  "core/schedule.py" \
  "('        guard = (SingleInstanceGuard(self.lock_path).acquire()\n                 if self.lock_path else None)', '        guard = None')" \
  "test_schedule"

# ENH-6 — THE ordering: cursor after journal means a crash duplicates (absorbed, R16);
# cursor first means a crash silently loses the tail of the batch, with no reconciler.
run_mutation "schedule: cursor committed before the journal" \
  "core/schedule.py" \
  "('                fresh += self._journal_and_route(ch, mine, src)\n                self._commit_cursor(src.name, ch, cursor, new_cursor)', '                self._commit_cursor(src.name, ch, cursor, new_cursor)\n                fresh += self._journal_and_route(ch, mine, src)')" \
  "test_schedule"

# ENH-6/R3 — the 8h17m bug verbatim: backoff self-gates exactly when owed work stalls.
run_mutation "schedule: backoff suppresses owed work again" \
  "core/schedule.py" \
  "('        if self.owed.should_fire(self._backoff_until, now):', '        if False:')" \
  "test_schedule"

# ENH-6/R4 — the goal-triggered edge: without it, promised work is only ever noticed
# when a new inbound message happens to fire the loop's attention.
run_mutation "schedule: the owed check deleted from the cycle" \
  "core/schedule.py" \
  "('        unattended = self._observe_owed()', '        unattended = 0')" \
  "test_schedule"

# ENH-6 — route is classify->consequence; without the owe, an EXEC-REQUEST is a
# journal row nobody owes anything about — the incident's exact shape.
run_mutation "schedule: routed asks stop creating owed work" \
  "core/schedule.py" \
  "('            if dest.startswith(\"owed:\") and (res.is_new or res.is_revision):', '            if False:')" \
  "test_schedule"

# ENH-6 — the lifecycle exit: answered asks must close, or the loop pages the
# operator about finished work forever and gets muted.
run_mutation "schedule: answered owed work never closes" \
  "core/schedule.py" \
  "('        self._close_answered()', '        pass')" \
  "test_schedule"

# ENH-6 — a busy channel stuck at the widened cadence is the backoff bug's mirror.
run_mutation "schedule: activity no longer resets the backoff" \
  "core/schedule.py" \
  "('        self._interval = (self.base_interval if fresh', '        self._interval = (self._interval if fresh')" \
  "test_schedule"

# ENH-6 — a kind the map has never heard of must fail toward a human; 'logged' as the
# default is the silently-inert class wearing a routing table.
run_mutation "schedule: unknown kinds routed to silence" \
  "core/schedule.py" \
  "('    return ROUTES.get(kind, \"owed:operator\")', '    return ROUTES.get(kind, \"logged\")')" \
  "test_schedule"

# ENH-6 — answered() is what the scheduler closes owed work from; if it lies, open
# asks get closed the cycle they arrive.
run_mutation "journal: answered() returns the unanswered rows" \
  "core/journal.py" \
  "('        q = \"SELECT * FROM journal WHERE responded_at IS NOT NULL\"', '        q = \"SELECT * FROM journal WHERE responded_at IS NULL\"')" \
  "test_journal"

# ENH-6 — the script wiring: a lock refusal that exits 0 tells cron the fire ran.
run_mutation "scheduler-script: the lock refusal reports success" \
  "scripts/scheduler.py" \
  "('        return 3', '        return 0')" \
  "test_schedule"

# ENH-6 — the script must actually pass the lock path; run() without one is unguarded.
run_mutation "scheduler-script: the loop runs unguarded" \
  "scripts/scheduler.py" \
  "('lock_path=cfg.state_dir / \"scheduler.lock\"', 'lock_path=None')" \
  "test_schedule"

# ENH-6/R21 — the 'no scheduler' claim was true for months; it must not quietly return
# over an implemented, tested loop (the made-vague-again class).
run_mutation "docs: the quickstart claims no scheduler ships again" \
  "docs/QUICKSTART.md" \
  "('is a single-process loop, not', 'does not ship — there is no scheduler in this repo, and it is not')" \
  "test_docs"

# ENH-6/R21 — the reflex fix is the dangerous one: deleting the lock puts two loops
# on one state directory. The runbook must keep saying never.
run_mutation "docs: the runbook starts teaching the lock-deletion reflex" \
  "docs/RUNBOOK.md" \
  "('**never delete\nthe lock**', 'delete the lock')" \
  "test_docs"

# ENH-7 — cursors lose their instance namespace: tenant B's watermark write REPLACEs
# tenant A's row for the same channel id (two workspaces, one channel name).
run_mutation "store: cursor rows keyed per channel instead of per (instance, channel)" \
  "core/store.py" \
  "('    PRIMARY KEY (instance, channel_id)', '    PRIMARY KEY (channel_id)')" \
  "test_isolation"

# ENH-7 — the loop commits every tenant's cursor under one shared name.
run_mutation "schedule: cursor committed under a shared key, not the instance" \
  "core/schedule.py" \
  "('self._commit_cursor(src.name, ch, cursor, new_cursor)', 'self._commit_cursor(\"engine\", ch, cursor, new_cursor)')" \
  "test_isolation"

# ENH-7 — the loop reads every tenant's cursor from one shared name (the other half
# of the same collapse: each tenant re-polls from scratch or from the wrong watermark).
run_mutation "schedule: cursor read from a shared key, not the instance" \
  "core/schedule.py" \
  "('cursor = self.store.cursor_get(src.name, ch)', 'cursor = self.store.cursor_get(\"engine\", ch)')" \
  "test_isolation"

# ENH-7 — per-instance outbox paths collapse to one shared file: recovery replays the
# other tenant's INTENT through the wrong adapter, dedup eats the other tenant's reply.
run_mutation "config: every instance handed one shared outbox file" \
  "core/config.py" \
  "('        return p.with_name(f\"{p.stem}-{name}{p.suffix}\")', '        return p')" \
  "test_isolation"

# ENH-7 — duplicate instance names silently merge two tenants into one.
run_mutation "config: duplicate instance names accepted" \
  "core/config.py" \
  "('        if any(i.name == name for i in instances):', '        if False:')" \
  "test_isolation"

# ENH-7 — a path-shaped name escapes the state directory via per-instance filenames.
run_mutation "config: path-shaped instance names accepted" \
  "core/config.py" \
  "('        if not re.fullmatch(r\"[A-Za-z0-9][A-Za-z0-9._-]*\", name):', '        if False:')" \
  "test_isolation"

# ENH-7 — one tenant's resolved credentials leak into the next instance's auth (the
# shared-accumulator bleed shape).
run_mutation "config: credentials accumulate across instances" \
  "core/config.py" \
  "('        auth = {k: resolve_secret(v, env) for k, v in (spec.get(\"auth\") or {}).items()}', '        auth = dict(instances[-1].auth) if instances else {}\n        auth.update({k: resolve_secret(v, env) for k, v in (spec.get(\"auth\") or {}).items()})')" \
  "test_isolation"

# ENH-8 — the viewer property. mode=ro is what makes the dashboard structurally unable
# to alter the audit trail it displays; a writable connection is one bug from an editor.
run_mutation "dashboard: the read connection accepts writes" \
  "core/dashboard.py" \
  "('sqlite3.connect(f\"file:{p}?mode=ro\", uri=True)', 'sqlite3.connect(f\"file:{p}?mode=rwc\", uri=True)')" \
  "test_dashboard"

# ENH-8 — an INTENT/SENT row is a send that may have died mid-flight (the 24-reconcile
# seam, viewed from the operator's side). Dropping it hides double-message risk.
run_mutation "dashboard: crashed in-flight sends vanish from the attention queue" \
  "core/dashboard.py" \
  "(\"WHERE state IN ('INTENT','SENT')\", \"WHERE state IN ('COMMITTED')\")" \
  "test_dashboard"

# ENH-8 — the operator gate: a staged draft the surface never shows is a draft nobody
# ever approves, which silently turns 'staged' policy into 'never'.
run_mutation "dashboard: staged drafts leave the operator gate" \
  "core/dashboard.py" \
  "(\"WHERE state='STAGED'\", \"WHERE state='STAGED' AND 0\")" \
  "test_dashboard"

# ENH-8 — severity order: the banner exists so the double-message risk is read FIRST.
run_mutation "dashboard: the unanswered backlog outranks a crashed send" \
  "core/dashboard.py" \
  "('SEVERITY = (\"engine_lost\", \"in_flight\", \"edited_after_response\", \"staged\",\n            \"unanswered\")', 'SEVERITY = (\"engine_lost\", \"unanswered\", \"edited_after_response\", \"staged\",\n            \"in_flight\")')" \
  "test_dashboard"

# ENH-8/R23 — an edit after our answer means the visible reply may answer text that no
# longer exists; comparing the wrong way round buries exactly those rows.
run_mutation "dashboard: an edit after our answer stops being surfaced" \
  "core/dashboard.py" \
  "('r.recorded_at > j.responded_at', 'r.recorded_at < j.responded_at')" \
  "test_dashboard"

# ENH-8 — HARD security bound: the UI renders journal text and staged drafts, so the
# serve wrapper must bind loopback only; streamlit's own default is every interface.
run_mutation "dashboard-serve: the UI binds beyond loopback" \
  "scripts/dashboard-serve.sh" \
  "('--server.address 127.0.0.1', '--server.address 0.0.0.0')" \
  "test_dashboard"

# ENH-8 — the acceptance itself: THEIR journal and outbox, resolved from THEIR config.
# A hardwired state path renders this repo's state and calls it the adopter's.
run_mutation "dashboard-script: reads a hardwired path instead of the adopter's config" \
  "scripts/dashboard.py" \
  "('snap = snapshot(cfg.journal_path, outbox_paths,', 'snap = snapshot(Path(\"state/journal.db\"), outbox_paths,')" \
  "test_dashboard"

# ---------------------------------------------------------------------------
# ENH-10 — compose: the citation discipline. The origin rule these encode is "never
# improvise a number, cite banked artifacts"; each mutation below re-opens one door a
# figure could walk through uncited, on its way to a customer channel in the
# operator's name.
# ---------------------------------------------------------------------------

# The one definition of "a number" both prose() and claim() share. Inert detector =
# no claim is ever factual = the whole layer waves everything through.
run_mutation "compose: the factual-claim detector goes inert" \
  "core/compose.py" \
  "('return NUMBER.findall(text or \"\")', 'return []')" \
  "test_compose"

# A number entering as prose is the exact improvisation the layer exists to stop.
run_mutation "compose: prose stops refusing numbers" \
  "core/compose.py" \
  "('        if numbers:', '        if False:')" \
  "test_compose"

# Needs its own teeth: on a number-free claim no other check fires afterwards.
run_mutation "compose: a claim no longer requires a citation" \
  "core/compose.py" \
  "('        if not any(refs):', '        if False:')" \
  "test_compose"

# A citation to nothing reads exactly like a citation to evidence.
run_mutation "compose: citations to unbanked artifacts admitted" \
  "core/compose.py" \
  "('        unbanked = [r for r in refs if r not in self.bank]', '        unbanked = []')" \
  "test_compose"

# Laundering: a REAL ref stapled to an INVENTED figure must still be refused.
run_mutation "compose: improvised numbers pass with a real citation" \
  "core/compose.py" \
  "('        improvised = [n for n in factual_claims(text) if n not in banked]', '        improvised = []')" \
  "test_compose"

# Coverage must come from the artifact itself, not only from digits in its name —
# otherwise banking an empty artifact under a numeric ref covers anything.
run_mutation "compose: artifact content dropped from coverage" \
  "core/compose.py" \
  "('            banked.update(factual_claims(self.bank[r]))', '            pass')" \
  "test_compose"

# The citation must be ON the wire: a receipt the recipient cannot see is not one.
run_mutation "compose: render drops the citation from the reply" \
  "core/compose.py" \
  "('if p.refs else p.text', 'if False else p.text')" \
  "test_compose"

# ---------------------------------------------------------------------------
# ENH-22 — adopter ergonomics. Each of these, if it survived, re-creates one measured
# way the docs and their objects were unusable as written: drafts and edit history
# printing as '<sqlite3.Row object at 0x...>', snippets with no documented constructor,
# advice pointing at a differ no doc names, and a hook check whose healthy result
# reads as a failure.
# ---------------------------------------------------------------------------

# ENH-22 — staged() is read by the human gating the drafts; a raw row hides the text.
run_mutation "outbox: staged() hands back raw row handles again" \
  "core/outbox.py" \
  "('        return [dict(r) for r in rows]', '        return rows')" \
  "test_outbox_faults"

# ENH-22 — revisions() is the audit walk a human reads while disputing a decision.
run_mutation "journal: revisions() hands back raw row handles again" \
  "core/journal.py" \
  "('        return [dict(r) for r in rows]', '        return rows')" \
  "test_journal"

# ENH-22 — without the constructor block, every snippet on the page needs a source
# dive first (the adopter re-run could instantiate Journal only from memory of a
# prior session).
run_mutation "docs: the runbook constructor block loses its construction" \
  "docs/RUNBOOK.md" \
  "('cfg = load(\"settings.json\")', 'cfg = your_engine_config   # construct however you like')" \
  "test_docs"

# ENH-22 — 'run both and diff' pointing at no tool is homework, not advice.
run_mutation "docs: the parity differ loses its name in honest limits" \
  "docs/QUICKSTART.md" \
  "('python3 -m core.parity --oracle', 'run a diff of your own against')" \
  "test_docs"

# ENH-22 — the check that prints nothing on a healthy clone, reintroduced verbatim.
run_mutation "docs: the hook check reverts to the silent-on-healthy form" \
  "docs/RUNBOOK.md" \
  "('ls .git/hooks/pre-commit .git/hooks/pre-push  # healthy: both paths print', 'git config core.hooksPath; ls .git/hooks/pre-push')" \
  "test_docs"

# ENH-9 — the hedge signal, per boundary. "Ambiguity silently becomes STATEMENT: safe
# but lossy and invisible" — each mutation below restores one piece of the invisibility.

# The downgrade stops signalling: ambiguity is a reason-string again, unroutable.
run_mutation "classify: the ambiguous downgrade stops raising its signal" \
  "core/classify.py" \
  "('                              exec_hits, ambiguous=True)', '                              exec_hits)')" \
  "test_classify"

# The signal reaches record() but dies before the row — stats read all-unrecorded.
run_mutation "journal: the confidence signal is never persisted" \
  "core/journal.py" \
  "('        amb = None if ambiguous is None else int(bool(ambiguous))', '        amb = None')" \
  "test_journal"

# A pre-ENH-9 journal.db keeps its old schema; the first record() with a decision fails.
run_mutation "journal: legacy databases never gain the ambiguity column" \
  "core/journal.py" \
  "('        if \\\"ambiguous\\\" not in cols:', '        if False:')" \
  "test_journal"

# The opt-in flag becomes decorative: hedges are logged for everyone again.
run_mutation "schedule: the escalate flag stops routing hedges to a human" \
  "core/schedule.py" \
  "('    if ambiguous and escalate_ambiguous:', '    if False:')" \
  "test_schedule"

# The loop routes on the signal but stops recording it — the count silently dies at
# the exact caller boundary where the R22 cues once died.
run_mutation "schedule: the loop stops recording the confidence signal" \
  "core/schedule.py" \
  "('                matched=c.matched, routed=dest, ambiguous=c.ambiguous)', '                matched=c.matched, routed=dest)')" \
  "test_schedule"

# The key loads (no unknown-key refusal) but configures nothing — ENH-17's inert class.
run_mutation "config: escalate_ambiguous is accepted and read by nothing" \
  "core/config.py" \
  "('        esc = spec.get(\"escalate_ambiguous\", False)', '        esc = False')" \
  "test_portability"

# The JSON footgun returns: a quoted \"false\" silently ENABLES escalation.
run_mutation "config: a quoted escalate_ambiguous loads clean and counts as true" \
  "core/config.py" \
  "('        if not isinstance(esc, bool):', '        if False:')" \
  "test_portability"

# Parsed but not passed to Source: the flag is silently inert in the shipped wiring
# (the ENH-7 adapter-wiring lesson).
run_mutation "scheduler.py: the wiring drops the escalate flag" \
  "scripts/scheduler.py" \
  "('                   escalate_ambiguous=inst.escalate_ambiguous,', '                   escalate_ambiguous=False,')" \
  "test_schedule"

# first-poll journals rows that read as never-classified — the hedge count starts
# broken for every adopter (the same boundary as the cues/attachments mutations).
run_mutation "first-poll: the confidence signal is dropped at the journal call" \
  "scripts/first-poll.py" \
  "('                          matched=c.matched, ambiguous=c.ambiguous)', '                          matched=c.matched)')" \
  "test_docs"

# An undocumented opt-in key is inert in practice; the doc must name the exact key.
run_mutation "docs: step 6 loses the escalation flag's name" \
  "docs/QUICKSTART.md" \
  "('\"escalate_ambiguous\": true', '\"escalated_ambiguity\": true')" \
  "test_docs"

# ENH-24 — the operator surface shows the parity VERDICT, not raw divergence counts.
# Each of these, if it survived, re-creates the measured failure: '342 missed, 24
# extra' rendered for a channel whose truth was PARITY OK / ENGINE_LOST=0.

# The lead number silently becomes the raw missed count — the scary number returns
# wearing the verdict's name.
run_mutation "parity: the panel's lead number becomes the raw missed count" \
  "core/parity.py" \
  "('        lost = sorted(self.by_class(ENGINE_LOST), key=_fl if self.orderable else str)', '        lost = sorted(self.missed, key=_fl if self.orderable else str)')" \
  "test_parity"

# The configuration that waived classes goes unnamed — a green verdict with no
# reviewable argument behind it.
run_mutation "parity: the accept-list in force goes unnamed" \
  "core/parity.py" \
  "('            \"accept_list\": sorted(self.accepted),', '            \"accept_list\": [],')" \
  "test_parity"

# Raw counts move back to the head of the panel; key order is render order.
run_mutation "parity: raw divergence counts return to the head of the panel" \
  "core/parity.py" \
  "('        return {\n            \"verdict\": \"PARITY OK\" if self.ok else \"PARITY FAIL\",', '        return {\n            \"raw\": {\"missed\": len(self.missed), \"extra\": len(self.extra)},\n            \"verdict\": \"PARITY OK\" if self.ok else \"PARITY FAIL\",')" \
  "test_parity"

# A proven archive loss sinks below a maybe-double-send — the queue stops saying
# 'nothing below this line can be trusted'.
run_mutation "dashboard: a proven loss no longer outranks a crashed send" \
  "core/dashboard.py" \
  "('SEVERITY = (\"engine_lost\", \"in_flight\", \"edited_after_response\", \"staged\",', 'SEVERITY = (\"in_flight\", \"engine_lost\", \"edited_after_response\", \"staged\",')" \
  "test_dashboard"

# ENGINE_LOST goes nonzero and no attention item fires — the panel exists but the
# operator is never pointed at it.
run_mutation "dashboard: the parity attention item goes silent" \
  "core/dashboard.py" \
  "('        if panel.get(\"engine_lost\"):', '        if False:')" \
  "test_dashboard"

# No retention db renders as 'tombstones: 0' — 'nothing was ever deleted' claimed
# on no evidence (the F-2 false-confidence shape).
run_mutation "dashboard: a missing tombstone db renders as zero" \
  "core/dashboard.py" \
  "('    if tombstones is None or not Path(tombstones).is_file():\n        return None', '    if tombstones is None or not Path(tombstones).is_file():\n        return 0')" \
  "test_dashboard"

# The shell stops naming what was waived — the operator sees 'accepted' with no
# reviewable list of what acceptance means here.
run_mutation "dashboard.py (shell): the accept-list in force goes unnamed" \
  "scripts/dashboard.py" \
  "('accept-list in force: {accepts}', 'accept-list in force: (unnamed)')" \
  "test_dashboard"

# The raw-count footnote climbs back above the verdict on the rendered surface.
run_mutation "dashboard.py (shell): raw divergence counts lead the parity tab again" \
  "scripts/dashboard.py" \
  "('            st.markdown(lead)\n            if breakdown:', '            st.markdown(raw_note, unsafe_allow_html=True)\n            st.markdown(lead)\n            if breakdown:')" \
  "test_dashboard"

# ENH-25 — THE telegram gap-free property: the offset sent is exactly the cursor the
# engine committed (after the journal write). One update past it acknowledges — destroys
# at the platform — a message that was never made durable.
run_mutation "telegram: offset overshoots the committed cursor" \
  "channels/telegram/adapter.py" \
  "('            params[\"offset\"] = cursor', '            params[\"offset\"] = str(int(cursor) + 1)')" \
  "test_telegram_adapter"

# ENH-25 — in-poll pagination advances the offset past batches the engine has not
# committed; a crash mid-drain loses them at the platform, unrecoverably.
run_mutation "telegram: poll drains the queue with an in-poll offset advance" \
  "channels/telegram/adapter.py" \
  "('        payload = self._api(\"getUpdates\", **params)\n        batch = payload.get(\"result\") or []', '        payload = self._api(\"getUpdates\", **params)\n        batch = list(payload.get(\"result\") or [])\n        while batch and len(batch) % PAGE_LIMIT == 0:\n            params[\"offset\"] = str(batch[-1][\"update_id\"] + 1)\n            more = self._api(\"getUpdates\", **params).get(\"result\") or []\n            if not more:\n                break\n            batch.extend(more)')" \
  "test_telegram_adapter"

# ENH-25 — a junk cursor must be refused BEFORE any I/O: a getUpdates call with a
# guessed offset is itself the destructive act.
run_mutation "telegram: a junk cursor is guessed at instead of refused" \
  "channels/telegram/adapter.py" \
  "('        if cursor is not None and not _CHAT_ID.fullmatch(str(cursor)):', '        if False:')" \
  "test_telegram_adapter"

# ENH-25 — deny-by-default at the transport funnel; without it the no-send property is
# one convenience wrapper away from being lost (the channels/slack lesson).
run_mutation "telegram: the read allowlist stops being enforced" \
  "channels/telegram/adapter.py" \
  "('        if method not in READ_METHODS:\n            raise ReadOnlyViolation(method)', '        pass')" \
  "test_telegram_adapter"

# ENH-25 — Telegram's 429 wait lives in the BODY (parameters.retry_after); a helper
# that only reads headers silently invents a wait on this platform.
run_mutation "telegram: 429 wait read from the header instead of the body" \
  "channels/telegram/adapter.py" \
  "('            wait = (payload.get(\"parameters\") or {}).get(\"retry_after\")\n            if wait is None:\n                wait = _header(headers, \"Retry-After\", \"1\")', '            wait = _header(headers, \"Retry-After\", \"1\")')" \
  "test_telegram_adapter"

# ENH-25 — an unlabelled 429 collapses ENH-1's (instance, method) back-off scope.
run_mutation "telegram: 429 loses the method name" \
  "channels/telegram/adapter.py" \
  "('            ex = RateLimited(wait, method=method)', '            ex = RateLimited(wait)')" \
  "test_telegram_adapter"

# ENH-25 — one chat per instance: a second chat sharing the bot queue lets one chat's
# committed cursor acknowledge the other's un-journaled messages.
run_mutation "telegram: a second chat is silently accepted" \
  "channels/telegram/adapter.py" \
  "('        if len(chats) != 1:', '        if len(chats) > 99:')" \
  "test_telegram_adapter"

# ENH-25 — ts identity: message.date is whole seconds, so burst messages would merge
# into one store row (PRIMARY KEY channel_type, channel_id, ts).
run_mutation "telegram: ts becomes the wall-clock second" \
  "channels/telegram/adapter.py" \
  "('            \"ts\": str(m[\"message_id\"]),', '            \"ts\": str(m[\"date\"]),')" \
  "test_telegram_adapter"

# ENH-25 — retrievable_ts must stay ABSENT: a stub snapshot marks every still-real
# message 'deleted upstream' — the one direction that hides a real loss — and
# un-declares the ENH-27 capability gap.
run_mutation "telegram: a stub snapshot capability appears" \
  "channels/telegram/adapter.py" \
  "('    def resolve(self, ref):', '    def retrievable_ts(self, channel, oldest=None, latest=None):\n        return set()\n\n    def resolve(self, ref):')" \
  "test_telegram_adapter"

# ENH-25 — the ingest set is pinned at the PLATFORM (allowed_updates); without it the
# queue holds kinds the adapter drops, and the next ack destroys them unread.
run_mutation "telegram: allowed_updates no longer pins the ingest set" \
  "channels/telegram/adapter.py" \
  "('        params = {\"limit\": PAGE_LIMIT, \"timeout\": 0,\n                  \"allowed_updates\": json.dumps(list(INGEST_UPDATES))}', '        params = {\"limit\": PAGE_LIMIT, \"timeout\": 0}')" \
  "test_telegram_adapter"

# ENH-25/ENH-4 — attachments are content; dropping them turns an image-only message
# into an empty row the classifier acknowledges and forgets.
run_mutation "telegram: attachments dropped at normalization" \
  "channels/telegram/adapter.py" \
  "('            \"attachments\": _attachments(m),', '            \"attachments\": [],')" \
  "test_telegram_adapter"

# ENH-25 — attachment urls stay None: Telegram file URLs embed the bot token, and the
# url field is the seam through which a credential would enter every adopter's store.
run_mutation "telegram: attachment url minted from the platform handle" \
  "channels/telegram/adapter.py" \
  "('                        \"mimetype\": media.get(\"mime_type\"), \"url\": None})', '                        \"mimetype\": media.get(\"mime_type\"), \"url\": media.get(\"file_id\")})')" \
  "test_telegram_adapter"

# ENH-25 — watched-channels rule: the bot queue carries every chat the bot is in, and
# ingesting foreign rows stores channels the instance never declared.
run_mutation "telegram: foreign chats are ingested" \
  "channels/telegram/adapter.py" \
  "('            if str((m.get(\"chat\") or {}).get(\"id\")) == self.channels[0]:\n                messages.append(self._normalize(update, m))', '            messages.append(self._normalize(update, m))')" \
  "test_telegram_adapter"

# ENH-25 — contract rule 5: a health check that can only pass is a defect (the reason
# this repo exists, docs/PROVENANCE.md).
run_mutation "telegram: health reports auth_ok on a rejected token" \
  "channels/telegram/adapter.py" \
  "('            return {\"reachable\": True, \"auth_ok\": False,\n                    \"detail\": f\"getMe failed: {ex.error}{detail_suffix}\"}', '            return {\"reachable\": True, \"auth_ok\": True,\n                    \"detail\": f\"getMe failed: {ex.error}{detail_suffix}\"}')" \
  "test_telegram_adapter"

# ENH-25 — the doc's telegram headline: without the no-history finding, the first
# fail-closed parity run gets misread as a read-path defect (the R8 misreading).
run_mutation "docs: telegram's no-history-API limit vanishes" \
  "docs/QUICKSTART.md" \
  "('have **no history API**', 'have **a queue-shaped API**')" \
  "test_docs"

# ENH-25 — and the stated consequence: parity can NEVER go green on deleted history.
run_mutation "docs: the permanently-fail-closed parity claim vanishes" \
  "docs/QUICKSTART.md" \
  "('**permanently fail-closed**', '**conservative at first**')" \
  "test_docs"

# ENH-26 — parity's non-orderable mode IS the email acceptance: if the orderability
# probe hardcodes True, every Message-ID comparison dies in min(map(_fl, ...)) and the
# parity path stops working end to end with non-numeric identity.
run_mutation "parity: every id space is assumed orderable again" \
  "core/parity.py" \
  "('    orderable = _orderable(served | oracle_ts | candidate_ts, channel)', '    orderable = True')" \
  "test_parity"

# ENH-26 — a numeric covered_from/covered_through on a non-orderable id space must be
# refused, not silently ignored: _fl accepts the number happily, so without this guard
# the caller's window argument configures nothing (the silent-no-op class, F-2).
run_mutation "parity: a window on non-orderable ids is silently ignored" \
  "core/parity.py" \
  "('    if not orderable and (covered_from is not None or covered_through is not None):', '    if False and (covered_from is not None or covered_through is not None):')" \
  "test_parity"

# ENH-26 — a MIXED id space is corruption (two id shapes in one channel), never a mode:
# degrading it to the unordered path would hide an adapter emitting two identity schemes.
run_mutation "parity: a mixed id space degrades to the unordered mode" \
  "core/parity.py" \
  "('    if len(unparseable) == len(set(ids)):\n        return False', '    if unparseable:\n        return False')" \
  "test_parity"

# ENH-26 — the degradation must be NAMED in the summary: an operator who does not know
# the window classes cannot fire on this id space reads their absence as a clean bill.
run_mutation "parity: the unavailable window classes go unnamed" \
  "core/parity.py" \
  "('        if not self.orderable:', '        if False:')" \
  "test_parity"

# ENH-26 — THE email identity property: ts is the Message-ID, never the Date header
# (sender-controlled, whole seconds — burst messages merge into one store row).
run_mutation "email: identity becomes the Date header" \
  "channels/email/adapter.py" \
  "('            \"ts\": mid,', '            \"ts\": str(msg.get(\"Date\")),')" \
  "test_email_adapter"

# ENH-26 — and never the UID: a UIDVALIDITY reset reassigns every UID, so a UID-keyed
# store duplicates the whole mailbox after a reset.
run_mutation "email: identity becomes the mailbox UID" \
  "channels/email/adapter.py" \
  "('            \"ts\": mid,', '            \"ts\": str(uid),')" \
  "test_email_adapter"

# ENH-26 — a surrogate identity derived from anything but the message bytes mints a new
# identity per sighting: every re-poll duplicates the row (R9).
run_mutation "email: the missing-Message-ID surrogate varies per sighting" \
  "channels/email/adapter.py" \
  "('        return f\"<{hashlib.sha256(raw_bytes).hexdigest()}@no-message-id.invalid>\"', '        return f\"<{hashlib.sha256(repr(object()).encode()).hexdigest()}@no-message-id.invalid>\"')" \
  "test_email_adapter"

# ENH-26 — THE ordering rule: mailbox UID (arrival) order, not whatever order the wire
# happened to serve the SEARCH result in.
run_mutation "email: ordering becomes the wire order of the search reply" \
  "channels/email/adapter.py" \
  "('        return sorted(uid for uid in map(int, found) if uid > last)', '        return [uid for uid in map(int, found) if uid > last]')" \
  "test_email_adapter"

# ENH-26 — UID SEARCH n:* returns the newest message even when n exceeds it (RFC 3501
# range semantics); unfiltered, every idle poll re-serves the tip forever.
run_mutation "email: the n:* quirk row is re-served on every idle poll" \
  "channels/email/adapter.py" \
  "('if uid > last)', 'if uid >= last)')" \
  "test_email_adapter"

# ENH-26 — threading: References is the ancestor chain and its FIRST id is the root
# (the Slack thread_ts alignment); dropping to In-Reply-To alone re-threads deep
# replies onto their immediate parent.
run_mutation "email: thread derivation ignores References" \
  "channels/email/adapter.py" \
  "('        chain = _msgids(msg.get(\"References\")) or _msgids(msg.get(\"In-Reply-To\"))', '        chain = _msgids(msg.get(\"In-Reply-To\"))')" \
  "test_email_adapter"

run_mutation "email: the thread root becomes the immediate parent" \
  "channels/email/adapter.py" \
  "('        thread = chain[0] if chain else None', '        thread = chain[-1] if chain else None')" \
  "test_email_adapter"

# ENH-26 — deny-by-default at the one funnel every command passes through.
run_mutation "email: the read allowlist stops being enforced" \
  "channels/email/adapter.py" \
  "('        if command not in READ_COMMANDS:', '        if False:')" \
  "test_email_adapter"

# ENH-26 — the two writes IMAP hides inside reads, forced shut at the funnel:
# a fetch without BODY.PEEK marks the operator's unread mail \Seen...
run_mutation "email: a fetch without PEEK reaches the wire" \
  "channels/email/adapter.py" \
  "('            if sub == \"FETCH\" and \"BODY.PEEK[\" not in str(args[-1]).upper():', '            if False:')" \
  "test_email_adapter"

# ...and a writable SELECT lets the server mutate mailbox state.
run_mutation "email: mailboxes are selected writable" \
  "channels/email/adapter.py" \
  "('            kwargs[\"readonly\"] = True', '            kwargs.setdefault(\"readonly\", False)')" \
  "test_email_adapter"

# ENH-26 — a UIDVALIDITY reset reassigns every UID; resuming from the stale number
# silently skips everything below it (the one direction that loses).
run_mutation "email: a UIDVALIDITY reset resumes from the stale uid" \
  "channels/email/adapter.py" \
  "('            if seen_validity == validity:\n                last = seen_uid', '            last = seen_uid')" \
  "test_email_adapter"

# ENH-26 — a cursor the adapter did not mint means state corruption; guessing a window
# from it re-shapes what gets polled without anyone deciding that.
run_mutation "email: a junk cursor is guessed at instead of refused" \
  "channels/email/adapter.py" \
  "('            raise ValueError(\n                f\"email adapter: cursor is not a mailbox->', '            return {}\n            raise ValueError(\n                f\"email adapter: cursor is not a mailbox->')" \
  "test_email_adapter"

# ENH-26 — the snapshot must derive identity through the SAME path as poll(), or
# parity classifies one row as missing AND extra in the same run.
run_mutation "email: the snapshot derives identity its own way" \
  "channels/email/adapter.py" \
  "('                out.add(self._identity(self._fetch_raw(client, uid)))', '                out.add(str(uid))')" \
  "test_email_adapter"

# ENH-26 — a bounded snapshot cannot be answered exactly on Message-IDs; truncating one
# relabels real losses as deletions (the direction that hides a defect).
run_mutation "email: a bounded snapshot is truncated instead of refused" \
  "channels/email/adapter.py" \
  "('        if oldest is not None or latest is not None:', '        if False:')" \
  "test_email_adapter"

# ENH-26 — the doc's email headline: an adopter who assumes an orderable ts misreads
# both the store's keys and parity's verdicts.
run_mutation "docs: email's identity-is-Message-ID limit vanishes" \
  "docs/QUICKSTART.md" \
  "('**identity is the Message-ID string**', '**identity is the arrival timestamp**')" \
  "test_docs"

# ENH-26 — and the stated consequence: parity classifies these ids WITHOUT ordering them.
run_mutation "docs: the non-orderable-identities claim vanishes" \
  "docs/QUICKSTART.md" \
  "('**non-orderable identities**', '**string identities**')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-28 — the write surface: composing stages, only a human click sends. Every
# mutation below removes one half of that gate; a survivor is an agent that can
# post in the operator's name without a human in the loop — the one behaviour
# this feature's design bound forbids outright.
# ---------------------------------------------------------------------------

# ENH-28 — THE gate, outbox side: a compose that reaches the adapter is an auto-send.
run_mutation "outbox: stage() falls through to the adapter (compose auto-sends)" \
  "core/outbox.py" \
  "('        return {\"key\": key, \"receipt\": None, \"state\": STAGED, \"staged\": True,\n                \"deduped\": False}', '        self._write(key, state=INTENT)\n        return self._finish(key, target, text, thread_id, None)')" \
  "test_outbox_write"

# ENH-28 — default deny must hold for composing too: staging to a 'never' channel
# implies someone could later approve a send the policy forbids.
run_mutation "outbox: stage() accepts a never-policy target" \
  "core/outbox.py" \
  "('        if self.policy_for(target, scope) == \"never\":', '        if False:')" \
  "test_outbox_write"

# ENH-28 — the approval must be durable BEFORE the adapter: without the flip, a
# release that crashes mid-send evaporates (recover() ignores STAGED rows) and the
# operator's click silently never happened.
run_mutation "outbox: release() skips the durable STAGED->INTENT flip" \
  "core/outbox.py" \
  "('        if not self._claim(key, STAGED, INTENT):', '        if False:')" \
  "test_outbox_write"

# ENH-28 — an INTENT/SENT row belongs to recover(); a second live delivery from the
# gate is the double-message bug entered by click.
run_mutation "outbox: release() re-sends a row that is already in flight" \
  "core/outbox.py" \
  "('        if row[\"state\"] in (INTENT, SENT):', '        if False:')" \
  "test_outbox_write"

# ENH-28 — a double-click must dedupe against the recorded delivery, never re-send.
run_mutation "outbox: release() delivers an already-delivered draft again" \
  "core/outbox.py" \
  "('        if row[\"state\"] in (VERIFIED, COMMITTED):\n            # Already proven on the target: return the receipt, never re-send.', '        if False:\n            # Already proven on the target: return the receipt, never re-send.')" \
  "test_outbox_write"

# ENH-28/review B1 — the click is a CLAIM: with the state dropped from the WHERE clause
# two Sends landing between the read and the write both reach the adapter (one approved
# draft, two messages), and a Discard interleaved with a Send still sends. DISCARDED being
# terminal is guaranteed by this same clause.
run_mutation "outbox: the state claim is unconditional (two clicks deliver one draft twice)" \
  "core/outbox.py" \
  "('            \"UPDATE outbox SET state=?, updated_at=? WHERE key=? AND state=?\",\n            (to_state, time.time(), key, from_state))', '            \"UPDATE outbox SET state=?, updated_at=? WHERE key=?\",\n            (to_state, time.time(), key))')" \
  "test_outbox_write"

# review B3 — refuse BEFORE the flip when the adapter cannot deliver: flipping first wedges
# the row in INTENT, undiscardable and unrecoverable.
run_mutation "outbox: release() flips a draft whose adapter cannot send" \
  "core/outbox.py" \
  "('        if not _can_send(self.adapter):', '        if False:')" \
  "test_outbox_write"

# ENH-28 — policy is re-resolved at the CLICK: a channel demoted to 'never' since
# staging must refuse, however long the draft sat at the gate.
run_mutation "outbox: release() honours the policy from staging time, not the click" \
  "core/outbox.py" \
  "('        if self.policy_for(row[\"target\"], row[\"scope\"]) == \"never\":', '        if False:')" \
  "test_outbox_write"

# ENH-28 — discard() must refuse anything that is not a draft: 'discarding' a
# delivered row misrecords what is already on the platform.
run_mutation "outbox: discard() skips the compare-and-swap (records nothing)" \
  "core/outbox.py" \
  "('        if not self._claim(key, STAGED, DISCARDED):', '        if False:')" \
  "test_outbox_write"

# ENH-28 — THE gate, shell side: the write surface must not exist for a read-only
# operator. Caught statically (the import's guard stops being the gate) and at
# runtime (the send layer appears in sys.modules with the gate off).
run_mutation "dashboard.py (shell): the write surface renders with the gate off" \
  "scripts/dashboard.py" \
  "('if WRITE_ENABLED:\n    sys.path.insert', 'if True:\n    sys.path.insert')" \
  "test_dashboard_write"

# ENH-28 — the default is READ-ONLY; a default-true gate turns every existing
# deployment into a write surface on upgrade.
run_mutation "dashboard.py (shell): the write gate defaults to enabled" \
  "scripts/dashboard.py" \
  "('os.environ.get(\"COMMS_UI_WRITE_ENABLED\", \"false\")', 'os.environ.get(\"COMMS_UI_WRITE_ENABLED\", \"true\")')" \
  "test_dashboard_write"

# ENH-28 — a junk gate value silently resolving to read-only is the ENH-17 inert-key
# defect on an env var: the operator who typed 'ture' believes writes are on.
run_mutation "dashboard.py (shell): a junk gate value is silently accepted" \
  "scripts/dashboard.py" \
  "('if _gate_raw not in (\"true\", \"false\", \"\"):', 'if False:')" \
  "test_dashboard_write"

# ENH-28 — the UI's compose handler must stage, never send: this is the same gate as
# the outbox mutation above, removed at the caller boundary instead (the seam where
# the R22 cues and the ENH-9 signal once died).
run_mutation "dashboard_write: the compose form sends instead of staging" \
  "scripts/dashboard_write.py" \
  "('        res = outbox.stage(ch.id, trigger, text, thread_id=thread)', '        res = outbox.stage(ch.id, trigger, text, thread_id=thread); res = _outbox_for(cfg, inst, _adapter_for(cfg, inst)).release(res[\"key\"])')" \
  "test_dashboard_write"

# ENH-28 — rendering reads, actions write: a missing outbox is the normal fresh
# state and must read as no rows, not crash the surface (or worse, mint the file).
run_mutation "dashboard_write: a fresh outbox is refused instead of read as empty" \
  "scripts/dashboard_write.py" \
  "('    if not Path(outbox_path).is_file():\n        return []', '    if False:\n        return []')" \
  "test_dashboard_write"

# ---------------------------------------------------------------------------
# ENH-29 — the CONFIG write surface: an edit stages an exact settings diff, only a
# human click on that diff writes the file. Every mutation below removes one half of
# that gate; a survivor is an agent that can rewrite reply policies — the rules that
# decide where the engine may post in the operator's name — without a human in the
# loop, or a UI that leaks the secret it promised never to hold.
# ---------------------------------------------------------------------------

# ENH-29 — THE gate, data side: a stage that writes the file is an auto-apply.
run_mutation "reconfig: stage() writes the settings file (config auto-applies)" \
  "core/reconfig.py" \
  "('        return {\"key\": key, \"state\": STAGED, \"staged\": True, \"deduped\": False,', '        self.apply(key)\n        return {\"key\": key, \"state\": STAGED, \"staged\": True, \"deduped\": False,')" \
  "test_reconfig"

# ENH-29 — the engine's own loader is the validator; skipping it stages exactly the
# candidates the adopter would meet as a startup refusal (the planted top-level
# taxonomy, the misspelled adapter).
run_mutation "reconfig: candidates stop being validated at stage time" \
  "core/reconfig.py" \
  "('        self._validate(new_raw)', '        pass')" \
  "test_reconfig"

# ENH-29 — the ENH-28 rule on config: the environment is re-checked when the human
# ACTS. Unchecked, an env var that vanished since staging writes a half-configured
# file the engine refuses at its next startup — the worst time to learn it.
run_mutation "reconfig: the click stops re-validating against the current environment" \
  "core/reconfig.py" \
  "('        self._validate(json.loads(row[\"new_text\"]))', '        pass')" \
  "test_reconfig"

# ENH-29 — a diff staged against yesterday's file describes nothing; applying it
# silently destroys whatever was edited since.
run_mutation "reconfig: the stale-file check is skipped at the click" \
  "core/reconfig.py" \
  "('        if current != row[\"old_text\"]:', '        if False:')" \
  "test_reconfig"

# ENH-29 — DISCARDED is terminal: a refused diff that can still be applied makes the
# human 'no' advisory (the outbox's discarded-draft rule, on config).
run_mutation "reconfig: a DISCARDED diff can still be applied" \
  "core/reconfig.py" \
  "('        if row[\"state\"] == DISCARDED:', '        if False:')" \
  "test_reconfig"

# ENH-29 — 'discarding' an applied diff rewrites the record of a change that is on
# the file.
run_mutation "reconfig: an applied diff can be discarded (the record rewritten)" \
  "core/reconfig.py" \
  "('        if row[\"state\"] != STAGED:', '        if False:')" \
  "test_reconfig"

# ENH-29 — THE widening property: a policy rising toward 'direct' must be detected,
# or the loud flag the surface promises is rendered from an always-empty list.
run_mutation "reconfig: widening detection goes inert" \
  "core/reconfig.py" \
  "('        if _POLICY_RANK.get(after, 0) > _POLICY_RANK.get(before, 0):', '        if False:')" \
  "test_reconfig"

# ENH-29 — deny-by-omission: a UI-created channel with no stated policy must carry NO
# policy key, so the loader's own DEFAULT DENY is the single copy of that rule.
run_mutation "reconfig: a new channel defaults to direct instead of omitted-deny" \
  "core/reconfig.py" \
  "('    if reply_policy is not None:\n        ch[\"reply_policy\"] = reply_policy', '    ch[\"reply_policy\"] = reply_policy or \"direct\"')" \
  "test_reconfig"

# ENH-29 — the env-NAME guard: with it gone, a pasted token walks through the ops
# into the candidate, and the loader's refusal echoes a prefix of it to the surface.
run_mutation "reconfig: the pasted-secret guard admits any string as an env name" \
  "core/reconfig.py" \
  "('    if isinstance(var, str) and _ENV_NAME.fullmatch(var):', '    if isinstance(var, str):')" \
  "test_reconfig"

# ENH-29 — the sweep's env:-prefix refusal is the other half: an identifier-shaped
# literal slips the NAME check, so removing THIS check must fail on its own.
run_mutation "reconfig: the candidate sweep stops refusing literal auth values" \
  "core/reconfig.py" \
  "('            if not (isinstance(value, str) and value.startswith(\"env:\")):', '            if False:')" \
  "test_reconfig"

# ENH-29 — the reload truth: 'applied' without the restart caveat reads as 'live
# everywhere', which is false for a running scheduler/watcher (no hot-reload exists).
run_mutation "reconfig: RELOAD_TRUTH stops stating the restart requirement" \
  "core/reconfig.py" \
  "('loads settings only at startup: restart it', 'applies live: nothing')" \
  "test_reconfig"

# ENH-29 — THE gate, shell side: the config surface must not exist for a read-only
# operator. Dedented out of the WRITE_ENABLED branch it loads (or crashes) with the
# gate off; the AST placement check catches it with no streamlit installed.
run_mutation "dashboard.py (shell): the config surface escapes the gate branch" \
  "scripts/dashboard.py" \
  "('    import dashboard_config  # noqa: E402\n    dashboard_config.render(cfg, cfg_path)', 'import dashboard_config  # noqa: E402\ndashboard_config.render(cfg, cfg_path)')" \
  "test_dashboard_config"

# ENH-29 — THE gate, UI side: a form submit that also applies is the click gate
# deleted at the caller boundary (the seam where the R22 cues and the ENH-9 signal
# once died).
run_mutation "dashboard_config: an edit applies without staging (the UI click gate deleted)" \
  "scripts/dashboard_config.py" \
  "('            res = stage.stage(candidate, summary=summary)', '            res = stage.stage(candidate, summary=summary)\n            if not res.get(\"deduped\"):\n                stage.apply(res[\"key\"])')" \
  "test_dashboard_config"

# ENH-29 — the widening flag must reach the card the human clicks; silent, the
# promised loud review point never happens.
run_mutation "dashboard_config: the widening banner goes silent" \
  "scripts/dashboard_config.py" \
  "('    if wide:', '    if False:')" \
  "test_dashboard_config"

# ENH-29 — set/NOT-SET is the whole disclosure: one .get() turns the indicator into
# an echo of the secret it promised never to show.
run_mutation "dashboard_config: the env indicator echoes the value" \
  "scripts/dashboard_config.py" \
  "('    return \"set\" if name in os.environ else \"NOT SET\"', '    return os.environ.get(name, \"NOT SET\")')" \
  "test_dashboard_config"

# ENH-29 — rendering reads, actions write: a missing stage db is the normal fresh
# state and must read as no rows, not crash the surface.
run_mutation "dashboard_config: a fresh stage db is refused instead of read as empty" \
  "scripts/dashboard_config.py" \
  "('    if not Path(db_path).is_file():\n        return []', '    if False:\n        return []')" \
  "test_dashboard_config"

# ENH-29/R21 — the doc's reload truth: an operator taught 'applied' without the
# restart caveat debugs a live loop that is still obeying the old policy.
run_mutation "docs: the config reload truth vanishes from the quickstart" \
  "docs/QUICKSTART.md" \
  "('loads settings only at startup and\nmust be restarted', 'picks the change up live and\nneeds no restart')" \
  "test_docs"

# ENH-29/R21 — the never-by-omission claim is what makes UI-created channels safe to
# create; vague, an operator assumes a new channel can already post.
run_mutation "docs: the never-by-omission claim vanishes from the quickstart" \
  "docs/QUICKSTART.md" \
  "('\`reply_policy: never\` (deny) **by omission**', '\`reply_policy: never\` (deny)')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-30 — in-thread answers close open asks. Measured on the aws-do-agent integration
# (2026-10-12): responded_at had NO production caller and the Slack adapter never fetched
# a thread reply, so every answered ask read UNANSWERED forever and every reply was an
# unlisted message. Each mutation below removes one piece of the fix; a survivor is a
# dashboard that lies about what still needs a human.
# ---------------------------------------------------------------------------

# ENH-30 — UNANSWERED is the open ASK backlog: list every row again (join notices,
# remarks) and the operator is trained to ignore the list.
run_mutation "dashboard: unanswered lists every journal row, not only asks" \
  "core/dashboard.py" \
  "('        ask_filter = (\"WHERE responded_at IS NULL AND (kind IS NULL OR kind IN (\"\n                      + \",\".join(\"?\" * len(ask_kinds)) + \")) \")', '        ask_filter = \"WHERE responded_at IS NULL AND (kind IS NULL OR kind IN (\" + \",\".join(\"?\" * len(ask_kinds)) + \") OR 1=1) \"')" \
  "test_dashboard"

# ENH-30 — a row with no recorded kind must be SURFACED, not hidden: unknown is not
# "not an ask".
run_mutation "dashboard: an unclassified row is hidden from the queue" \
  "core/dashboard.py" \
  "('WHERE responded_at IS NULL AND (kind IS NULL OR kind IN (', 'WHERE responded_at IS NULL AND (kind IN (')" \
  "test_dashboard"

# ENH-30 — the asker bumping their own thread is not an answer.
run_mutation "threads: the asker's own follow-up counts as the answer" \
  "core/threads.py" \
  "('        if not sender or sender == asker or sender in UNATTRIBUTED_SENDERS:', '        if not sender or sender in UNATTRIBUTED_SENDERS:')" \
  "test_threads"

# ENH-30 — an adapter without replies() must read as 'cannot see answers', never as
# 'no answers' — the F-2 silent-no-op shape.
run_mutation "threads: an adapter without replies() reads as 'nothing new'" \
  "core/threads.py" \
  "('    if not hasattr(adapter, \"replies\"):\n        return None', '    if not hasattr(adapter, \"replies\"):\n        return {\"swept\": 0, \"replies\": [], \"answered\": [], \"truncated\": False, \"rate_limited\": None}')" \
  "test_threads"

# ENH-30 — STATEMENTs are never swept: one platform call per remark, and remarks would be
# marked 'answered'.
run_mutation "threads: STATEMENTs are swept like asks" \
  "core/threads.py" \
  "('    rows = [r for r in journal.unanswered(channel) if r[\"kind\"] in ACTIONABLE_KINDS]', '    rows = [r for r in journal.unanswered(channel)]')" \
  "test_threads"

# ENH-30 — the sweep is bounded and SAYS when it truncated.
run_mutation "threads: a truncated sweep reports itself complete" \
  "core/threads.py" \
  "('    result[\"truncated\"] = len(open_asks) > len(batch)', '    result[\"truncated\"] = False')" \
  "test_threads"

# ENH-30 — a 429 must be visible in the result, never swallowed.
run_mutation "threads: a rate limit is swallowed silently" \
  "core/threads.py" \
  "('            result[\"rate_limited\"] = ex.method or \"replies\"\n            reached = min(pos[str(r[\"ts\"])] for r in rows)    # this root was not fetched\n            break', '            break')" \
  "test_threads"

# ENH-30 — the tie itself: without mark_responded the ask stays open forever.
run_mutation "threads: a foreign reply never marks the ask responded" \
  "core/threads.py" \
  "('                journal.mark_responded(channel, row[\"ts\"], f\"thread-reply:{hit[\\'ts\\']}\",\n                                       at=arrivals.get(str(hit[\"ts\"])))', '                pass')" \
  "test_threads"

# ENH-30 — the scheduler must actually run the sweep each cycle.
run_mutation "schedule: the cycle skips the thread sweep" \
  "core/schedule.py" \
  "('                swept = threads.sweep(', '                swept = None and threads.sweep(')" \
  "test_schedule"

# ENH-30 — the Slack adapter must drop the parent (it is the ask, not an answer).
run_mutation "slack: replies() returns the parent as a reply" \
  "channels/slack/adapter.py" \
  "('        out = [self._normalize(channel, m) for m in raw if m.get(\"ts\") != thread_ts]', '        out = [self._normalize(channel, m) for m in raw]')" \
  "test_slack_adapter"

# ENH-30 — a truncated thread page must raise, never drop the tail.
run_mutation "slack: replies() drops the rest of a truncated thread" \
  "channels/slack/adapter.py" \
  "('                raise ApiError(\"conversations.replies\",', '                break\n                raise ApiError(\"conversations.replies\",')" \
  "test_slack_adapter"

# ENH-30 — the fake adapter's replies() must honour the exclusive floor, or every sweep
# re-fetches and re-stores the whole thread.
run_mutation "fake: replies() ignores oldest" \
  "channels/fake/adapter.py" \
  "('               and m.get(\"thread_id\") == thread_ts and float(m[\"ts\"]) > floor]', '               and m.get(\"thread_id\") == thread_ts]')" \
  "test_threads"

# ENH-30 — the write surface: a PROVEN send from Reply must tie the ask.
run_mutation "dashboard_write: a sent reply never marks the ask answered" \
  "scripts/dashboard_write.py" \
  "('        journal.mark_responded(row[\"target\"], row[\"trigger_ts\"], key,\n                               at=row[\"created_at\"] if \"created_at\" in row.keys() else None)', '        pass')" \
  "test_dashboard_write"

# ENH-30 — STAGING must not tie: a draft is a promise, not an answer.
run_mutation "dashboard_write: staging a reply already marks the ask answered" \
  "scripts/dashboard_write.py" \
  "('    if (reply_to is not None and res.get(\"deduped\") and res.get(\"receipt\")\n            and res[\"state\"] in (COMMITTED, VERIFIED)):', '    if reply_to is not None:')" \
  "test_dashboard_write"

# ENH-30 — Reply must only be offered where composing is allowed (deny-by-default).
run_mutation "dashboard_write: Reply offered on a never-policy channel" \
  "scripts/dashboard_write.py" \
  "('            if a[\"severity\"] == \"unanswered\" and a[\"where\"] in replyable]', '            if a[\"severity\"] == \"unanswered\"]')" \
  "test_dashboard_write"

# ENH-30/R21 — the docs must keep saying what 'unanswered' means and how it closes.
run_mutation "docs: the answered-state explanation vanishes from the quickstart" \
  "docs/QUICKSTART.md" \
  "('A top-level reply posted in the channel by someone else cannot be attributed to the\nask it answers, so it leaves the ask open — answer in the thread, or from this surface\\'s\nReply button', 'Replies close asks.')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-30 follow-up — the independent reviews of 89fd0fa (comms lanes + the merge
# reviewer, 2026-10-13). Each mutation below removes one property those reviews found
# missing or watched fail; a survivor is a review finding with no test behind it.
# ---------------------------------------------------------------------------

# A deleted ask answers thread_not_found forever; escaping made a crash loop, swallowing
# is the F-2 silent no-op. Recorded BY ASK or not at all.
run_mutation "threads: a platform error on one thread is swallowed" \
  "core/threads.py" \
  "('            result[\"errors\"].extend((str(r[\"ts\"]), f\"{type(ex).__name__}: {ex}\")\n                                    for r in rows)', '            pass')" \
  "test_threads"

# The inverted dual-write: a store-derived floor skips a reply that is stored but not
# yet journaled, and a broadcast bump hides an earlier real answer.
run_mutation "threads: the fetch floor is the newest stored reply, not the ask" \
  "core/threads.py" \
  "('        oldest = min((str(r[\"ts\"]) for r in rows), key=ts_order)', '        oldest = max([str(r[\"ts\"]) for r in rows] + [str(m[\"ts\"]) for m in store.thread_replies(channel, root)], key=ts_order)')" \
  "test_threads"

# Only replies AFTER the ask can answer it.
run_mutation "threads: a reply older than the ask answers it" \
  "core/threads.py" \
  "('            later = [m for m in in_thread if ts_order(m[\"ts\"]) > ts_order(row[\"ts\"])]', '            later = list(in_thread)')" \
  "test_threads"

# SWEEP_MAX is a bound only if the default the scheduler relies on honours it.
run_mutation "threads: the default sweep is unbounded" \
  "core/threads.py" \
  "('          max_threads: int = SWEEP_MAX, start: int = 0, is_answer=None,\n          answerers=()) -> dict | None:', '          max_threads: int = 10**9, start: int = 0, is_answer=None,\n          answerers=()) -> dict | None:')" \
  "test_threads"

# The sweep's refusals must reach the operator (G4): a dead thread, a 429, a truncation.
run_mutation "schedule: sweep errors never reach the cycle summary" \
  "core/schedule.py" \
  "('                    sweep_errors.extend((ch, ts, msg) for ts, msg in swept[\"errors\"])', '                    pass')" \
  "test_schedule"

run_mutation "schedule: a sweep 429 prints the same cycle line as a healthy sweep" \
  "core/schedule.py" \
  "('                    if swept[\"rate_limited\"]:\n                        sweep_rate_limited.append((ch, swept[\"rate_limited\"]))', '                    if False:\n                        sweep_rate_limited.append((ch, swept[\"rate_limited\"]))')" \
  "test_schedule"

# A third party's \"+1\" closes an ask too; the surface must keep that countable apart.
run_mutation "dashboard: thread-closed asks are counted as answers sent from here" \
  "core/dashboard.py" \
  "('                \"AND response_key LIKE \\'thread-reply:%\\'\"', '                \"AND 0\"')" \
  "test_dashboard"

# A blank line ends a CommonMark HTML block and re-arms markdown for untrusted text.
run_mutation "dashboard: html_text keeps blank lines (the HTML block ends early)" \
  "core/dashboard.py" \
  "('    return html.escape(text or \"\").replace(\"\\\\r\", \"\").replace(\"\\\\n\", \"<br>\")', '    return html.escape(text or \"\")')" \
  "test_dashboard"

# review B2 — the click must act on the config as it is NOW, never the render-time copy.
run_mutation "dashboard_write: the click gets all-direct policies (render-time config)" \
  "scripts/dashboard_write.py" \
  "('        outbox = _outbox_for(cfg, inst, _adapter_for(cfg, inst))\n        res = outbox.release(key)', '        outbox = Outbox(cfg.outbox_path_for(inst.name), _adapter_for(cfg, inst), {ch.id: \"direct\" for ch in inst.channels})\n        res = outbox.release(key)')" \
  "test_dashboard_write"

# review B3 — Send is offered only where the adapter can deliver.
run_mutation "dashboard_write: Send offered on a draft whose adapter cannot send" \
  "scripts/dashboard_write.py" \
  "('            if no_send is None:\n                approve.button(', '            if True:\n                approve.button(')" \
  "test_dashboard_write"

# review S5 — Discard needs no adapter; building one crashes on a half-configured connection.
run_mutation "dashboard_write: Discard builds the adapter it never needs" \
  "scripts/dashboard_write.py" \
  "('        outbox = _outbox_for(cfg, inst, _NoAdapter())\n        outbox.discard(key)', '        outbox = _outbox_for(cfg, inst, _adapter_for(cfg, inst))\n        outbox.discard(key)')" \
  "test_dashboard_write"

# review F2 — a journal refusal after a PROVEN send is a warning with a retry, never a crash.
run_mutation "dashboard_write: a journal refusal after a proven send crashes the page" \
  "scripts/dashboard_write.py" \
  "('    except sqlite3.Error as ex:', '    except () as ex:')" \
  "test_dashboard_write"

# review F4 — a reply to an in-thread ask threads under the ROOT the sweep reads.
run_mutation "dashboard_write: a reply threads under the ask instead of the thread root" \
  "scripts/dashboard_write.py" \
  "('            thread = _thread_root(cfg, ch.id, trigger) if placement == \"thread\" else None', '            thread = trigger if placement == \"thread\" else None')" \
  "test_dashboard_write"

# review F5 — a thread-only channel is answerable through Reply.
run_mutation "dashboard_write: a thread-only channel offers no Reply" \
  "scripts/dashboard_write.py" \
  "('            if cpol == \"never\" and tpol == \"never\":\n                continue', '            if cpol == \"never\":\n                continue')" \
  "test_dashboard_write"

# review F6/R21 — the limits entry names the stranded adapters from the code, not from memory.
run_mutation "docs: the stranded-adapters list drifts from the shipped tree" \
  "docs/QUICKSTART.md" \
  "('\`slack_socket\`, \`telegram\` and \`email\`', '\`telegram\` and \`email\`')" \
  "test_docs"

# ---------------------------------------------------------------------------
# ENH-30 follow-up, round 3 — the review lanes' second pass over the fixed tree
# (edit laundering, tie-only-on-proof, starvation, budget, "+1" replies, eight unpinned
# UI properties). Each mutation removes one property; a survivor is a finding with no
# test behind it.
# ---------------------------------------------------------------------------

# review F9 (blocker) — a late tie stamped at send time launders an edit made while the
# draft waited at the gate.
run_mutation "dashboard_write: the tie is stamped at send time, laundering an edit" \
  "scripts/dashboard_write.py" \
  "('                               at=row[\"created_at\"] if \"created_at\" in row.keys() else None)', '                               at=None)')" \
  "test_dashboard_write"

run_mutation "journal: mark_responded ignores the time the answer was written" \
  "core/journal.py" \
  "('            (time.time() if at is None else float(at), response_key, channel, str(ts)))', '            (time.time(), response_key, channel, str(ts)))')" \
  "test_journal"

run_mutation "threads: the thread tie is stamped at sweep time, not the reply's arrival" \
  "core/threads.py" \
  "('                                       at=arrivals.get(str(hit[\"ts\"])))', '                                       at=None)')" \
  "test_threads"

# review F8-A — tie ONLY on proof: an in-flight (unproven) release must never tie.
run_mutation "dashboard_write: an in-flight release is tied as if proven" \
  "scripts/dashboard_write.py" \
  "('        if res.get(\"in_flight\"):\n            # Not proven', '        if False:\n            # Not proven')" \
  "test_dashboard_write"

# review F8-B — the first proven answer keeps the tie.
run_mutation "dashboard_write: a later send overwrites the first answer's tie" \
  "scripts/dashboard_write.py" \
  "('        if ask is None or ask[\"responded_at\"] is not None:', '        if ask is None:')" \
  "test_dashboard_write"

# review F8-F — a Send with no journal must not mint journal.db.
run_mutation "dashboard_write: a Send with no journal mints journal.db" \
  "scripts/dashboard_write.py" \
  "('    if row is None or not Path(cfg.journal_path).is_file():', '    if row is None:')" \
  "test_dashboard_write"

# review F8-H — cancel actually cancels.
run_mutation "dashboard_write: cancel reply does nothing" \
  "scripts/dashboard_write.py" \
  "('def _cancel_reply_clicked():\n    st.session_state.pop(\"reply_to\", None)', 'def _cancel_reply_clicked():\n    pass')" \
  "test_dashboard_write"

# review F8-E — the placement is stated truthfully, in flash and on the card.
run_mutation "dashboard_write: every reply is described as in-thread" \
  "scripts/dashboard_write.py" \
  "('                 (f\" as a reply to ts {trigger}, in its thread\" if thread', '                 (f\" as a reply to ts {trigger}, in its thread\" if True')" \
  "test_dashboard_write"

# review N2 — the banner must not promise to record an ask that is already closed.
run_mutation "dashboard_write: the reply banner outlives an ask answered elsewhere" \
  "scripts/dashboard_write.py" \
  "('    if reply_to is not None and (reply_to[\"channel\"], reply_to[\"ts\"]) not in {', '    if False and (reply_to[\"channel\"], reply_to[\"ts\"]) not in {')" \
  "test_dashboard_write"

# review F7 — a follow-up question is a new ask, never the answer; an unattributed
# placeholder sender answers nothing.
run_mutation "threads: a reply that is itself an ask is credited as the answer" \
  "core/threads.py" \
  "('        if is_answer is not None and not is_answer(r):\n            continue', '        pass')" \
  "test_threads"

run_mutation "threads: the unattributed placeholder sender answers an ask" \
  "core/threads.py" \
  "('        if not sender or sender == asker or sender in UNATTRIBUTED_SENDERS:', '        if not sender or sender == asker:')" \
  "test_threads"

run_mutation "schedule: the cycle sweeps without the classifier's verdict" \
  "core/schedule.py" \
  "('                    is_answer=lambda m, _src=src: classify(\n                        m.get(\"text\") or \"\", _src.taxonomy,\n                        attachments=m.get(\"attachments\")).kind not in ACTIONABLE_KINDS)', '                    is_answer=None)')" \
  "test_schedule"

# review F8 — one platform call per thread root, one budget per adapter, 429 stops the source.
run_mutation "threads: every ask under one root issues its own platform call" \
  "core/threads.py" \
  "('        by_root.setdefault(store.thread_root(channel, row[\"ts\"]), []).append(row)', '        by_root.setdefault((store.thread_root(channel, row[\"ts\"]), row[\"ts\"]), []).append(row)')" \
  "test_threads"

run_mutation "schedule: the sweep budget is per channel, re-spent for every channel" \
  "core/schedule.py" \
  "('                    budget -= swept[\"calls\"]', '                    pass')" \
  "test_schedule"

run_mutation "schedule: a sweep 429 does not stop the rest of that source's sweeps" \
  "core/schedule.py" \
  "('                        budget = 0          # the platform said stop; stop for this source', '                        pass')" \
  "test_schedule"

# review F9 — the window must rotate, or asks beyond the newest few starve.
run_mutation "threads: the window never rotates (newest-first forever)" \
  "core/threads.py" \
  "('    start = start % len(open_asks)\n    rotated = open_asks[start:] + open_asks[:start]', '    start = 0\n    rotated = open_asks')" \
  "test_threads"

run_mutation "schedule: the scheduler forgets where the sweep window stopped" \
  "core/schedule.py" \
  "('                    self.store.sweep_start_set(src.name, ch, swept[\"next_start\"])', '                    pass')" \
  "test_schedule"

# review F12 — a non-orderable id space is refused, never guessed.
run_mutation "threads: a non-orderable id space is swept as if chronological" \
  "core/threads.py" \
  "('        if ts_order(row[\"ts\"])[0] != 0:', '        if False:')" \
  "test_threads"

# review F11 — the sweep's lookups are indexed.
run_mutation "store: the thread lookups lose their indexes" \
  "core/store.py" \
  "('        self.conn.execute(\"CREATE INDEX IF NOT EXISTS messages_channel_thread \"\n                          \"ON messages (channel_id, thread_id)\")', '        pass')" \
  "test_store"

# review D2 — one call reserved per waiting channel, the bulk position rotates, a skipped
# channel is swept first next cycle, and every skip is named.
run_mutation "schedule: no call is reserved for the channels still waiting (a backlog takes the whole budget)" \
  "core/schedule.py" \
  "('                allow = max(1, budget - (len(order) - 1 - i))', '                allow = budget')" \
  "test_schedule"

run_mutation "schedule: the bulk position never rotates (one backlog channel keeps it forever)" \
  "core/schedule.py" \
  "('            bulk = max(swept_now, key=lambda c: spent.get(c, 0)) if swept_now else None', '            bulk = None')" \
  "test_schedule"

run_mutation "schedule: a channel the cycle skipped is not swept first next cycle" \
  "core/schedule.py" \
  "('            new_order = skipped_now + rest', '            new_order = rest + skipped_now')" \
  "test_schedule"

run_mutation "schedule: a channel the cycle skipped is not named in the summary" \
  "core/schedule.py" \
  "('                    if threads.open_asks_for(self.journal, ch):\n                        sweep_skipped.append((ch, stopped or \"budget\"))', '                    pass')" \
  "test_schedule"

# round 2 (independent review lane on 2851289): F1 durable order, F2 charge by attempt,
# F3 bulk by spend, F4 skip causes + the report lines.
run_mutation "schedule: the sweep order is not persisted (every cron run restarts in config order)" \
  "core/schedule.py" \
  "('            self.store.sweep_order_set(src.name, new_order)', '            pass')" \
  "test_schedule"

run_mutation "schedule: refused fetches are not charged (an all-errors channel sweeps for free)" \
  "core/schedule.py" \
  "('                    budget -= swept[\"calls\"]', '                    budget -= swept[\"swept\"]')" \
  "test_schedule"

run_mutation "schedule: the bulk is taken to be position 0, not the channel that spent the most" \
  "core/schedule.py" \
  "('            bulk = max(swept_now, key=lambda c: spent.get(c, 0)) if swept_now else None', '            bulk = swept_now[0] if swept_now else None')" \
  "test_schedule"

run_mutation "schedule: a skip after a 429 is blamed on the budget" \
  "core/schedule.py" \
  "('                        sweep_skipped.append((ch, stopped or \"budget\"))', '                        sweep_skipped.append((ch, \"budget\"))')" \
  "test_schedule"

run_mutation "schedule: a channel with no open ask is named SKIPPED" \
  "core/schedule.py" \
  "('                    if threads.open_asks_for(self.journal, ch):', '                    if True:')" \
  "test_schedule"

# round 2 delta review: R2-1 the window advances only as far as the loop got; R2-2 the
# window commits after the fetched replies are journaled.
run_mutation "threads: a 429 mid-batch advances the window past the asks never fetched" \
  "core/threads.py" \
  "('            reached = min(pos[str(r[\"ts\"])] for r in rows)    # this root was not fetched', '            pass')" \
  "test_schedule"

run_mutation "schedule: the sweep window commits before the fetched replies are journaled" \
  "core/schedule.py" \
  "('                    fresh += self._journal_and_route(ch, swept[\"replies\"], src)\n                    self.store.sweep_start_set(src.name, ch, swept[\"next_start\"])', '                    self.store.sweep_start_set(src.name, ch, swept[\"next_start\"])\n                    fresh += self._journal_and_route(ch, swept[\"replies\"], src)')" \
  "test_schedule"

# round 2 delta review, R2-8/R2-9: the durable order under a config edit, and the adapter's name.
run_mutation "schedule: a channel added to the config never joins the saved sweep order" \
  "core/schedule.py" \
  "('                + [c for c in src.channels if c not in saved])', '                + [])')" \
  "test_schedule"

run_mutation "schedule: a channel removed from the config stays in the saved sweep order" \
  "core/schedule.py" \
  "('        return ([c for c in saved if c in src.channels]', '        return (list(saved)')" \
  "test_schedule"

run_mutation "outbox: the refusal names the adapter class, not its channels/ directory" \
  "core/outbox.py" \
  "('    return module[len(prefix):] if module.startswith(prefix) else type(adapter).__name__', '    return type(adapter).__name__')" \
  "test_dashboard_write"

run_mutation "scheduler: the SKIPPED line is never printed" \
  "scripts/scheduler.py" \
  "('    for ch, why in summary[\"sweep_skipped\"]:', '    for ch, why in []:')" \
  "test_schedule"

run_mutation "scheduler: every SKIPPED line blames the budget" \
  "scripts/scheduler.py" \
  "('                 \"channel\" if why == \"rate-limited\" else', '                 \"channel\" if False else')" \
  "test_schedule"

# review D1 — a thread reply the adapter cannot place is refused BEFORE a row is written or claimed.
run_mutation "outbox: release() flips to INTENT before asking whether the adapter can thread" \
  "core/outbox.py" \
  "('        self._check_placement(row[\"thread_id\"], ReleaseError,\n                              \" — the draft stays STAGED; it can only be discarded\")', '        pass')" \
  "test_outbox_write"

run_mutation "outbox: a direct thread send writes an INTENT row the adapter can never deliver" \
  "core/outbox.py" \
  "('            self._check_placement(thread_id)        # before the INSERT that is the claim', '            pass')" \
  "test_outbox_write"

run_mutation "outbox: stage() accepts a thread draft no click could ever send" \
  "core/outbox.py" \
  "('            self._check_placement(thread_id)        # before the draft exists at all', '            pass')" \
  "test_outbox_write"

# round 2: F5 the declaration counts, F9 replays answered from the row, F6/F7 the surface
# decides placement with the adapter and offers Send only where the draft can be placed.
run_mutation "outbox: the declared thread capability is ignored (a **kwargs send flattens)" \
  "core/outbox.py" \
  "('    if callable(caps) and not caps().get(\"threads\"):', '    if False:')" \
  "test_outbox_write"

run_mutation "outbox: send() refuses a replay of a delivered thread reply" \
  "core/outbox.py" \
  "('        if row is None:\n            self._check_placement(thread_id)        # before the INSERT that is the claim', '        self._check_placement(thread_id)        # before the INSERT that is the claim\n        if row is None:')" \
  "test_outbox_write"

run_mutation "outbox: stage() refuses a replay of a delivered thread reply" \
  "core/outbox.py" \
  "('        if self.get(key) is None:               # a new draft, not a replay (review F9)', '        if True:                                # a new draft, not a replay (review F9)')" \
  "test_outbox_write"

run_mutation "dashboard_write: placement is decided blind to the adapter" \
  "scripts/dashboard_write.py" \
  "('    blocked = thread_block_reason(adapter)\n    if tpol != \"never\" and blocked is None:', '    blocked = None\n    if tpol != \"never\" and blocked is None:')" \
  "test_dashboard_write"

run_mutation "dashboard_write: Send offered on a thread draft the adapter cannot place" \
  "scripts/dashboard_write.py" \
  "('            if no_send is None and row[\"thread_id\"]:', '            if False:')" \
  "test_dashboard_write"

# the two gap mutations that SURVIVED the delta review at 67dd804 (G1, M-ESC-R).
run_mutation "dashboard_write: every Send releases the newest draft" \
  "scripts/dashboard_write.py" \
  "('on_click=_release_clicked, args=(inst.name, row[\"key\"]))', 'on_click=_release_clicked, args=(inst.name, drafts[0][\"key\"]))')" \
  "test_dashboard_write"

run_mutation "dashboard: html_text keeps a bare carriage return (CR CR is a blank line)" \
  "core/dashboard.py" \
  "('    return html.escape(text or \"\").replace(\"\\\\r\", \"\").replace(\"\\\\n\", \"<br>\")', '    return html.escape(text or \"\").replace(\"\\\\n\", \"<br>\")')" \
  "test_dashboard"

# ---- ENH-31 slice C: the loopback-only API base, the payload scan, the config keys ----
run_mutation "slack: a non-loopback COMMS_SLACK_API_BASE is accepted" \
  "channels/slack/adapter.py" \
  "('    if (u.scheme != \"http\" or u.hostname != \"127.0.0.1\"\\n            or not u.port or u.path not in (\"\", \"/\") or u.query or u.fragment\\n            or u.username is not None):', '    if False:')" \
  "test_slack_adapter"

run_mutation "slack_socket: a non-loopback COMMS_SLACK_API_BASE is accepted" \
  "channels/slack_socket/adapter.py" \
  "('    if (u.scheme != \"http\" or u.hostname != \"127.0.0.1\"\\n            or not u.port or u.path not in (\"\", \"/\") or u.query or u.fragment\\n            or u.username is not None):', '    if False:')" \
  "test_socket_adapter"

run_mutation "redact: the payload scan finds nothing" \
  "core/redact.py" \
  "('    return [name for name, pat in BLOCK_PATTERNS if pat.search(text)]', '    return []')" \
  "test_redact"

run_mutation "redact: the private-key class is dropped" \
  "core/redact.py" \
  "('    (\"private-key\", re.compile(\"BEGIN [A-Z ]*PRIV\" + \"ATE KEY\")),', '')" \
  "test_redact"

run_mutation "config: an unknown channel kind is accepted" \
  "core/config.py" \
  "('        if self.kind is not None and self.kind not in VALID_KINDS:', '        if False:')" \
  "test_config"

run_mutation "config: a DM is never inferred from the platform id" \
  "core/config.py" \
  "('        return bool(_SLACK_DM_ID.fullmatch(self.id))', '        return False')" \
  "test_config"

run_mutation "config: answerers accept a bare string (crediting nobody)" \
  "core/config.py" \
  "('        if (not isinstance(answerers, (list, tuple))\n                or not all(isinstance(a, str) and a.strip() for a in answerers)):', '        if False:')" \
  "test_config"

run_mutation "config: an empty send_marker is accepted" \
  "core/config.py" \
  "('        if marker is not None and (not isinstance(marker, str) or not marker.strip()):', '        if False:')" \
  "test_config"

# ---- ENH-31 slice A: the gated send — adapter primitives, the outbox's six guards, the surface ----
run_mutation "slack: send posts without the outbox key in the message metadata" \
  "channels/slack/adapter.py" \
  "('                  \"metadata\": json.dumps({\"event_type\": METADATA_EVENT,\n                                          \"event_payload\": {\"key\": key}})}', '                  }')" \
  "test_slack_adapter"

run_mutation "slack: read_back accepts a message whose text differs" \
  "channels/slack/adapter.py" \
  "('        if _canonical(found.get(\"text\", \"\")) == _canonical(approved):\\n            return True', '        if True:\\n            return True')" \
  "test_slack_adapter"

run_mutation "slack: read_back accepts a message carrying another key" \
  "channels/slack/adapter.py" \
  "('            if found is None or _metadata_key(found) != key:\\n                return False', '            if found is None:\\n                return False')" \
  "test_slack_adapter"

run_mutation "slack: direct sends are declared ON" \
  "channels/slack/adapter.py" \
  "('                \"send\": self._token.startswith(\"xoxb-\"), \"direct\": False,', '                \"send\": self._token.startswith(\"xoxb-\"), \"direct\": True,')" \
  "test_slack_adapter"

run_mutation "slack: newest() returns the oldest reply of a thread" \
  "channels/slack/adapter.py" \
  "('        return self._normalize(channel_id, max(others, key=lambda m: float(m[\"ts\"])))', '        return self._normalize(channel_id, others[0])')" \
  "test_slack_adapter"

run_mutation "outbox: the marker guard is gone" \
  "core/outbox.py" \
  "('        if self.marker and not re.match(', '        if False and not re.match(')" \
  "test_outbox_write"

run_mutation "outbox: the size guard is gone" \
  "core/outbox.py" \
  "('        if limit is not None and len(text) > limit:', '        if False:')" \
  "test_outbox_write"

run_mutation "outbox: the credential scan finds nothing" \
  "core/outbox.py" \
  "('        hits = redact.scan(text)', '        hits = []')" \
  "test_outbox_write"

run_mutation "outbox: a newer message no longer refuses the click" \
  "core/outbox.py" \
  "('        if latest_ts > base:', '        if False:')" \
  "test_outbox_write"

run_mutation "outbox: a direct policy on a direct-off adapter is accepted" \
  "core/outbox.py" \
  "('        if not _allows_direct(adapter):', '        if False:')" \
  "test_outbox_write"

run_mutation "outbox: release() skips the text guards" \
  "core/outbox.py" \
  "('        self._check_payload(row[\"text\"], ReleaseError)', '        pass')" \
  "test_outbox_write"

run_mutation "outbox: a direct send skips the text guards" \
  "core/outbox.py" \
  "('            self._check_placement(thread_id)        # before the INSERT that is the claim\n            self._check_payload(text)               # marker, size, redaction (ENH-31)', '            self._check_placement(thread_id)        # before the INSERT that is the claim')" \
  "test_outbox_write"

run_mutation "outbox: stage() skips the text guards" \
  "core/outbox.py" \
  "('            self._check_placement(thread_id)        # before the draft exists at all\n            self._check_payload(text)               # marker, size, redaction (ENH-31)', '            self._check_placement(thread_id)        # before the draft exists at all')" \
  "test_outbox_write"

run_mutation "outbox: read_back is never told the thread" \
  "core/outbox.py" \
  "('        if thread_id is not None and _accepts_kw(rb, \"thread_id\"):', '        if False:')" \
  "test_outbox_write"

run_mutation "dashboard_write: the connection's marker is not prepended at stage" \
  "scripts/dashboard_write.py" \
  "('        if inst.send_marker and not text.startswith(inst.send_marker):', '        if False:')" \
  "test_dashboard_write"

run_mutation "dashboard_write: a direct message is threaded like a channel" \
  "scripts/dashboard_write.py" \
  "('    if ch.is_dm:', '    if False:')" \
  "test_dashboard_write"

# ---- ENH-31 slice B: the DM answered-state, the shared answerer list, the confirm list ----
run_mutation "threads: the answerer list is ignored (a bystander closes the ask)" \
  "core/threads.py" \
  "('        if restricted and sender not in answerers:', '        if False:')" \
  "test_schedule"

run_mutation "threads: a DM answer closes the OLDEST open ask, not the most recent" \
  "core/threads.py" \
  "('        for a in reversed(preceding):', '        for a in preceding:')" \
  "test_schedule"

run_mutation "threads: the DM rule credits the asker's own follow-up" \
  "core/threads.py" \
  "('            if answered_by(a[\"sender_id\"], [m], is_answer, answerers) is not None:', '            if True:')" \
  "test_schedule"

run_mutation "schedule: the DM rule never runs" \
  "core/schedule.py" \
  "('            for ch in src.dm_channels:', '            for ch in ():')" \
  "test_schedule"

run_mutation "schedule: the sweep ignores the source's answerers" \
  "core/schedule.py" \
  "('                    answerers=src.answerers)', '                    answerers=())')" \
  "test_schedule"

run_mutation "dashboard: asks closed by someone else are not listed" \
  "core/dashboard.py" \
  "('        closed_by_others = j_items.pop(\"closed_by_others\", [])', '        j_items.pop(\"closed_by_others\", [])')" \
  "test_dashboard"

run_mutation "dashboard: a Send from the surface is listed as someone else's closure" \
  "core/dashboard.py" \
  "('                \"WHERE responded_at IS NOT NULL AND (response_key LIKE \\'thread-reply:%\\' \"\n                \"OR response_key LIKE \\'dm-reply:%\\') ORDER BY responded_at DESC LIMIT 50\")]', '                \"WHERE responded_at IS NOT NULL ORDER BY responded_at DESC LIMIT 50\")]')" \
  "test_dashboard"

run_mutation "dashboard: DM closures are not counted" \
  "core/dashboard.py" \
  "('                \"AND response_key LIKE \\'dm-reply:%\\'\"', '                \"AND 0\"')" \
  "test_dashboard"

# ---- ENH-31 review round 1 (F1 one answer one ask; F2 metadata on replies; F3 mismatch;
# F6 scan classes; F7 per-row recovery; F8 canonical labels; the MISMATCH card) ----
run_mutation "threads: a spent DM answer closes the next-older ask on the next cycle" \
  "core/threads.py" \
  "('        if f\"dm-reply:{m[\\'ts\\']}\" in used or _is_engine_post(m):', '        if _is_engine_post(m):')" \
  "test_schedule"

run_mutation "slack: a thread read-back never asks for message metadata" \
  "channels/slack/adapter.py" \
  "('                # recovery posts the reply again (independent review of ENH-31, F2).\\n                params[\"include_all_metadata\"] = \"true\"', '                # recovery posts the reply again (independent review of ENH-31, F2).\\n                pass')" \
  "test_slack_adapter"

run_mutation "slack: a labelled link keeps its URL instead of its label" \
  "channels/slack/adapter.py" \
  "('    t = re.sub(r\"<(?:https?://|mailto:)[^|>]+\\\\|([^>]*)>\", r\"\\\\1\", t)', '')" \
  "test_slack_adapter"

run_mutation "outbox: a mismatched post is not parked (row stays SENT, recovery re-sends)" \
  "core/outbox.py" \
  "('            self._write(key, state=MISMATCH)\n            raise SendBlocked(', '            raise SendBlocked(')" \
  "test_outbox_write"

run_mutation "outbox: one failing row wedges every other row in recovery" \
  "core/outbox.py" \
  "('            except Exception as ex:  # noqa: BLE001 — recorded by row, never a wedge\n                counts[\"errors\"].append((key, f\"{type(ex).__name__}: {ex}\"))', '            except Exception as ex:  # noqa: BLE001 — recorded by row, never a wedge\n                raise')" \
  "test_outbox_write"

run_mutation "redact: a pasted Bearer token passes" \
  "core/redact.py" \
  "('    (\"bearer-token\", re.compile(r\"(?i)\\\\bBearer\\\\s+[A-Za-z0-9+/=_.-]{16,}\")),', '')" \
  "test_redact"

run_mutation "dashboard_write: a MISMATCH row is shown only next to a staged draft" \
  "scripts/dashboard_write.py" \
  "('        if not drafts and not mismatched:', '        if not drafts:')" \
  "test_dashboard_write"

# ---- ENH-31 review round 1, second batch (F1 addendum, F4, F5, F10, N2, N3, N4, N7, X3, X5-X7) ----
run_mutation "threads: an engine delivery is handed to the next older open ask" \
  "core/threads.py" \
  "('        if f\"dm-reply:{m[\\'ts\\']}\" in used or _is_engine_post(m):', '        if f\"dm-reply:{m[\\'ts\\']}\" in used:')" \
  "test_schedule"

run_mutation "threads: an answerer's own ask can never close" \
  "core/threads.py" \
  "('    restricted = bool(answerers) and asker not in answerers', '    restricted = bool(answerers)')" \
  "test_threads"

run_mutation "threads: an answerer's reply is dropped at its own open question" \
  "core/threads.py" \
  "('        for a in reversed(preceding):', '        for a in preceding[-1:]:')" \
  "test_schedule"

run_mutation "threads: the DM rule runs on a non-orderable id space" \
  "core/threads.py" \
  "('    if any(ts_order(a[\"ts\"])[0] != 0 for a in open_asks):', '    if False:')" \
  "test_threads"

run_mutation "schedule: DM asks spend the thread-sweep budget" \
  "core/schedule.py" \
  "('            order = [c for c in self._sweep_order_for(src) if c not in src.dm_channels]', '            order = self._sweep_order_for(src)')" \
  "test_schedule"

run_mutation "store: top_level_after admits thread replies" \
  "core/store.py" \
  "('            \"SELECT * FROM messages WHERE channel_id=? AND thread_id IS NULL \"', '            \"SELECT * FROM messages WHERE channel_id=? \"')" \
  "test_schedule"

run_mutation "scheduler.py: the DM channels are never wired into the Source" \
  "scripts/scheduler.py" \
  "('                   answerers=inst.answerers, dm_channels=inst.dm_channels())', '                   answerers=inst.answerers, dm_channels=())')" \
  "test_schedule"

run_mutation "scheduler.py: the answerers are never wired into the Source" \
  "scripts/scheduler.py" \
  "('                   answerers=inst.answerers, dm_channels=inst.dm_channels())', '                   answerers=(), dm_channels=inst.dm_channels())')" \
  "test_schedule"

run_mutation "dashboard_write: the surface's outbox is built without the marker" \
  "scripts/dashboard_write.py" \
  "('                  marker=inst.send_marker)', '                  marker=None)')" \
  "test_dashboard_write"

run_mutation "dashboard_write: a re-read acknowledgement is reported as a plain dedupe" \
  "scripts/dashboard_write.py" \
  "('    if res.get(\"re_read\"):', '    if False:')" \
  "test_dashboard_write"

run_mutation "dashboard_write: a MISMATCH is flashed as 'SENT, unproven — run recover()'" \
  "scripts/dashboard_write.py" \
  "('        if state == MISMATCH:', '        if False:')" \
  "test_dashboard_write"

run_mutation "outbox: a 429 on the click strands the approved draft in INTENT" \
  "core/outbox.py" \
  "('            self._claim(key, INTENT, STAGED)\\n            raise', '            raise')" \
  "test_outbox_write"

run_mutation "outbox: the seam accepts any policy value that is not never as a send" \
  "core/outbox.py" \
  "('        if policy not in (\"staged\", \"direct\"):', '        if False:')" \
  "test_outbox_write"

run_mutation "outbox: a direct policy sends through an adapter declaring direct off" \
  "core/outbox.py" \
  "('        if policy == \"direct\" and not _allows_direct(self.adapter):', '        if False:')" \
  "test_outbox_write"

run_mutation "outbox: re-staging does not refresh what the human saw" \
  "core/outbox.py" \
  "('(seen_ts, time.time(), key, STAGED))', '(row[\"seen_ts\"], time.time(), key, STAGED))')" \
  "test_outbox_write"

run_mutation "outbox: the click compares with the host clock, not with what the human saw" \
  "core/outbox.py" \
  "('            base = float(baseline) if baseline is not None else float(row[\"created_at\"])', '            base = float(row[\"created_at\"])')" \
  "test_outbox_write"

run_mutation "outbox: the marker is a raw prefix again (glued text and a longer marker pass)" \
  "core/outbox.py" \
  "('re.escape(self.marker) + r\"(\\\\s|\$)\"', 're.escape(self.marker)')" \
  "test_outbox_write"

run_mutation "outbox: an empty thread has no baseline (the host clock judges the first reply)" \
  "core/outbox.py" \
  "('            return None if thread_id is None else str(thread_id)', '            return None')" \
  "test_outbox_write"

run_mutation "ratelimit: a missing instance prints as None in the refusal" \
  "core/ratelimit.py" \
  "('        bits = [str(b) for b in (instance, method) if b]', '        bits = [str(b) for b in (instance, method)]')" \
  "test_outbox_write"

run_mutation "slack: newest() counts the engine's own post as the conversation moving on" \
  "channels/slack/adapter.py" \
  "('            others = [m for m in page.get(\"messages\", []) if _metadata_key(m) is None]', '            others = list(page.get(\"messages\", []))')" \
  "test_slack_adapter"

run_mutation "slack: send is declared for any token (a user token posts in a person's name)" \
  "channels/slack/adapter.py" \
  "('                \"send\": self._token.startswith(\"xoxb-\"), \"direct\": False,', '                \"send\": True, \"direct\": False,')" \
  "test_slack_adapter"

run_mutation "slack: the restart search ignores the draft's age (newest page only)" \
  "channels/slack/adapter.py" \
  "('        elif since is not None:', '        elif False:')" \
  "test_slack_adapter"

run_mutation "redact: the json-secret class is dropped" \
  "core/redact.py" \
  "('    (\"json-secret\", re.compile(\\n        r\"(?i)\\\\\"(token|bot_token|secret|password|api_key|hmac_secret)\\\\\"\\\\s*:\\\\s*\"\\n        r\"\\\\\"[A-Za-z0-9+/=_-]{16,}\\\\\"\")),\\n', '')" \
  "test_redact"

run_mutation "redact: assignments are case-sensitive again" \
  "core/redact.py" \
  "('        r\"(?i)(SECRET|TOKEN|PASSWD|PASSWORD|API_KEY)\\\\s*=\\\\s*[\\\\\"\\']?[A-Za-z0-9+/=_-]{16,}\")),\\n', '        r\"(SECRET|TOKEN|PASSWD|PASSWORD|API_KEY)\\\\s*=\\\\s*[\\\\\"\\']?[A-Za-z0-9+/=_-]{16,}\")),\\n')" \
  "test_redact"

run_mutation "redact: the refresh and client Slack token families are dropped" \
  "core/redact.py" \
  "('    (\"slack-token\", re.compile(\"xo\" + \"x[a-z]-[0-9A-Za-z-]{8,}\")),', '    (\"slack-token\", re.compile(\"xo\" + \"x[abprs]-[0-9A-Za-z-]{8,}\")),')" \
  "test_redact"
run_mutation "docs: step 7 claims the slack adapter has no send method again" \
  "docs/QUICKSTART.md" \
  "('The shipped real adapter is \`slack\`. This step uses its read path only:', 'The shipped real adapter is \`slack\`, and it is read-only at every layer: the class has no send method. This step uses its read path only:')" \
  "test_docs"

run_mutation "docs: the write-surface paragraph counts six pre-flip guards again" \
  "docs/QUICKSTART.md" \
  "('Every Slack send passes five refusals **before** the durable INTENT flip and one proof\\nafter the post,', 'Every Slack send passes six guards, each refusing **before** the durable INTENT flip,')" \
  "test_docs"

echo
echo "mutation_check: caught=$PASS survived/error=$FAIL"
[ "$FAIL" -eq 0 ] && { echo "PASS — every removed property turned the suite red"; exit 0; }
echo "FAIL — a property can be removed without any test noticing" >&2
exit 1
