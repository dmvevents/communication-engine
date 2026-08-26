# Quickstart — adopt the engine for your own team

Target: **first successful poll in under 30 minutes**, editing only `settings.json`. If you
find yourself editing code to adopt this, that is a bug — please file it.

## What you need

- Python 3.11+ (no third-party packages required for the core engine)
- A bot/app token for the platform you want to watch, in your **environment**, never in a file
- Somewhere to put state (a directory the engine owns)

## 1. Clone and verify the engine works before configuring anything

```sh
git clone <this repo> communication-engine
cd communication-engine
python3 -m unittest discover -s tests -q     # expect OK
bash tests/mutation_check.sh                 # expect "every removed property turned the suite red"
scripts/install-hooks.sh                     # gates run on every commit/push
```

Running the tests first is deliberate: if they fail on your machine, you want to know that
now and not while debugging your own config.

## 2. Copy the example config

```sh
cp settings.example.json settings.json       # settings.json is gitignored
```

Edit it. The only things you must change:

| Field | What to put |
|---|---|
| `instances[].name` | any label you like, e.g. `my-team-slack` |
| `instances[].adapter` | any directory under `channels/` containing an `adapter.py` — discovered, never hardcoded. `fake` ships; use it to try the engine dry |
| `instances[].auth.token` | **`env:YOUR_VAR_NAME`** — an environment-variable *reference* |
| `instances[].channels[].id` | the channel/chat IDs you want watched |
| `instances[].channels[].reply_policy` | `never` (default), `staged`, or `direct` |
| `instances[].channels[].thread_reply_policy` | optional; same three values, applied only to replies **inside a thread**. Omit it and a thread is governed by `reply_policy` like anything else |
| `instances[].principals` | the user IDs whose messages matter |

Paths in `engine` are resolved **relative to the config file**, so the same file works on any
machine. Point `state_dir` wherever you like.

## 3. Put the credential in your environment

```sh
export YOUR_VAR_NAME='...'        # the value your config references as env:YOUR_VAR_NAME
```

The engine **refuses to start** if a referenced variable is missing, and refuses a literal
token pasted into the config. Half-configured is the worst state to discover during an
incident.

## 4. Understand the reply policy before you point it at anything real

This is the one decision that can embarrass you, so it is deny-by-default:

| Policy | Behaviour |
|---|---|
| `never` (default) | read-only. `outbox.send()` raises; the adapter is never called. |
| `staged` | writes a draft for a human to approve. The adapter is never called. |
| `direct` | may send, after the outbox records intent and verifies delivery by read-back. |

**A channel you forget to configure is `never`.** You cannot accidentally post as anyone by
omission. Start every channel at `never`, watch what the engine *would* have done, and only
then promote a channel you are confident about.

### Answering in a thread but never in the main channel

A top-level post in a busy channel is seen by everyone; a thread reply is seen by the people
already in that thread. So placement is policed separately, with `thread_reply_policy`:

```json
{ "id": "C_YOUR_CHANNEL", "reply_policy": "never", "thread_reply_policy": "direct" }
```

That channel now answers **only** inside a thread — `outbox.send(...)` with no `thread_id`
raises, exactly as a `never` channel does. Each scope is deny-by-default on its own: naming
one placement does not promote the other. The outbox records which placement each reply used
(`scope` and `thread_id` columns), so a staged draft tells the approving human where it would
land, and a crashed thread reply resumes **in its thread** instead of resurfacing top-level.

## 5. First poll — the fake adapter, but YOUR config

Prove the target of this document against the file you just edited, with no network at all.
The example already **leads** with the fake-adapter instance, which loads and polls with no
credentials (a test pins both properties, so the copy step cannot ship broken again). One
edit to `settings.json` first:

- **delete the instances you are not using** — for this dry run, keep only the first. A
  leftover instance blocks startup, because its `auth` env references must resolve at
  load time (by design — see `docs/RUNBOOK.md`, "The engine refuses to start").

```sh
python3 scripts/first-poll.py --config settings.json --seed-demo
```

Expect one line per channel and then `FIRST POLL OK`. That was the real pipeline — poll →
store → classify → journal → cursor — run against your config; look in `state/journal.db`
for the demo message and its classification. `--seed-demo` plants one message per channel
inside the fake adapter's memory; without it a first poll legitimately returns 0 messages.
Run the command a second time *without* `--seed-demo`: the cursor was persisted, nothing is
re-read, and the journal count does not move. Polling is read-only — this script cannot
send (there is a test asserting it never even imports the send layer).

The same pipeline shape is pinned by the suite you ran in step 1:

```sh
python3 -m unittest tests.test_portability -k EndToEnd -v
```

## 6. Tune the classifier to your team's vocabulary

Your team's words are not ours. Override them in config, never in code. The taxonomy is
**per-instance** — it lives *inside* the instance object, next to that instance's `adapter`
and `channels`. A `"taxonomy"` at the top level of the file is refused at load, with the
message naming this placement — the first non-author adoption run made exactly that
mistake back when the loader silently ignored unread keys, and believed the classifier
was retuned while it kept our vocabulary:

