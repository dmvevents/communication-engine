# channels/slack — the read-only Slack adapter

**Landed (ENH-18) as a dir-drop: zero `core/` changes.** Implements
[`../CONTRACT.md`](../CONTRACT.md) with one deliberate asymmetry: **there is no send path
at any layer**. The workspace token is authorized read-only, so read-only is a tested
invariant (`tests/test_slack_adapter.py`), not a promise:

- no `send`/`read_back` on the class — an outbox pointed here has nothing to drive;
- the transport default-denies: any Web API method outside the `READ_METHODS` allowlist
  in `adapter.py` is refused before any I/O.

## What is implemented

| Method | How |
|---|---|
| `poll(cursor)` | `conversations.history`, gap-free (all pages, or fail loudly), oldest-first. The cursor is a JSON object of channel id → newest ingested ts, because `poll()` is adapter-wide while Slack history is per-channel. |
| `resolve(ref)` | id → name via `conversations.info` / `users.info`; name → id by paging `conversations.list` / `users.list`. Unresolvable refs raise. |
| `health()` | `auth.test`. Can FAIL (bad token → `auth_ok: false`; network down → `reachable: false`) and reports any active 429 hold in `detail`. |
| `capabilities()` | `{read: true, history: true, search: false, send: false, react: false, threads: false}` — honest, not aspirational. |

Rate limits follow contract rule 3: a 429 raises `core.ratelimit.RateLimited` with the
platform's exact `Retry-After` and the method it hit; the engine's `(instance, method)`
back-off (ENH-1) owns the waiting. The adapter never sleeps.

## Auth

Both values are `env:NAME` references in `settings.json` (see `settings.example.json`):

- `token` — the workspace token. Never a literal, anywhere.
- `channels` — comma-separated channel ids to poll. These ride the environment because
  real Slack ids are host-specific literals the sanitize gate refuses in committed
  files, and the incumbent's env contract proved the pattern in production.

## Deliberately absent

- **Any send primitive** — reply policy for this platform is deny-by-default; a send
  path would need its own authorization and its own outbox-gated adapter work.
- `search.messages`, reactions, thread traversal — not needed for the parity gate (G1);
  each lands only with a test and an allowlist entry in the same diff.
- The optional loopback bridge and MCP surface from the phase-0 design sketch — out of
  scope for a read-only adapter.
