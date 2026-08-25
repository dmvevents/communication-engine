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

## 5. First poll — the fake adapter, but YOUR config

Prove the target of this document against the file you just edited, with no network at all.
Two edits to `settings.json` first:

- set `"adapter": "fake"` on the instance you kept;
- **delete the instances you are not using.** Every instance's `env:` references must
  resolve at load time, so a leftover example stub blocks startup (by design — see
  `docs/RUNBOOK.md`, "The engine refuses to start").

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

Your team's words are not ours. Override them in config, never in code:

```json
"taxonomy": {
  "exec_verbs": ["provision", "deploy", "roll back"],
  "commitment_phrases": ["sign off", "by when", "eta"]
}
```

Classification decides whether a message is treated as work to execute, so it is worth ten
minutes. See `docs/RUNBOOK.md` for how to check its behaviour on your own message samples.

## What to expect next

- `state/journal.db` — one row per distinct inbound message, with its classification. Replay
  is safe: re-reading a window never duplicates a row.
- `state/outbox.db` — every send attempt with its state (`INTENT` → `SENT` → `VERIFIED` →
  `COMMITTED`) and any staged drafts awaiting a human.
- `state/messages.db` — the message store.

## Honest limits

- The **`fake` adapter is the only one implemented**; `slack` and `telegram` are contract
  stubs (design READMEs, no `adapter.py` — the engine refuses them by name until one lands).
  "Multi-channel" is so far proven as a *mechanism* — a new channel type is a directory drop
  with zero `core/` changes (`tests/test_extensibility.py`) — not as shipped platform adapters.
- **Read parity against an existing system has not been demonstrated** over a long window
  (gate G1). If you are replacing an incumbent, run both and diff before trusting this one.
- There is **no scheduler in this repo**. `scripts/first-poll.py` runs exactly one cycle;
  the engine gives you poll/classify/journal/outbox primitives and you invoke them from
  cron, a loop, or your own supervisor.
- CI on the origin repo is currently disabled by account billing, so the gates run via local
  git hooks. Verify they are installed on your clone (`scripts/install-hooks.sh`).
