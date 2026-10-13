"""core/redact — the BLOCK-tier payload scan every outward message passes (ENH-31, guard 5).

The sanitize gate (scripts/sanitize-gate.sh) keeps credentials out of the REPO; this
module keeps them out of what the engine would POST. Same pattern classes for the
credential tier, written by concatenation so this file's own source can never match
them. Deliberately NOT the gate's identifier tier (Slack ids, IPv4): a reply may
legitimately name a channel id or a loopback URL, and a scan that refuses "here:
http://127.0.0.1:8502" would refuse the answer the dashboard exists to give.

`scan(text)` names the classes hit and never returns the match itself — a refusal
message that echoed the secret would move it into a log.
"""
from __future__ import annotations

import re

# (class, pattern). Each pattern's literal prefix is split so the source cannot match.
BLOCK_PATTERNS = (
    ("slack-token", re.compile("xo" + "x[abprs]-[0-9A-Za-z-]{8,}")),
    ("slack-app-token", re.compile("xa" + "pp-[0-9]-[0-9A-Za-z-]{8,}")),
    ("telegram-bot-token", re.compile(r"\b[0-9]{8,10}:" + "AA[0-9A-Za-z_-]{30,}")),
    ("aws-access-key", re.compile(r"\b(AK" + r"IA|AS" + r"IA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile("BEGIN [A-Z ]*PRIV" + "ATE KEY")),
    ("assigned-secret", re.compile(
        r"(SECRET|TOKEN|PASSWD|PASSWORD|API_KEY)=[\"']?[A-Za-z0-9+/=_-]{16,}")),
)


def scan(text: str | None) -> list:
    """Class names of every BLOCK-tier credential shape found in `text`, in pattern
    order, each once; an empty list means the payload may leave the host."""
    if not text:
        return []
    return [name for name, pat in BLOCK_PATTERNS if pat.search(text)]