```json
"instances": [{
  "name": "my-team-slack",
  "adapter": "fake",
  "taxonomy": {
    "exec_verbs": ["review", "provision", "deploy", "roll back"],
    "commitment_phrases": ["sign off", "by when", "eta"]
  }
}]
```

`review` is in that list on purpose: the demo message is "Please review the quickstart
demo message.", so this exact example is observable against it. (A doc test holds the
example and the demo text together — the first adopter re-run found them drifted apart,
which made this step unfollowable.)

To watch the change you need a *fresh* demo message, and a plain re-run cannot produce
one: `--seed-demo` re-plants the same message and your cursor is already past it, so a
second run correctly reports `0 polled` (that is the replay safety from step 5; the
"0 messages" entry in `docs/RUNBOOK.md` says the same). Delete the state directory you
configured — the cursor lives in the message store inside it — then re-run the seeded
poll command from step 5. (Pointing `engine.state_dir` somewhere new works only if your
`store` path derives from it; the shipped example pins `store` under `state/` explicitly,
so moving `state_dir` alone leaves the cursor behind and still reports `0 polled` —
measured, not theory.)

Under the shipped vocabulary the demo message is a QUESTION (its only cue is "please");
with `review` in your `exec_verbs` it becomes an EXEC-REQUEST, matched `["review",
"please"]`. Classification decides whether a message is treated as work to execute, so
it is worth ten minutes. See `docs/RUNBOOK.md` for how to check its behaviour on your
own message samples.

## 7. First real poll — your workspace, read-only

The shipped real adapter is `slack`, and it is read-only at every layer: the class has
no send method, and its transport refuses any non-read Web API method before a byte
leaves the process (`tests/test_slack_adapter.py` fails if either property is lost).
Switch your instance to it — still editing only `settings.json`:

```json
"instances": [{
  "name": "my-team-slack",
  "adapter": "slack",
  "auth": { "token": "env:MY_SLACK_TOKEN", "channels": "env:MY_SLACK_CHANNELS" },
  "channels": [{ "id": "C_YOUR_CHANNEL", "label": "team", "reply_policy": "never" }]
}]
```

```sh
export MY_SLACK_TOKEN='<your bot token>'       # needs history-read scope
export MY_SLACK_CHANNELS='C_YOUR_CHANNEL'      # comma-separated channel ids
python3 -m core.doctor --config settings.json  # preflight BEFORE the first poll
python3 scripts/first-poll.py --config settings.json
```

The doctor is the same checks a failed poll would surface, run up front: config
validated, credentials resolved (named, never printed), one live read-only poll per
instance confirming each configured channel readable — including the two-place rule
below — and the effective reply policy printed per channel. `DOCTOR OK` means the
first poll should work; anything else names what to fix.

Both `auth` values are `env:` references, like every credential: the channel ids are
workspace-specific literals that should no more live in a committed file than the token.

A channel id appears in **two places**, and the lists must agree: the adapter polls the
ids in `MY_SLACK_CHANNELS`, but the engine keeps a message only for ids listed under
`channels[]`. An id present in one list and missing from the other is not an error — it
is a successful-looking poll that stores nothing, which makes this the one line worth
double-checking before you trust a quiet channel (see the `0 messages` entry in
`docs/RUNBOOK.md`).

Expect one line per channel with a real message count, then `FIRST POLL OK`. The first
poll reads the channel's full visible history (no cursor yet), so give a busy channel a
minute. Re-run the command and the count drops to 0 — the cursor survived, exactly like
the dry run in step 5.

## 8. Keep it running — the reference scheduler

`scripts/first-poll.py` runs exactly one cycle. The reference scheduler keeps that cycle
running, with the loop bugs an adopter would otherwise re-learn already fixed
(`core/schedule.py` carries the incident behind each one): only **one instance per state
directory** — a second refuses with exit 3 instead of racing the first; the cursor
commits only **after** the journal, so a crash mid-cycle duplicates and the journal
absorbs it, never loses; idle backoff widens the cadence for a quiet channel but can
**never suppress owed work**; and unattended owed work pages once per state *change*,
not once per cycle.

```sh
python3 scripts/scheduler.py --config settings.json          # run until Ctrl-C
python3 scripts/scheduler.py --config settings.json --once   # one guarded cycle (cron)
```

Every classified message is **routed** and the destination recorded on its journal row:
asks (EXEC-REQUEST, COMMITMENT-ASK, QUESTION, ATTACHMENT-ONLY) become owed work that
stays visible until answered or attended, STATEMENTs are logged. `OPERATOR:` lines on
stdout are the escalations — wire them to whatever your operator actually reads, never
to a send path (the script does not even import the outbox; a test enforces that).

To watch the full wiring fire on the fake adapter, start from a fresh state directory
(the step-6 rule — the cursor is already past the demo message):

