# channels/telegram — the read-only Telegram bot adapter (ENH-25)

The first **non-Slack** platform, landed as a pure dir-drop (R11: zero `core/` changes).
Its purpose beyond its own utility: proving the contract against a platform that
disagrees with Slack about something structural — **whether re-reading is possible at
all.**

## The platform finding

Telegram bots have **no history API**. `getUpdates` is a queue, and its `offset`
parameter is an acknowledgement: calling `getUpdates(offset=N)` permanently confirms
every update below `N`, and those updates are gone from the platform forever. Everything
unusual about this adapter follows from that (full design record:
`state/DESIGN-ENH-25-telegram.md` in the standup store; properties pinned by
`tests/test_telegram_adapter.py`):

| Consequence | What the adapter does |
|---|---|
| Advancing the offset destroys the re-read window | The offset sent is **exactly the cursor the engine handed back** — committed only after the store/journal write (`core/schedule.py`). The adapter never remembers its own high-water mark. |
| A crash mid-drain would lose acknowledged batches | **One `getUpdates` call per `poll()`** — no in-poll pagination. A backlog longer than 100 drains across successive polls, each ratcheting the cursor after the journal write. |
| One bot queue has one ack authority, but the engine keys cursors per (instance, channel) | **Exactly one chat per instance**, refused at construction otherwise. Run one bot per chat. Updates from other chats the bot is in are not ingested but do advance the cursor. |
| A bot cannot ask "what do you still serve?" | `retrievable_ts` is **deliberately absent**, never stubbed. Parity against this store is **permanently fail-closed** (every miss reads `ENGINE_LOST`); `core/parity.py` and `core/doctor.py` declare the capability gap by name (ENH-27). |

Other contract mappings: `ts` is the **message_id** (the store keys rows on ts and
`message.date` is whole seconds — burst messages would merge; an `edited_message`
carries the same message_id, so edits land as revisions, R23). `channel_id` is the
numeric chat id — **negative for groups**. A 429's wait is read from the JSON body
(`parameters.retry_after`), not the Retry-After header, and raises
`core.ratelimit.RateLimited` naming the method. `health()` uses `getMe` and can FAIL;
it never touches the destructive queue. File attachments carry `url: None` always —
Telegram download URLs embed the bot token, and a store must never hold a credential.

## Configuration

```json
{"name": "telegram-watch", "adapter": "telegram",
 "auth": {"token": "env:TELEGRAM_BOT_TOKEN", "channels": "env:TELEGRAM_CHAT_ID"},
 "channels": [{"id": "-1001234567890", "label": "ops", "reply_policy": "never"}]}
```

`channels` in `auth` is the single numeric chat id (an `env:NAME` reference — real chat
ids are host-specific literals the sanitize gate refuses in committed files). The same
id must appear under `channels[]` — the two-place rule from `docs/QUICKSTART.md` step 7.

## Explicitly NOT here

- **No send path at any layer** — no send authorization exists for this platform; the
  transport default-denies everything outside `getMe` / `getUpdates` / `getChat`.
- **The incumbent host's HMAC bridge, reply watcher, and live chat id** — out of scope
  by design; this adapter is a fresh, config-driven client and must never share the
  live bridge's queue (two `getUpdates` consumers on one token conflict).
- **Live smoke is operator-owned**: it needs a bot token and a chat the operator
  designates, and the first live run must use a throwaway bot — a shared token would
  steal updates from the production bridge.
