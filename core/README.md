# core/ — the channel-agnostic engine

**Phase 1+ (HELD).** No implementation code lands here in phase 0 — this stub pins the module
plan so the layout is reviewable before any code exists. Import rule for everything in this
directory: **no platform imports** — `core/` may import from `channels/CONTRACT` types only,
never from a concrete adapter.

| Module | Responsibility |
|---|---|
| `engine.py` | poll scheduler: per-(instance, channel) opaque cursors, gap-free idempotent ingest, heartbeat file |
| `store.py` | sqlite message store; PK `(channel_type, channel_id, ts)`; schema pinned by a conformance test |
| `triggers.py` | mention / keyword / principal rules; tier classifier (info vs work) |
| `outbox.py` | stage-first send discipline: draft → `state/outbox/` → operator gate → adapter send → read-back verify. The ONLY caller of `adapter.send()` |
| `watchdog.py` | check registry; every check MUST emit PASS or FAIL — a check that emits neither fails the run (regression: silent no-op class, see `docs/PROVENANCE.md`) |
| `supervisor.py` | the command-loop pattern: observe → log → dashboard → respond → decide-execute; flock, backoff, cursor auto-reconcile with proof-of-reply |
| `probe.py` | cheap per-minute liveness + new-message trigger; edge-only alerting |
| `dashboard.py` | monitors catalog (JSON) → rendered issues-style dashboard |
