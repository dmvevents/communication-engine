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
| `instances[].channels[].kind` | optional; `dm` or `channel`. A 1:1 direct message is answered top-level and closed by the DM rule (step 9). Omitted, a Slack-shaped `D…` id infers `dm` and any other id is a channel |
| `instances[].answerers` | optional; the sender ids whose replies may close OTHER people's asks (the agent's bot user, the operators). Omitted, any non-asker's answer closes an ask |
| `instances[].send_marker` | optional; a prefix (e.g. `"[AGENT]"`) every send from this connection must carry — prepended when a draft is staged, required again at the click |
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
A thread reply through an adapter that cannot place it — one declaring `threads: false`, or
one whose `send()` takes no `thread_id` — is refused before any new row is written or claimed
(at `stage()`, at a direct send, and again at the Send click), so the outbox never flattens it
into the main channel and never leaves it wedged at the gate; the write surface decides a
reply's placement with the adapter in hand, and posts top-level only where the channel policy
allows it, saying so.

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

One shape deliberately stays unacted-on however you tune: an action verb with no
directive ("the build and test cycle takes an hour") classifies *down* to STATEMENT
rather than risking a false order. That hedge is no longer silent — the decision
carries an ambiguity flag, the journal records it on every row, and
`Journal.ambiguity_stats()` counts how often the classifier hedged versus decided, so
you can see what the safe default is costing you before changing it. If your channel's
hedges deserve human eyes, set `"escalate_ambiguous": true` on the instance (next to
`taxonomy`) and the reference scheduler routes each hedged decision to the operator's
owed-work queue instead of only logging it. The signal comes from the same word-boundary
rules you just tuned — no LLM sits in the hot path.

## 7. First real poll — your workspace, read-only

The shipped real adapter is `slack`. This step uses its read path only: with every channel
at `reply_policy: never` (the default you copy below) nothing can post. Its send path
(ENH-31) exists, but is reachable only through `core/outbox.py` at a channel policy of
`staged` — a human click on the write surface — and only for a bot token (`xoxb-`); the
transport refuses any Web API method outside its read allowlist plus that single write,
`chat.postMessage`, before a byte leaves the process (`tests/test_slack_adapter.py` and
`tests/test_outbox_write.py` fail if the allowlist, the token rule or the policy gate is
lost). Switch your instance to it — still editing only `settings.json`:

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
imported while the write gate below is off (same AST-enforced rule as the scheduler),
and state that does not exist yet is *reported* missing — never silently created,
never rendered as a healthy zero.

