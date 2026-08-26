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

# R2 — dedupe. Without it, a retry is never answered with the delivered receipt.
run_mutation "outbox: drop the already-delivered dedupe" \
  "core/outbox.py" \
  "('            if row[\"state\"] in (VERIFIED, COMMITTED):', '            if False:')" \
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
  "('                          matched=c.matched)', '                          )')" \
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

# ---------------------------------------------------------------------------
# ENH-18 — the slack adapter is read-only BY AUTHORIZATION (operator granted the
# token read-only, 2026-08-26). Each of these mutations, if it survived, would let a
# write reach the workspace, silently lose polled history, or blind the engine's
# (instance, method) back-off.
# ---------------------------------------------------------------------------

# ENH-18 — THE read-only property: the transport's deny-by-default allowlist. With
# it gone, chat.postMessage is one _api call away, in Anton's name.
run_mutation "slack: transport allowlist deleted (write methods reach the wire)" \
  "channels/slack/adapter.py" \
  "('        if method not in READ_METHODS:', '        if False:')" \
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
  "('        self._pace(target)\n        receipt = self.adapter.send(target, text, key=key)', '        receipt = self.adapter.send(target, text, key=key)')" \
  "test_outbox_pacing"

# ENH-13 — recovery is a burst source too: N undelivered rows for one channel
# re-sent back-to-back would 429 exactly like the live path.
run_mutation "outbox: recovery re-sends a burst unpaced" \
  "core/outbox.py" \
  "('                self._pace(target)\n                receipt = self.adapter.send(target, row[\"text\"], key=key)', '                receipt = self.adapter.send(target, row[\"text\"], key=key)')" \
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

echo
echo "mutation_check: caught=$PASS survived/error=$FAIL"
[ "$FAIL" -eq 0 ] && { echo "PASS — every removed property turned the suite red"; exit 0; }
echo "FAIL — a property can be removed without any test noticing" >&2
exit 1
