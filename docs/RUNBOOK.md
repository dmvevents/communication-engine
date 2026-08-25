# Runbook — when something goes wrong

Every entry below is a failure this engine (or the system it generalizes) has actually
produced. Symptom first, because that is what you have at 2am.

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

To start over from a clean slate, point `engine.state_dir` somewhere new (or delete the
state directory you configured) — cursors live there, nowhere else.

## "It is not sending anything"

**Check the reply policy first.** Deny-by-default means silence is the *designed* behaviour for
any channel you have not explicitly promoted.

```python
outbox.policy_for("C_YOUR_CHANNEL")   # 'never' | 'staged' | 'direct'
```

- `never` → `PolicyError` on send, adapter never called. Working as intended.
- `staged` → look in the outbox for drafts: `outbox.staged()`. A human is meant to gate these.
- `direct` → if it still is not sending, see the next section.

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

If you retune, add your samples to a corpus test. Vocabulary changes without a test are how a
classifier regresses silently.

## "Are the gates actually running?"

```sh
bash scripts/sanitize-gate.sh --self-test   # must catch all 10 planted secret classes
bash tests/mutation_check.sh                # must report every mutation caught
git config core.hooksPath; ls .git/hooks/pre-push
```

**A control that cannot run is not a control.** CI on the origin repo is disabled by account
billing, which is why the hooks matter. And note: a billing-blocked GitHub Actions run still
reports "completed" — check that steps actually executed before believing a green list.

## Escalation

The engine does not page anyone by itself. If you wire notifications, make them
**edge-triggered**: notify on a state *change*, never per cycle. A per-minute monitor that
pages every fire produces 1440 alerts a day, gets muted, and a muted monitor is an abandoned
one.
