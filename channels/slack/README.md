# channels/slack — first adapter

**Phase 1 (HELD).** Stub only in phase 0.

Planned files, all implementing [`../CONTRACT.md`](../CONTRACT.md):

- `adapter.py` — `poll` via `conversations.history` with ts cursors; `resolve` for channels/users;
  `search` via `search.messages` when the token has the scope; `health` via `auth.test`.
- `bridge.py` — optional loopback-bound HMAC-authenticated post bridge for co-located processes.
- `mcp_server.py` — optional MCP tool surface over the local message store + live API.

Capabilities: `{read: true, history: true, search: token-dependent, send: true, react: true, threads: true}`.

Auth: `env:` reference from `settings.json` only. No token literal ever appears in code, config
examples, or tests (CI sanitize gate enforces).
