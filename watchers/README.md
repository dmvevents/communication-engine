# watchers/ — meta-monitoring

**Phase 1+ (HELD).** Stub only in phase 0.

Planned: `stack-watchdog.sh` — one parameterized watchdog for any adapter stack (the origin host
runs two hand-mirrored copies of the same script for two platforms; this folds them into one).

Check skeleton (from the proven originals):

1. process/container running and healthy
2. service unit active
3. bridge reachable on loopback AND rejects a bad HMAC (proves the auth path, not just liveness)
4. heartbeat fresh (< 2× poll interval)
5. store has messages (ingest happened at least once)
6. last-message age vs quiet-threshold, with a live API probe to distinguish "quiet channel"
   from "dead monitor"
7. inbox not stuck (oldest unacked event age) — **must emit PASS or FAIL, never silently no-op**

Failure discipline: consecutive-fail threshold before alerting, alert cooldown, maintenance
flag to silence during planned work, alert on recovery edge too.
