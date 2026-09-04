# communication-engine

Channel-agnostic communication engine for agent fleets: **poll, store, classify, journal,
stage** — across any messaging platform. Nothing in this repo is named after a platform
except the adapters themselves: a new channel type is a directory drop, not a rewrite.

## What ships today

The engine core is implemented and tested — [`core/README.md`](core/README.md) maps it module
by module (that table is checked against the filesystem by the test suite, in both
directions). Adapters live one directory per platform, all implementing
[`channels/CONTRACT.md`](channels/CONTRACT.md):

| Adapter | State |
|---|---|
| `fake` | in-memory contract adapter — drives the quickstart dry-run and the fault-injection tests |
| `slack` | read-only Web API polling (poll / resolve / health) — no send path at any layer |
| `slack_socket` | Socket Mode push ingestion — read-only, legitimized by a continuous push-vs-poll parity watch |
| `telegram` | read-only bot-API polling — the first non-Slack platform; no history API upstream, so one destructive read per poll at the committed cursor, one chat per instance, no send path at any layer |
| `email` | read-only IMAP polling (Outlook or any RFC-3501 server) — identity is the Message-ID (a non-orderable string, which store and parity handle end to end), ordering is mailbox UID order, threading from References/In-Reply-To; every select is EXAMINE and every fetch BODY.PEEK, so polling never marks mail seen |

What does NOT ship is listed where an adopter acts: the quickstart's honest-limits section.
Notably there is no live send path (the outbox ladder is complete, but only the `fake`
adapter exposes `send()`) and `watchers/` is a stub. A reference scheduler ships
(`scripts/scheduler.py`): a guarded, read-only probe → classify → journal → route → owed
loop you run under your own supervisor. An operator dashboard ships too
(`scripts/dashboard.py` over `core/dashboard.py`): a read-only, loopback-only Streamlit
surface over YOUR journal and outboxes — severity-ordered attention (crashed sends,
answers invalidated by edits, drafts at the gate, the unanswered backlog); streamlit is
the one optional extra it needs, and the engine core never imports it. The gate itself
is on the surface now, OFF by default: `COMMS_UI_WRITE_ENABLED=true` adds a write half
(`scripts/dashboard_write.py`) where composing stages a draft and only a human click on
that exact text releases it through the outbox ladder — the engine never auto-sends.

## Quickstart

Full walkthrough: [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — from a fresh clone to a real
read-only first poll of your own workspace, by config alone (78 seconds in the recorded
non-author adoption run).

```sh
cp settings.example.json settings.json   # then edit; settings.json is gitignored
scripts/install-hooks.sh                 # pre-commit + pre-push gates
scripts/sanitize-gate.sh --self-test     # prove the secret gate on this machine
python3 -m unittest discover -s tests -q # the suite is the spec
```

## Why

An agent fleet already talks through multiple channels (chat workspaces, messenger bridges,
email). Each grew its own poller, watchdog, send path, and staging discipline — mirrored
copies of the same five ideas. This repo turns the mirroring into a contract:

- `core/` — the engine: cursored gap-free polling into a pinned-schema store, word-boundary
  classification that records its cues, an idempotent audit journal, the **stage-first
  outbox** (sending is gated, never a default), evidence-cited reply composition (a
  number enters a reply only as a claim citing a banked artifact), the owed-work edge, edge-triggered
  escalation, health checks that cannot silently no-op, a shadow-mode parity differ, and
  keyed rate-limit back-off.
- `channels/` — one adapter per platform, capability-honest, discovered from configuration.
- `watchers/` — meta-monitoring (stub: the plan is one parameterized watchdog for any
  adapter stack).

Every module generalizes something already running in production on the origin host — see
[`docs/PROVENANCE.md`](docs/PROVENANCE.md); each core module's docstring names the measured
incident it exists to prevent.

## Security model (non-negotiable)

1. **No secrets in this repo, ever** — not in code, not in docs, not as scan patterns.
   `scripts/sanitize-gate.sh` enforces this in the local pre-commit and pre-push hooks
   (`scripts/install-hooks.sh`) and proves itself with `--self-test`. Do not trust a green
   GitHub Actions run: Actions is billing-blocked for this repo and reports "completed"
   while executing zero steps (finding F-3) — the local gates are the real ones.
   Host-specific literals (chat ids, account ids, real channel ids) belong in a local,
   gitignored denylist the gate also applies.
2. **Read-only by default.** Reply policy per target is configuration
   (`never` / `staged` / `direct`), and the outbox stage-gate lives in core — adapters cannot
   bypass it.
3. **No listening ports.** Outbound calls and loopback-bound HMAC bridges only (Socket Mode
   is an outbound WebSocket).
4. Real config is `settings.json` (gitignored). The only config file committed is
   [`settings.example.json`](settings.example.json), all values placeholders or `env:NAME` references.

## Testing

Documentation here is tested like code — the docs above are cross-checked against the
filesystem and the APIs they cite, because prose drifts silently in both directions: this
README once gated its quickstart on a phase that had already shipped, and `core/`'s README
listed six modules that never existed. Every load-bearing property has a test that fails if
the property is removed, and `tests/mutation_check.sh` proves those tests have teeth.

## License

Private repository; licensing intentionally deferred until/unless visibility ever changes.
