"""core/ratelimit.py — per-(instance, method) rate-limit back-off (ENH-1).

The platform contract this encodes (researched for ENH-1 at
docs.slack.dev/apis/web-api/rate-limits): a 429 applies to THAT method for THAT
workspace — "calls to other methods on behalf of this workspace are not restricted" —
and the response's Retry-After header says exactly how long to wait. Two consequences
are load-bearing:

  * Hold state is keyed (instance, method). A global back-off would let a read 429 on
    a history poll pause the send method — the engine would go quiet on a customer
    BECAUSE it was reading too fast.
  * Per-method limits are unpublished and change; they are DISCOVERED from Retry-After
    on each 429, never encoded as a table of assumed budgets. The wait is the
    platform's number, exactly — no padding, no exponential escalation.

State is deliberately in-memory: Retry-After horizons are seconds, so a restarted
process re-learns a hold from the platform's next 429 at the cost of one request.
The never-lose-a-message guarantee does NOT live here — it lives in the durable outbox
INTENT ladder. This module's share of it is narrower: a 429 is always retried or
re-raised, never swallowed (see Backoff.call).

Method names are opaque strings to core, exactly like cursors — the adapter picks them.
"""
from __future__ import annotations

import math
import time
from typing import Callable


class RateLimited(Exception):
    """A platform answered 429. Adapters raise this carrying the Retry-After header
    value; Backoff.call re-raises it once retries are exhausted, so a caller always
    sees either the result or this exception — a 429 has no silent exit path."""

    def __init__(self, retry_after, instance: str | None = None,
                 method: str | None = None):
        self.retry_after = _seconds(retry_after)
        self.instance = instance
        self.method = method
        where = f" on {instance}/{method}" if instance or method else ""
        super().__init__(f"rate limited{where}: retry after {self.retry_after}s")


def _seconds(retry_after) -> float:
    """Retry-After as float seconds. HTTP delivers the header as a string, so strings
    are accepted; an adapter for a platform that sends HTTP-dates converts before
    raising — core parses no date formats. Junk fails loudly HERE, at the boundary the
    header entered: a NaN admitted into the clock arithmetic compares false against
    everything and the hold silently never engages."""
    try:
        s = float(retry_after)
    except (TypeError, ValueError):
        raise ValueError(
            f"Retry-After is not a number of seconds: {retry_after!r}") from None
    if not math.isfinite(s) or s < 0:
        raise ValueError(f"Retry-After is not a usable wait: {retry_after!r}")
    return s


class Backoff:
    """Per-(instance, method) hold state, honouring Retry-After exactly.

    `clock` and `sleep` are injectable so the engine's scheduler (and the tests)
    control time; production defaults are the real ones.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._ready_at: dict[tuple, float] = {}

    @staticmethod
    def _key(instance: str, method: str) -> tuple:
        # THE scope decision (module docstring): one method's 429 must never widen to
        # its siblings, and one workspace's must never widen to another's.
        return (instance, method)

    def note(self, instance: str, method: str, retry_after) -> None:
        """Record a 429: (instance, method) is held for EXACTLY the platform's wait."""
        self._ready_at[self._key(instance, method)] = (
            self._clock() + _seconds(retry_after))

    def ready_in(self, instance: str, method: str) -> float:
        """Seconds until (instance, method) may be called; 0.0 when callable now.
        Expired holds need no cleanup — they read as 0.0 forever."""
        ready = self._ready_at.get(self._key(instance, method))
        if ready is None:
            return 0.0
        return max(0.0, ready - self._clock())

    def call(self, instance: str, method: str, fn: Callable,
             max_tries: int = 3):
        """Run fn under the back-off discipline: wait out any active hold, call, and
        on a RateLimited record the new hold and retry — up to max_tries calls of fn.

        When tries are exhausted the LAST RateLimited is re-raised: surfaced, so the
        caller's durable state (an outbox INTENT row, a poll cursor that did not
        advance) is what carries the work to the next attempt. Exceptions other than
        RateLimited propagate untouched — real faults keep their own handling.
        """
        if max_tries < 1:
            raise ValueError("max_tries must be >= 1 — zero tries would silently "
                             "drop the call")
        for _ in range(max_tries):
            delay = self.ready_in(instance, method)
            if delay > 0:
                self._sleep(delay)
            try:
                return fn()
            except RateLimited as ex:
                self.note(instance, method, ex.retry_after)
                last = ex
        raise last
