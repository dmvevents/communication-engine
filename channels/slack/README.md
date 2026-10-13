# channels/slack — the Slack adapter

**Landed (ENH-18) as a dir-drop: zero `core/` changes.** Implements
[`../CONTRACT.md`](../CONTRACT.md). Its read path came first; its send path (ENH-31,
2026-10-13) is authorized **for the aws-do-agent harness only** — the harness's bot token,
its own channels, policy `staged` (a human click), never `direct`. Both are tested
invariants (`tests/test_slack_adapter.py`, `tests/test_slack_send.py`), not promises:

- `send()` is reachable only through `core/outbox.py`, whose ladder (durable INTENT → send →
  read-back → COMMITTED), pre-flip refusals (marker, re-read against what the page showed
  the human when staging, 3,500-character size guard, BLOCK-tier credential scan, the policy
  re-read at the click — default deny at every seam) and post-send exact canonical read-back
  (a message carrying the key with other text parks the row as MISMATCH, never re-sent; a
  DEFINITE ok:false refusal of the post — the adapter's `NOT_POSTED_ERRORS` — raises
  `core.delivery.NotPosted` and the draft returns to the gate, while `internal_error`,
  `fatal_error`, `service_unavailable`, `request_timeout` and a non-JSON 5xx stay `ApiError`
  with `may_have_landed` set, the row stays in flight and recovery's read-back decides; an
  ok:false the allowlist does not know is a refusal recovery hands back once absence is
  proven) stand between any caller and
  `chat.postMessage`; `capabilities()["direct"]` is `false`, so a `direct` policy refuses at
  outbox construction and again at the send seam, and `capabilities()["send"]` is true for
  a bot token (`xoxb-`) only — a user token would post in a person's name, so its drafts
  stay Discard-only;
- the transport default-denies: any Web API method outside `READ_METHODS` plus the single
  `WRITE_METHODS` entry (`chat.postMessage`) is refused before any I/O;
- a token granted read-only (this host's, 2026-08-26) still never writes: its channels'
  reply policy is `never`, and the outbox refuses a send there before anything is written.
- `COMMS_SLACK_API_BASE` points the adapter at a loopback stub for tests
  (`http://127.0.0.1:<port>` only — a name such as `localhost` resolves wherever /etc/hosts
  says, so it is refused like any third host, by name, before any I/O) — nothing in the
  test suite reaches slack.com, enforced by the loopback-only guard in `tests/netguard`
  (TCP, UDP and name lookups from Python; not raw sockets or `-S`/`-I` children).

## What is implemented

| Method | How |
|---|---|
| `poll(cursor)` | `conversations.history`, gap-free (all pages, or fail loudly), oldest-first. The cursor is a JSON object of channel id → newest ingested ts, because `poll()` is adapter-wide while Slack history is per-channel. |
| `resolve(ref)` | id → name via `conversations.info` / `users.info`; name → id by paging `conversations.list` / `users.list`. Unresolvable refs raise. |
| `health()` | `auth.test`. Can FAIL (bad token → `auth_ok: false`; network down → `reachable: false`) and reports any active 429 hold in `detail`. |
| `send(channel_id, text, key, thread_id?)` | one `chat.postMessage`, the outbox key in the message **metadata** (`event_type: comms_engine_reply`) so `read_back` can find it without altering the approved text; `thread_ts` places a thread reply. No guard lives here — the outbox refuses before the INTENT flip. |
| `read_back(channel_id, key, thread_id?, text?, since?)` | three answers. True: a message carries this outbox key in its metadata and its canonical text equals the approved text (Slack escapes `&`, `<`, `>`, wraps a typed URL as `<url>` and auto-links a bare domain or an email as `<target\|label>`, so both sides are canonicalized to the content the human typed). False: no message carries the key. `core.delivery.DeliveryMismatch`: a message carries the key with other text. For a message this process posted the fetch is exact (history with `inclusive`, or the thread via `conversations.replies`, both asking for metadata); after a restart the outbox passes the row's `text` and `since` (the platform ts the human saw, else its `created_at`), and history is searched from `since` minus a margin to the end, every page. `thread_not_found` and `is_archived` raise `core.delivery.TargetGone`, which recovery parks at once; any other error — `channel_not_found` included, since Slack also answers it for a private channel the token temporarily cannot see — is transient and retried. |
| `newest(channel_id, thread_id?)` | the newest top-level message, or the newest reply of a thread, that this engine did NOT post (its own carry the metadata) — the outbox's pre-send re-read, and the baseline it records when a draft is staged. |
| `capabilities()` | `{read: true, history: true, search: false, send: <true for a bot token only>, direct: false, react: false, threads: true}` — honest, not aspirational. |

The 3,500-character size guard counts the approved text as typed. Whether Slack's 4,000
split counts the stored, entity-escaped form (`&` → `&amp;`) or UTF-16 units is unverified
against the platform: measure once on the harness before trusting the margin for code-heavy
replies.

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

- **Any write but `chat.postMessage`** — `chat.update`, `chat.delete`, reactions, uploads and
  joins stay refused by the transport allowlist; the grant is "post a human-approved reply",
  nothing else. `direct` sends are declared off, so the engine never posts without a click.
- `search.messages` and reactions — not needed for the parity gate (G1); thread reads
  (`conversations.replies`) landed in ENH-30 for the sweep and the read-back;
  each lands only with a test and an allowlist entry in the same diff.
- **Any send outside the harness grant** — this host's token is read-only by
  authorization (2026-08-26) and its channels' reply policy is `never`; the send path
  exists for the aws-do-agent harness's bot token, at `staged`, and nowhere else.
- The optional loopback bridge and MCP surface from the phase-0 design sketch — out of
  scope for this adapter.
