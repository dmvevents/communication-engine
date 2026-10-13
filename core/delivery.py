"""core/delivery — the one exception an adapter's read_back() may raise (ENH-31 guard 3).

`read_back(target, key, ...)` answers True (the message carrying this key is on the
platform, with the approved text), False (no message carries this key), or raises
DeliveryMismatch: a message carrying this key IS on the platform, but its text is NOT
the approved text — truncated, split, rewritten. The outbox must treat the three
differently: False re-sends (recovery), True commits, a mismatch parks the row in a
terminal MISMATCH state for a human. Before this existed, a mismatch read as False and
recovery posted the reply again (independent review of ENH-31, F3). Lives apart from
core/outbox so an adapter can raise it without importing the send layer.
"""


# The marker an adapter attaches to the engine's own posts (Slack: message metadata
# event_type), so a read-back can find them by key and so the DM rule can recognise a
# message the engine itself delivered — one that was tied to its ask at send time and
# must never be handed to the next older open ask (review of ENH-31, F1 addendum).
ENGINE_EVENT_TYPE = "comms_engine_reply"


class DeliveryMismatch(RuntimeError):
    """A message with this key is on the platform; its text is not the approved text."""


class NotPosted(RuntimeError):
    """The platform ANSWERED the post with a DEFINITE refusal (Slack: an ok:false such as
    not_in_channel, is_archived, msg_too_long — an allowlist the adapter keeps), so nothing
    is on the platform. The outbox hands such a draft back to the gate instead of leaving it
    in INTENT with no exit (independent review of ENH-31, round 2, R2-8). A timeout, a
    connection error, an internal_error / fatal_error, or a non-JSON 5xx is NOT this: the
    post may have landed (round 3, R3-3 — raising it for those made the next click post a
    duplicate), and only a read-back can tell, so those stay plain adapter errors."""


class TargetGone(RuntimeError):
    """A read-back (or re-send) failed because the target itself is gone for good: the
    thread's parent was deleted, the channel no longer exists or is archived. Recovery
    parks such a row for a human at once — nothing will ever place this reply (review F7)
    — while every other read-back error is treated as transient and simply retried, never
    parked and never posted (round 3, R3-5 a: three outage runs used to park a reply that
    was never sent)."""
