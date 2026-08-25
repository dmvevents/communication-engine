# communication-engine

Channel-agnostic communication engine for agent fleets: **poll, store, classify, stage, watch** —
across any messaging platform. Slack adapter first, Telegram second; Outlook/Teams/email are
adapters to add, not rewrites (nothing in this repo is named after a platform except the adapters
themselves).

## Why

An agent fleet already talks through multiple channels (chat workspaces, messenger bridges,
soon email). Each grew its own poller, watchdog, send path, and staging discipline — mirrored
copies of the same five ideas. This repo turns the mirroring into a contract:

- `core/` — the engine: cursored gap-free polling, message store, trigger/tier classification,
  **stage-first outbox** (sending is gated, never a default), watchdog framework, supervisor and
  probe loop patterns, dashboard rendering.
- `channels/` — one adapter per platform, all implementing [`channels/CONTRACT.md`](channels/CONTRACT.md).
- `watchers/` — the meta-monitoring (is every loop firing?).

Every module generalizes something already running in production on the origin host — see
[`docs/PROVENANCE.md`](docs/PROVENANCE.md).

## Security model (non-negotiable)

1. **No secrets in this repo, ever** — not in code, not in docs, not as scan patterns.
   `scripts/sanitize-gate.sh` enforces this in CI and in local hooks
   (`scripts/install-hooks.sh`), and proves itself with `--self-test`.
   Host-specific literals (chat ids, account ids, real channel ids) belong in a local,
   gitignored denylist the gate also applies.
2. **Read-only by default.** Reply policy per target is configuration
   (`never` / `staged` / `direct`), and the outbox stage-gate lives in core — adapters cannot
   bypass it.
3. **No listening ports.** Outbound calls and loopback-bound HMAC bridges only.
4. Real config is `settings.json` (gitignored). The only config file committed is
   [`settings.example.json`](settings.example.json), all values placeholders or `env:NAME` references.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Secret-free skeleton: layout, adapter contract, sanitize gate + CI, provenance | **this commit** |
| 1 | Slack adapter, read-only (engine + store + poll/resolve/health) + regression tests incl. the silent-health-check class | HELD |
| 2 | Dashboard generator (monitors catalog → rendered dashboard) | HELD |
| 3 | Telegram adapter (proves the contract: two platforms, zero core changes) | HELD |
| 4 | Outbox/staging with operator gate (`auto_send: never` default) | HELD |
| 5 | Outlook/email adapter (validates capability degradation) | HELD |

Held phases each require an explicit GO from the fleet hub.

## Quickstart (once phase 1 lands)

```sh
cp settings.example.json settings.json   # then edit; settings.json is gitignored
scripts/install-hooks.sh                 # pre-commit + pre-push sanitize gate
scripts/sanitize-gate.sh --self-test     # prove the gate on this machine
```

## License

Private repository; licensing intentionally deferred until/unless visibility ever changes.
