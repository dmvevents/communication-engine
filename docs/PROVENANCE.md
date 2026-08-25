# PROVENANCE — where each module's design comes from

Every part of this engine generalizes something that already runs in production on the origin
host. This file maps planned modules to their battle-tested ancestors — **paths only, no content,
no identifiers**. The originals stay where they are; this repo re-implements the *patterns*.

| Planned module | Origin (on the origin host) | What it proved |
|---|---|---|
| `core/engine.py` | `jarvis-slack/monitor.py` | 15s gap-free cursored polling, idempotent ingest (INSERT OR REPLACE), heartbeat file contract; container up for weeks |
| `core/store.py` | `slack/db.py` | working schema: messages / users / channels / reactions / files / sync_state / workspaces |
| `core/triggers.py` | `jarvis-slack/monitor.py` (classify_tier) | mention/keyword triggers + tier-1(info)/tier-2(work) classification |
| `core/outbox.py` | the staged-send discipline in the colleague-supervisor loop | draft → staged file → operator gate → send; read-back verification after send |
| `core/watchdog.py` | `jarvis-slack/watchdog.sh` + `telegram-bridge/watchdog.sh` (near-identical mirrors) | check registry, consecutive-fail threshold, alert cooldown, maintenance flag, quiet-channel vs dead-monitor disambiguation |
| `core/supervisor.py` | the colleague-supervisor command loop (cron, 15-min cadence) | flock single-instance, idempotent cursor, idle backoff, byte-identical-outcome alarm, cursor auto-reconcile with proof-of-reply, detached bounded responder sessions |
| `core/probe.py` | that loop's per-minute connection probe | cheap-probe/expensive-responder split; edge-only alerting ("who monitors the monitor"); per-minute probe doubling as new-message trigger (~1-min detection at zero LLM cost) |
| `core/dashboard.py` | the comms-spoke monitors catalog + dashboard | monitors.json schema → issues-style rendered dashboard |
| `channels/slack/` | `slack/mcp_server.py` + `jarvis-slack/monitor.py` | 12-tool surface over db-mirror + live API; loopback HMAC post bridge |
| `channels/telegram/` | `telegram-dispatcher/` + `session-router/` | inbound envelope routing to consumers; marker-line reply scraping; loopback HMAC bridge |
| `watchers/stack-watchdog.sh` | the two watchdog mirrors above | same 7-check skeleton parameterized per adapter |

## Defects the origin system taught us (encode as regression tests, phase 1)

1. **A health check that can neither pass nor fail is a defect.** The origin stack's inbox-staleness
   check read a field name that had drifted out of the event schema; it silently contributed
   nothing for weeks while the summary line kept reading "all checks passed". Phase 1 pins the
   event schema and requires every check to emit exactly one of PASS/FAIL — a no-op emission is
   itself a test failure.
2. **A send-path watcher whose input source vanished is a zombie, not a safe default.** The origin
   host has a reply-scraper service that outlived its target; it is inert only by accident and
   would reactivate silently if the target name were ever reused. Phase 1's watcher framework
   requires send-path watchers to verify their *source* exists and alert when it doesn't.
