# Runbook — when something goes wrong

Every entry below is a failure this engine (or the system it generalizes) has actually
produced. Symptom first, because that is what you have at 2am.

**First move, whatever the symptom:**

```sh
python3 -m core.doctor --config settings.json
```

The doctor preflights the whole configuration in one command — config validated,
credentials resolved, each configured channel confirmed readable by a live read-only
poll, the effective reply policy printed per channel — and it cannot report healthy
vacuously (`core/checks.py` refuses a PASS that inspected nothing). Exit 0 means the
problem is probably further down this page; exit 2 is a config refusal (first table
below); exit 1 names the failing check.

**Second move, before any Python snippet below:** every `journal.` / `outbox.` /
`owed.` / `store.` call on this page assumes objects built from YOUR config. Paste
this first, from the directory holding your `settings.json`, in the same environment
your engine runs under — `load()` refuses a missing `env:` reference, by design:

```python
from core.config import load, load_adapter_class
from core.journal import Journal
from core.outbox import Outbox
from core.owed import OwedRegistry
from core.store import Store

cfg = load("settings.json")
inst = cfg.instances[0]                 # or cfg.instance("my-team-slack")
adapter = load_adapter_class(cfg.channels_dir, inst.adapter)(auth=inst.auth)
journal = Journal(cfg.journal_path)
outbox = Outbox(cfg.outbox_path_for(inst.name), adapter, inst.policies())
owed = OwedRegistry(cfg.state_dir / "owed.db")
store = Store(cfg.store_path)
```