**What "unanswered" means, and how an ask stops being one.** The unanswered backlog is
the open *ask* backlog: rows whose kind is one of `core/classify.ACTIONABLE_KINDS`
(EXEC-REQUEST, COMMITMENT-ASK, QUESTION, ATTACHMENT-ONLY) with no response tied to
them. A STATEMENT — a status remark, a join notice — owes nobody an answer and is never
listed there (it still counts as a message, and the Journal tab's kind table shows it);
a row with no recorded kind is shown as *unclassified* rather than hidden. An ask
becomes answered in exactly two ways, both recorded in `journal.db` with a response key
naming what answered: a reply **sent from the write surface** with the ask's Reply
button (the outbox key, tied the moment the send is proven on the target — staging
alone never ties), or a reply **inside the ask's thread from someone other than the
asker**, which the reference scheduler sweeps every cycle through the adapter's
optional `replies()` (`core/threads.py`; the shipped `fake` and `slack` adapters have
it). A top-level reply posted in the channel by someone else cannot be attributed to the
ask it answers, so it leaves the ask open — answer in the thread, or from this surface's
Reply button, which ties the ask itself wherever the reply lands. "Responded" means
exactly that — somebody else replied in the thread, or you sent an answer — not
"resolved": a third party's "+1" closes an ask too, and the Journal tab counts those
apart from answers sent from here. The scheduler's cycle line reports `answered=N` — the
in-thread ties the sweep made that cycle; answers sent from the surface are tied by the
surface and never appear in that count — and
one line per channel whenever the sweep was rate-limited, truncated, or refused by the
platform for one ask (a deleted ask answers `thread_not_found` forever; it is reported
and skipped, never allowed to stop the loop), and one line (`sweep: SKIPPED`, with its cause)
per channel that owes a sweep the cycle never reached (after a 429, or with more channels than
calls) — that channel is swept first next cycle, also when the next cycle is a new `--once`
run, because the sweep window and the channel order live in the store.

The dashboard is read-only **by default**. Setting `COMMS_UI_WRITE_ENABLED=true` in
the environment adds a write surface (`scripts/dashboard_write.py` — the one module
allowed to import the send layer; with the gate off, or unset, it is never even
imported, and any value other than true/false is refused by name): composing there
**stages** a draft into the outbox, and only your click on that exact staged text
sends it — `outbox.release(key)` under the hood, so the approved send is durable,
deduped, paced, and read-back-proven like any other; Discard is terminal and kept.
The agent never auto-sends: there is no compose path that skips the gate, and
`tests/mutation_check.sh` removes the gate one piece at a time to prove a test goes
red each time. Deny-by-default still binds — a channel at `reply_policy: never` (the
default) is not composable even with the gate on — and of the shipped adapters only
`fake` and `slack` declare send capability. `fake`'s makes the whole write cycle provable
offline; `slack`'s (ENH-31) is **authorized for the aws-do-agent harness only** — its bot
token, its own channels, policy `staged`, never `direct` (the adapter declares `direct`
off, so an outbox built for a direct policy refuses at construction) — and flipping the
gate gives a live channel a send path only where its policy already says `staged`. A token
granted read-only never writes: its channels stay at `never`.

Every Slack send passes five refusals **before** the durable INTENT flip and one proof
after the post, so a refused draft stays STAGED and discardable and nothing reaches the
platform: (1) the connection's `send_marker` (an instance key, e.g. `"[AGENT]"`) is
prepended when the draft is staged, shown on the card, and required again at the click by
the outbox the surface builds — several senders share the bot identity in the harness, and
the marker is how a reader and the other senders' dedupe tell the engine's messages apart
(it must be followed by a space or the end of the text, so `[AGENT]glued` and another
sender's `[AGENT-OTHER]` do not pass); (2) the click **re-reads** the channel or thread and
refuses, with who and when ("re-read first"), when anything the human has NOT seen has
arrived — the baseline is the platform's newest message recorded when the draft was staged
(platform clock against platform clock; the engine's own earlier posts never count), and
staging the same text again is the acknowledgement that re-arms the click; (3) a text over
3,500 characters is refused — Slack silently splits at 4,000 and returns one ts; the guard
counts the approved text as typed, and whether Slack's split counts the stored,
entity-escaped form is unverified against the platform, which is what the 500-character
margin is for; (4) a BLOCK-tier credential shape in the text (`core/redact.py`) is refused
by class name, never echoed; (5) the channel's reply policy, re-read at the click, must be
exactly `staged` — anything else is deny, and `direct` sends only through an adapter that
declares them on, which Slack does not. After the post the **read-back** must find the SAME
message: the outbox key rides the message metadata and the canonical text must match. A
message carrying the key with other text parks the row as `MISMATCH` — never re-sent,
never committed, and the card says a human must look; no message carrying the key leaves
the row SENT for `Outbox.recover()`, which re-sends only what the platform does not hold,
isolates each row (one dead thread cannot wedge the others) and stops on a 429. A 429 on
the post itself hands the draft back to the gate as STAGED, because nothing was posted.
Placement is the surface's routing rule, not a refusal: in the ask's thread in a channel,
top-level in a 1:1 direct message (a channel declared `kind: dm`, or inferred from a
Slack-shaped `D…` id). The outbox ladder and the surface are tested against a loopback
Slack stub through `COMMS_SLACK_API_BASE` — which admits `http://127.0.0.1:<port>` only
and refuses a name, a third host or a path before any I/O — and against the fake adapter;
nothing in the suite reaches slack.com.

Who may **close** an ask is the instance's `answerers` list (sender ids — the agent's bot
user, the operators): with one, only their replies close OTHER people's asks, in threads
and in DMs alike, so a bystander's "+1" closes nothing; an ask posed by a listed answerer
(the agent's own clarifying question) is closed by the other party's answer, or the engine
would owe its agent an answer forever. Without a list, today's rule stands (any non-asker
whose reply is not itself an ask; the other party of a DM) — note that a multi-party
channel declared `kind: dm` with no answerer list closes an ask on anyone's next
statement. In the thread sweep and the DM rule a reply that classifies as a question is
never credited, from anyone: a clarifying question is a question (the write surface's
Reply ties the text the human chose to send, whatever its shape). A message closes at most
one ask, ever, and the engine's own deliveries are never re-credited to an older ask. The
Journal tab lists every ask closed by someone else's thread or DM reply under "closed by
someone else — review", newest first, so the operator can see which asks left the queue
that way; there is no reopen action there — the journal row is the record to inspect.

The same gate covers **configuration** (`scripts/dashboard_config.py`, over
`core/reconfig.py`): with it on, the dashboard can also add, edit and remove
connections (`instances[]` — the adapter is picked from the discovered `channels/`
set, never typed free-form) and monitored channels (`channels[]`). Editing **stages**
an exact `settings.json` diff; nothing touches the file until your click on that exact
diff, and Discard is terminal and kept. Every candidate is validated with the engine's
own loader *before* it can be staged, so a mistake (an unknown key, an undiscovered
adapter, a missing env var) is refused at the gate instead of at the next startup. A
new channel defaults to `reply_policy: never` (deny) **by omission** — the loader's own
default is the single copy of that rule — and any change that **widens** a reply policy
(toward `staged` or `direct`, on either placement scope) is flagged in red on the card
you click, never bundled silently into an unrelated apply. Auth is configured as
environment-variable NAMES only (`env:NAME`); the surface shows whether the named
variable is set, never its value, and a pasted secret is refused unstored and unechoed.
Applying tells the truth about reload semantics: the dashboard re-reads `settings.json`
on every rerun, but a **running scheduler or watcher loads settings only at startup and
must be restarted to pick the change up** — there is no hot-reload path, and the
surface says so instead of implying one.

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

- **Answered state has exactly three routes.** An ask is marked answered by a reply sent
  from the write surface's Reply button (tied by the ask itself, in its thread or
  top-level as the channel's thread policy allows), by an in-thread reply from someone
  other than the asker — from a listed answerer, when the source names them — swept each
  scheduler cycle, or, in a 1:1 direct message (declared or inferred), by the next
  top-level message that is an answer, which closes the most recent open ask before it,
  once — a message closes at most one ask, ever, and the engine's own deliveries are never
  re-credited; a reply inside a DM's thread is not credited (0 of 535 DM messages measured
  in-thread), and DM asks spend no thread-sweep budget. "Responded" means exactly that, not
  "resolved": a third party's "+1" closes an ask too, and the dashboard counts
  thread-closed asks apart from answers sent from here. A reply posted top-level in the
  channel by anyone else is stored and listed but cannot be tied to the ask, and the
  engine never guesses. Adapters with neither `replies()` nor send capability — today
  `slack_socket`, `telegram` and `email` — have no route to answered at all: their asks
  stay UNANSWERED until a send-capable path exists for that platform.
- **The thread sweep spends platform calls.** Each open ask costs one
  `conversations.replies` call per cycle — a refused fetch (a deleted ask) counts too —
  bounded by `SWEEP_MAX` (25) per adapter per cycle and spent across that adapter's
  channels with one call reserved for every channel still waiting: a backlog channel takes
  the bulk, never the whole budget, and the bulk moves between channels; a channel a cycle
  did skip (a 429, or more channels than calls) is swept first in the next and named on
  its own `sweep: SKIPPED` line; within a channel the window rotates through the open
  asks — as a position in the newest-first list, so at about one call per cycle with a
  new ask arriving every cycle it can stick on one ask (COMMS-79 item 18, due before a
  deployment sweeps 25 or more channels on one adapter or runs at that tier). Window and
  order are stored, so `--once` runs keep them. A 429 is honoured and reported, and the
  rest of that adapter's sweep waits for the next cycle. Slack's rate tier for
  `conversations.replies` is far lower for commercially distributed non-Marketplace apps
  than for an internal app, so a distributed app should expect asks to close more slowly,
  never faster — and at about one call per cycle it needs that window fix first.
- Five adapters ship: **`fake`** (in-memory dry-run), **`slack`** (poll / replies / resolve /
  health, and one send — `chat.postMessage`, reachable only through `core/outbox.py` at policy
  `staged`, authorized for the aws-do-agent harness's bot token only; `tests/test_slack_adapter.py`
  fails if any other write surface appears) — **`slack_socket`** (Socket Mode push ingestion,
  read-only; push is for latency only: Socket Mode can MISS events, so the socket adapter is
  never the truth — run `scripts/push-poll-parity.py` against the polling store continuously
  before trusting it, and know that **it has not yet run against a live workspace**; that
  needs an operator-created app-level token, see `channels/slack_socket/README.md`) — and
  **`telegram`** (bot-API polling, read-only, the first non-Slack platform). Telegram bots
  have **no history API**: `getUpdates` is a queue whose offset permanently acknowledges
  everything below it, so the adapter makes exactly one destructive read per poll at the
  engine's committed cursor, watches exactly **one chat per instance**, and can never supply
  the platform snapshot — parity against a Telegram store is **permanently fail-closed**
  (every miss reads ENGINE_LOST), which `core/parity.py` and the doctor declare by name
  rather than leave to be misread as a read-path defect (`channels/telegram/README.md`;
  `tests/test_telegram_adapter.py` fails if a send path or a stub snapshot appears). It has
  **not yet run against a live bot**. Finally **`email`** (IMAP polling, read-only —
  Outlook or any RFC-3501 server): the platform that breaks the float-ts assumption on
  purpose. Message **identity is the Message-ID string** — never the sender-controlled
  Date header and never the IMAP UID — ordering is mailbox UID (arrival) order, threading
  derives from the References/In-Reply-To headers, and `core/parity.py` classifies these
  **non-orderable identities** by served-set membership, declaring its timeline-based
  window classes unavailable rather than guessing an order. Every mailbox selection is
  read-only (EXAMINE) and every fetch uses BODY.PEEK, so polling never marks the
  operator's unread mail `\Seen`. It has **not yet run against a live mailbox**, and on
  Outlook/M365 specifically, IMAP `LOGIN` needs an app password or a tenant that still
  permits basic auth — OAuth2/XOAUTH2 is not implemented (`channels/email/README.md`).
  "Multi-channel" is now proven as a *mechanism* — a new
  channel type is a directory drop with zero `core/` changes (`tests/test_extensibility.py`)
  — plus three real platforms (four ingestion paths).
- **Read parity against an existing system has not been demonstrated** over a long window
  (gate G1). If you are replacing an incumbent, run both and diff before trusting this one.
  The diff is shipped, not homework — `core/parity.py` opens the incumbent's own database
  read-only and compares it to the engine's store:
  `python3 -m core.parity --oracle <incumbent.db> --candidate state/messages.db --channel C_YOUR_CHANNEL`
  (exit 0 parity, 1 names every missed/extra message, 2 an unusable comparison — an empty
  oracle is an ERROR, never a pass).
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
