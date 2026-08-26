# core/ — the channel-agnostic engine

Implemented and tested. Every module below ships with tests that fail if its load-bearing
property is removed, and `tests/mutation_check.sh` deletes those properties one at a time to
prove the tests have teeth. This table is itself tested (`tests/test_docs.py`): a row citing
a module that does not exist, or a module that ships without a row here, turns the suite red —
this file listed six phantom modules while omitting the implemented ones before that check
existed (ENH-19).

Import rule for everything in this directory: **no platform imports** — `core/` never imports
a concrete adapter. Adapters are discovered and loaded from configuration (`core/config.py`),
so a new channel type is a directory drop with zero core changes.

| Module | Responsibility |
|---|---|
| `checks.py` | health checks that cannot silently no-op: every check emits PASS or FAIL, a None or junk verdict becomes FAIL, and a PASS that inspected nothing is refused (the incumbent watchdog read "OK" for weeks while checking a field its events never carried) |
| `classify.py` | message classification (EXEC-REQUEST / COMMITMENT-ASK / QUESTION / STATEMENT): word-boundary keyword matching (the incumbent's substring match misfired on 34% of its EXEC-REQUESTs), a commitment ask always outranks a work request, and every decision records the cues that produced it |
| `config.py` | build the whole engine from `settings.json` alone: adapter discovery, `env:NAME` credential references (literal credentials refused at load), default-deny reply policy, relative paths resolved against the config directory |
| `escalate.py` | edge-triggered operator escalation: notify on a state CHANGE, never on a state (a level-triggered once-a-minute probe is 1,440 identical alerts a day); edge state lives in sqlite so every cron-shaped poll sees it; recovery is announced, not just recorded |
| `journal.py` | idempotent audit journal: one row per distinct message however often it is re-seen (the incumbent log held 323 entries for 177 messages), edits detected by content and kept as revisions, classification cues preserved for dispute |
| `outbox.py` | stage-first send discipline and the ONLY caller of `adapter.send()`: durable INTENT → send → read-back VERIFY → COMMIT, so a crash at any seam recovers to exactly-once; staged targets stop at the operator gate; sends paced at ≤1 message/second per channel |
| `owed.py` | the owed-work edge: promised-but-unstarted work stays visible, idle backoff can never suppress it (root cause of a measured 8h17m silent stall), and a recorded driver must be provably alive, not just named |
| `parity.py` | shadow-mode parity differ against the incumbent oracle: reports missed / extra / cursor divergence, and an empty comparison is an ERROR, never a pass |
| `ratelimit.py` | per-(instance, method) 429 back-off honouring Retry-After exactly: a read 429 never pauses sends, one workspace's hold never crosses to another, and exhausted retries surface the error instead of dropping the message |
| `store.py` | sqlite message store: explicit pinned schema (unknown and missing fields refused), idempotent re-ingest, per-(instance, channel) cursor persistence |

Each module's docstring carries the measured incident it exists to prevent — read it before
changing behaviour.