One honesty note: with this construction `owed.unattended()` lists **all** open work —
the default liveness probe counts every driver as dead, which over-reports, the safe
direction at 2am. To honour live drivers, pass the probe your loop actually uses
(the reference scheduler's `pid_alive` in `scripts/scheduler.py`) as
`OwedRegistry(..., driver_alive=...)`.

## "The engine refuses to start"

| Message | Cause | Fix |
|---|---|---|
| `environment variable X is not set` | config references `env:X`, your shell does not have it | export it; do not paste the value into the config |
| `a literal credential was found in configuration` | a real token was pasted into `settings.json` | move it to the environment. Also rotate it — assume anything in a file has leaked |
| `unknown adapter 'slakc'` | typo in `adapter`, or the channel type has no `adapter.py` yet | the error lists what WAS discovered under `channels_dir`; fix the spelling or land the adapter. This fails loudly on purpose: a silently inert instance looks like a quiet channel |
| `no instances configured` | empty `instances` list | the engine would poll nothing, so it refuses |
| `config file not found` / `not valid JSON` | path or syntax | check you copied `settings.example.json` |

## "My first poll returned 0 messages"

`FIRST POLL OK — 0 polled` is a *successful* poll of a quiet source, not a failure.

| Situation | Why | What to do |
|---|---|---|
| fake adapter, no `--seed-demo` | its memory starts empty | re-run with `--seed-demo` |
| second run after `--seed-demo` | the cursor was persisted; nothing is new | working as designed — that is the replay-safety you want |
| messages exist but are filtered out | a polled message's `channel_id` did not match any configured channel | check `instances[].channels[].id` against what the platform actually reports |
| you expected old history | a first cursor starts from the adapter's default window, not from the beginning of time | backfill is an adapter capability (`history`), not a first-poll feature |

To start over from a clean slate, delete the state directory you configured — the cursor
lives in the message store inside it, nowhere else. Pointing `engine.state_dir` somewhere
new works only if your `store` path derives from it: the shipped example pins `store`
explicitly, so moving `state_dir` alone leaves the cursor behind (measured — QUICKSTART
step 6 hit exactly this).

## "It is not sending anything"

**Check the reply policy first.** Deny-by-default means silence is the *designed* behaviour for
any channel you have not explicitly promoted.

```python
outbox.policy_for("C_YOUR_CHANNEL")             # 'never' | 'staged' | 'direct'
outbox.policy_for("C_YOUR_CHANNEL", "thread")   # the SAME channel, inside a thread
```

- `never` → `PolicyError` on send, adapter never called. Working as intended.
- `staged` → look in the outbox for drafts: `outbox.staged()`. A human is meant to gate these.
- `direct` → if it still is not sending, see the next section.

**Ask about the placement that was actually refused.** The one-argument call answers for the
main channel, so a channel configured with `thread_reply_policy` will look read-only while
its threads are perfectly sendable, and vice versa. The `PolicyError` names the scope it
refused; `outbox.staged()` rows carry `scope` and `thread_id`, which is also how you see
where a draft would land before you approve it.

## "A send failed or the process died mid-send"

Run recovery. It is idempotent and safe to run repeatedly:

```python
outbox.recover()   # -> {'resumed': n, 'resent': n, 'already_delivered': n}
```

Recovery resumes from the durable **INTENT** record, asks the target whether the idempotency
key already landed, and re-sends **only** if it did not. `already_delivered` > 0 means a
message was on the target but not recorded — the crash-after-send case. That is handled, not a
problem.

`SendBlocked: read-back could not prove delivery` means the adapter accepted the message but
the engine could not find it afterwards. Do **not** retry blindly; check the platform. An
unverifiable send is treated as a failure by design, because the alternative is claiming
success you cannot prove.

## "A health check says nothing"

It cannot. That is the point of `core/checks.py`:

- a check returning `None`, returning junk, or raising becomes a **FAIL**
- `Verdict.passed()` **refuses** a pass that inspected zero items

If a check reports PASS with `inspected=0`, that is a bug in the check — file it. The
incumbent's watchdog printed "OK — 7 checks passed" for weeks while one check was inert
because it read a field name the events did not carry.

**Judging a log by its age?** Ask first whether that log is written *only* on action. An
action-only log is healthy when silent, and treating its age as staleness produces a false
alarm. Pass `action_only_log=True` to `freshness_check` for those.

## "The audit numbers look wrong"

`journal.row_count()` must equal `journal.distinct_count()`. If they differ, the journal's
idempotence is broken — that is exactly the defect this replaced (an incumbent log with 323
entries for 177 distinct messages). `seen_count` on a row tells you how many times a message
was re-read; that is expected and harmless.

To find what is still owed: `journal.unanswered(channel)`.

## "Work was promised and nothing is happening"

This is the failure that cost 8h17m on a customer request with a deadline.

```python
owed.unattended()     # promised work whose driver is NOT ALIVE
owed.should_fire(backoff_until)   # True whenever unattended work exists
```

A driver *string* is not evidence — liveness is. A note saying "NEXT STEP: …" is inert; only a
live process makes progress. If `unattended()` returns rows, something needs relaunching, and
backoff must not be the reason it is waiting.

If you run the reference loop (`scripts/scheduler.py`), this check happens every cycle with
no inbound message required, and unattended work both restores the base polling cadence and
pages `OPERATOR: DEGRADED: owed-work-unattended` — once per state change, not per cycle.

## "The scheduler refuses to start"

`REFUSED: another scheduler already holds .../scheduler.lock` (exit 3) means exactly what it
says: one loop per state directory. Find the running instance and stop it — **never delete
the lock** to force a second one; two loops on one state directory double-classify, race the
cursor, and (the day a send path is enabled) double-deliver. The lock is held by the process
itself, not by a marker file: a crashed holder releases it automatically, so a refusal always
means a *live* holder exists. Check your supervisor or cron for the overlap — an `--once`
cron fire overlapping a long-running instance is the usual cause, and the refusal (rather
than a race) is the designed outcome.

## "The classifier is wrong on my team's messages"

Expected — you inherited our vocabulary. Check behaviour on your own samples:

```python
from core.classify import classify, Taxonomy
tax = Taxonomy.from_config({"exec_verbs": ["provision"]})
c = classify("Please provision staging", tax)
print(c.kind, "|", c.reason, "|", c.matched)
```

Every classification carries a **reason** and the **matched cues**, so it can be disputed. Bias
is deliberate: an exec verb with no directive is downgraded to `STATEMENT`, because a missed
order costs a follow-up while a false order can start a cluster run.

To dispute a decision that was already journaled, do NOT re-run the classifier — your taxonomy
may have changed since. Ask the journal for the decision as recorded:

```python
journal.audit(channel_id, ts)
# {'kind': 'EXEC-REQUEST', 'reason': 'imperative or directed request to perform work',
#  'matched': ['deploy', 'please'], 'revision': 1}
```

`matched` names the cues that fired; each one occurs verbatim in the message, so the row is
checkable evidence, not narration. `matched` of `None` (as opposed to `[]`) means the row was
journaled before cue recording existed. Per-edit history is in `journal.revisions(channel_id, ts)` —
each revision keeps the cues of *its* classification.

If you retune, add your samples to a corpus test. Vocabulary changes without a test are how a
classifier regresses silently.

## "Are the gates actually running?"

```sh
bash scripts/sanitize-gate.sh --self-test     # healthy: catches all 10 planted secret classes
bash tests/mutation_check.sh                  # healthy: "every removed property turned the suite red"
ls .git/hooks/pre-commit .git/hooks/pre-push  # healthy: both paths print
```

A `No such file or directory` from that last line means a gate is not installed — run
`scripts/install-hooks.sh`. Do **not** judge the hooks with `git config core.hooksPath`:
on a correctly-installed clone it prints *nothing* and exits 1 (the hooks live in
`.git/hooks/` itself, no redirection), so the healthy result reads exactly like a
failure.

**A control that cannot run is not a control.** CI on the origin repo is disabled by account
billing, which is why the hooks matter. And note: a billing-blocked GitHub Actions run still
reports "completed" — check that steps actually executed before believing a green list.

## Escalation

The engine does not page anyone by itself. If you wire notifications, make them
**edge-triggered**: notify on a state *change*, never per cycle. A per-minute monitor that
pages every fire produces 1440 alerts a day, gets muted, and a muted monitor is an abandoned
one.
