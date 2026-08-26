# channels/slack_socket — Socket Mode push ingestion (ENH-14)

**Push for latency, poll for truth.** This adapter receives Slack Events API payloads
over an outbound WebSocket (Socket Mode) — no public endpoint, no inbound firewall
hole, sub-second detection, zero Web API rate-limit budget. It landed as a pure
dir-drop (R11: zero `core/` changes) and is **ingestion-only**: like the polling
`slack` adapter it exposes no send path at any layer.

## Why it must never run alone

Slack documents that Socket Mode events can be **missed**: a 10-connection cap,
connections that cycle every few hours (announced by `disconnect` frames), and drops
when the mode is enabled mid-flow. Push-only would silently lose messages — the exact
failure class this project exists to eliminate. So:

- the polling `slack` adapter remains the **truth** (gap-free cursored history);
- this adapter provides **latency** on the same channels;
- `scripts/push-poll-parity.py` diffs the two stores **continuously** and fail-stops
  on any divergence (poll-settled rows push never delivered, or push rows the poller
  never confirmed). Rows newer than the poll watermark are "not yet confirmable",
  never an alarm.

Properties pinned by `tests/test_socket_adapter.py` (and torn out one at a time by
`tests/mutation_check.sh`): reconnect on `disconnect` frames using a **fresh**
single-use `apps.connections.open` url; every envelope acked with its `envelope_id`
(after buffering, so a crash duplicates rather than loses); unwatched channels and
ephemeral edit wrappers (`message_changed`/`message_deleted`) never ingested (they
would poison parity permanently); same-cursor re-polls may duplicate, never lose;
health can fail.

## `app_rate_limited` — the platform admitting it dropped deliveries (ENH-16)

The Events API stops delivering above **30,000 deliveries per workspace per app per
hour** and announces the overflow with an `app_rate_limited` payload — an event, not
an error, so an engine that shrugs at it loses messages while looking healthy. This
adapter treats it as a first-class loss signal:

- the payload (enveloped or bare) records a **delivery-gap window** — the platform
  names the minute it dropped in (`minute_rate_limited`); a malformed admission
  records an *unknown window*, which is still a loss;
- `health()` returns `complete: false` and names the window(s) while any gap is
  unrecovered (feed `delivery_gaps()` to `core.checks.delivery_gap_check` for a
  failing registry verdict);
- `recovery_due()` is true while a gap is open: the **completeness poll** (the
  gap-free cursored `slack` adapter — the truth side) is due *now*, on the owed-work
  rule (R3: backoff never defers recovery of an admitted loss);
- only `mark_recovered(completed_at)` — the epoch time a completeness poll
  **completed**, past the window's end — closes a gap. Completion time is the
  evidence, not a message watermark: a cursored poll covers all history up to its
  completion even when it finds nothing, and a quiet channel's watermark would
  otherwise leave the gap failing forever.

## Configuration

```json
{
  "name": "example-slack-socket",
  "adapter": "slack_socket",
  "auth": {
    "app_token": "env:SLACK_APP_TOKEN_ENV_REF",
    "channels": "env:SLACK_CHANNELS_ENV_REF"
  },
  "channels": [ { "id": "C_EXAMPLE_CHANNEL", "reply_policy": "never" } ]
}
```

`app_token` is a Slack **app-level** token (they start with `xapp-`), not a bot token.
It is only accepted as an `env:NAME` reference — `core/config.py` refuses the literal.
`channels` is the same comma-separated id list the polling instance watches, so the
parity watch compares like for like.

## Live smoke — OPERATOR-OWNED

The code path is fully tested against faked transports; nothing here has touched a
live workspace yet, because the required token does not exist yet. The operator
(authorized 2026-08-26) must:

1. In the Slack app settings: **Socket Mode → Enable**, and subscribe the app to the
   `message.*` event scopes for the watched conversations.
2. **Basic Information → App-Level Tokens → Generate** with scope
   `connections:write`; export it as the env var the config references.
3. Run both ingestion paths against separate stores, then keep the watch running:

   ```
   python3 scripts/push-poll-parity.py --poll-db state/poll.db \
       --push-db state/push.db --channel <watched-id> --interval 60
   ```

   Exit 1 means divergence (stop trusting push; investigate); exit 2 means the
   comparison was unusable (empty poll oracle — R8: never a pass).
