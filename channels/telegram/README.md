# channels/telegram — second adapter

**Phase 3 (HELD).** Stub only in phase 0.

Purpose beyond its own utility: proving the contract — a second platform must slot in with
**zero `core/` changes**, or the contract is wrong.

Planned files:

- `adapter.py` — bot-API polling (`getUpdates` offset as the opaque cursor); `health` via `getMe`;
  send primitive gated by `core/outbox` like every adapter.
- `reply_watcher.py` — generalized marker-line scraper (watch a text stream for
  `<MARKER>: ...` lines and forward via the outbox), with a hard rule inherited from a real
  incident: the watcher must verify its *source* still exists and alert when it doesn't
  (zombie-watcher class, see `docs/PROVENANCE.md`).

Capabilities: `{read: true, history: limited, search: false, send: true, react: false, threads: false}`
— exactly the degradation case the engine must handle gracefully.