```sh
rm -rf state && python3 scripts/scheduler.py --config settings.json --once --seed-demo
```

The demo ask journals as a QUESTION routed `owed:answer`, and because no live process is
driving it the same cycle prints `OPERATOR: DEGRADED: owed-work-unattended` — the
goal-triggered edge firing with **no new inbound message**. Owed work closes when the
ask's journal row is marked responded, when you close it yourself (`owed.close(id)`),
or goes quiet while a recorded driver is verifiably alive
(`owed.attach_driver(id, "pid:1234")` — a pid, because a plan written to a file is not
a driver; only a live process makes progress).

## 9. Watch it — the operator dashboard (optional)

Everything so far runs on the standard library. The dashboard is the one place that
wants a third-party package: `pip install streamlit` (the engine core never imports
it, and every other step works without it).

```sh
COMMS_SETTINGS=settings.json scripts/dashboard-serve.sh
```

Then browse `http://127.0.0.1:8502` — from another machine, tunnel first:
`ssh -L 8502:127.0.0.1:8502 <host>`. The wrapper binds **loopback only**, on purpose:
the page shows journal text and staged drafts, so it is never put on an open port
(a test pins the bind address).

What you get is the attention queue, severity first: sends that may have died
mid-flight (run `Outbox.recover()`), answers invalidated by a later edit, drafts
waiting at the operator gate with their exact text, then the unanswered backlog —
all read from YOUR `journal.db` and one outbox per instance, as resolved by YOUR
`settings.json`. It is a viewer by construction: every database connection is
read-only (`core/dashboard.py` opens sqlite `mode=ro`), the send layer is never even
imported (same AST-enforced rule as the scheduler), and state that does not exist yet
is *reported* missing — never silently created, never rendered as a healthy zero.

## What to expect next

- `state/journal.db` — one row per distinct inbound message, with its classification and
  the destination the loop routed it to. Replay is safe: re-reading a window never
  duplicates a row.
- `state/outbox.db` — every send attempt with its state (`INTENT` → `SENT` → `VERIFIED` →
  `COMMITTED`) and any staged drafts awaiting a human. Created on the first staged or sent
  draft — a read-only first poll leaves no outbox, correctly.
- `state/messages.db` — the message store.
- `state/owed.db`, `state/escalate.db`, `state/scheduler.lock` — created by the reference
  scheduler: the owed-work registry, the escalation edge state, and the single-instance
  lock (held by the running process; released automatically when it dies).

## Honest limits

- Three adapters ship: **`fake`** (in-memory dry-run), **`slack`** — **read-only on purpose**
  (poll/resolve/health; it exposes no send path at any layer, and `tests/test_slack_adapter.py`
  fails if one appears) — and **`slack_socket`** (Socket Mode push ingestion, equally
  read-only). Push is for latency only: Socket Mode can MISS events, so the socket adapter is
  never the truth — run `scripts/push-poll-parity.py` against the polling store continuously
  before trusting it, and know that **it has not yet run against a live workspace** (that
  needs an operator-created app-level token; see `channels/slack_socket/README.md`).
  `telegram` remains a contract stub (design README, no `adapter.py` — the engine refuses it
  by name until one lands). "Multi-channel" is so far proven as a *mechanism* — a new channel
  type is a directory drop with zero `core/` changes (`tests/test_extensibility.py`) — plus
  one real platform (two ingestion paths for it), not two platforms.
- **Read parity against an existing system has not been demonstrated** over a long window
  (gate G1). If you are replacing an incumbent, run both and diff before trusting this one.
- **Slack throttles by distribution model, and the poll path is what it throttles.** Since
  **2025-05-29**, newly-created apps that are commercially distributed without Slack
  Marketplace approval face sharply reduced limits on `conversations.history` and
  `conversations.replies` — the two methods every poll cycle here depends on.
  Internal customer-built apps are **exempt** (as are Marketplace-approved apps and
  existing installs of older apps), and an internal app is this quickstart's target case —
  but choose the distribution model knowing this: take the tool commercial without
  Marketplace approval and the poller is throttled by the platform, not by the engine.
  Slack does not publish the reduced values, so the engine discovers each method's real
  limit at runtime from `Retry-After` (`core/ratelimit.py`) — a throttled poller slows
  down honestly instead of dropping messages.
- The reference scheduler (`scripts/scheduler.py`, step 8) is a single-process loop, not
  a daemon manager: it does not fork, detach, or restart itself — run it under your own
  supervisor (cron with `--once`, a service unit, a container), and let a crash restart
  it; the cursor-commit ordering is what makes that restart safe. Its single-instance
  guard is a sqlite lock on the state directory and is only trustworthy on a **local**
  filesystem — two hosts sharing state over NFS can both acquire it. One state
  directory, one host, one loop.
- CI on the origin repo is currently disabled by account billing, so the gates run via local
  git hooks. Verify they are installed on your clone (`scripts/install-hooks.sh`).
