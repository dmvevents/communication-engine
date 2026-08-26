# channels/ — adapter contract

Every channel type (Slack, Telegram, Outlook, Teams, email, …) is a directory under `channels/`
implementing this interface. The core engine imports **only** this contract — a new channel type
must never require a change to `core/`.

## Discovery — how a channel type lands (R11, enforced by `tests/test_extensibility.py`)

A channel type **is** a directory containing `adapter.py` that defines a class named
`Adapter`. Core discovers types by scanning the configured `engine.channels_dir` (default:
`channels/` next to the config) at load time — it never enumerates platform names. So
landing a new type is exactly two actions, neither of which touches `core/`:

1. `mkdir channels/<type>` and write `channels/<type>/adapter.py` with `class Adapter`.
2. Reference `"adapter": "<type>"` from an instance in `settings.json`.

A directory **without** `adapter.py` (like a design-stub README) is not offered as a type;
naming it in config fails loudly, listing what was discovered. `channels/fake/adapter.py`
is the reference implementation to copy.

## Interface (phase 1 will pin this as an abstract base class + conformance test)

| Method | Semantics |
|---|---|
| `capabilities()` | `{read, history, search, send, react, threads}` booleans. The engine degrades gracefully: a channel without `react` simply never gets reaction work; a channel without `search` is covered by history scans. |
| `poll(cursor) -> (messages[], new_cursor)` | Gap-free and idempotent: re-polling with the same cursor may return duplicates, never lose messages. Cursor format is adapter-private; the engine treats it as an opaque string and persists it per (instance, channel). |
| `resolve(ref) -> id` | Human ref (name, handle, alias) ↔ platform id, both directions. |
| `send(channel_id, text, thread_id?) -> receipt` | **Only callable through `core/outbox`.** Adapters expose the primitive; the stage-gate (draft → outbox file → operator gate → send) is enforced in core, never re-implemented per adapter. |
| `health() -> {reachable, auth_ok, detail}` | Cheap (<2s) liveness used by the probe and watchdog layers. Must not consume rate-limit budget meaningfully. |

## Normalized message

`poll()` returns messages in one shape regardless of platform:

```json
{
  "channel_type": "slack",
  "channel_id": "C_EXAMPLE_CHANNEL",
  "sender_id": "U_EXAMPLE_USER",
  "sender_name": "display-name",
  "ts": "platform-native timestamp, sortable within the channel",
  "text": "plain text (platform markup preserved)",
  "thread_id": "null when top-level",
  "raw": { "the untouched platform payload": "kept for audit" }
}
```

## Instance binding

`settings.json` (never committed; see `settings.example.json`) binds adapter instances. Each
instance carries its own auth env-refs, cursor namespace, trigger rules, and **reply policy**:

- `never` — read-only; `core/outbox` refuses `send()` for this target.
- `staged` — drafts land in `state/outbox/` and wait for an explicit operator gate.
- `direct` — adapter may send after outbox logging (reserved for explicitly precedented targets).

Reply policy is **configuration, not code** — the same adapter serves a stage-only customer
channel and a direct-reply DM without branching.

An optional `thread_reply_policy` scopes the same three values to replies **inside a thread**,
so "answer in thread, never the main channel" is expressible; each scope defaults to deny on
its own. An adapter whose `capabilities()["threads"]` is true must honour the `thread_id`
argument — accepting it and posting top-level anyway reports success while doing the one
thing the policy forbade, so `core/outbox` refuses a thread send through an adapter whose
`send()` has no `thread_id` parameter rather than silently flattening it.

## Rules for adapter authors

1. No secrets in code or defaults — auth arrives as `env:NAME` references from config.
2. No listening ports. Outbound-only, or loopback-bound bridges when a local HTTP surface is needed.
3. On a platform 429, raise `core.ratelimit.RateLimited(retry_after)` with the platform's
   Retry-After value — do not sleep inside the adapter. The engine holds back-off state per
   (instance, method) in `core/ratelimit.py`, because a 429 is scoped to one method on one
   workspace and limits are discovered from Retry-After, never assumed. Expose any active
   backoff in `health().detail`.
4. Every send primitive logs to the instance's audit trail before returning.
5. A health check must be able to FAIL. A check that can only pass or no-op is a defect (this
   repo exists partly because of one — see `docs/PROVENANCE.md`).
