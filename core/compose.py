"""core/compose.py — evidence-citation discipline for replies (ENH-10).

Measured problem this makes structural
--------------------------------------
The origin system's strongest reply rule is enforced by a HUMAN: "never improvise a
number — cite banked artifacts". Its reply guidance says to cite specific things (file
paths, commit hashes, exact figures) precisely because a plausible invented number sent
in the operator's name to a customer channel is unrecoverable — there is no read-back
ladder for a wrong figure the way there is for a duplicated message. Meanwhile
`Outbox.send()` is deliberately content-blind: it delivers whatever string it is handed.
Nothing between "I want to reply" and "here is the string" ever checked the rule.

This module is that layer. A reply is COMPOSED from parts, and the parts are policed:

* `prose(text)`   — words only. Any digit-bearing token is refused: a number can only
                    enter a reply as a claim.
* `claim(text, cite=...)` — requires a citation; every cited ref must be BANKED, and
                    every number in the claim must actually occur in the cited evidence
                    (ref or content). A real citation on an invented figure is
                    laundering, not discipline, and is refused the same way.
* `render()`      — the citation is written INTO the reply ("... (per bench.log)"), so
                    the recipient can check the figure. Bookkeeping the wire never
                    carries protects nobody.

The bank is (ref -> artifact content), supplied by the caller: what counts as banked
evidence is the adopter's judgment (a log excerpt, a commit line, a benchmark row);
that the reply's numbers CAME from it is this module's.

Honest scope: "factual claim" here is the machine-checkable subset — digit-bearing
tokens. A false claim written entirely in words is the composer's judgment; the layer
raises the floor, it is not a lie detector. Refusal always fails toward the human:
rephrase without the number, or bank the artifact that proves it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# One definition of "a number" for BOTH sides of the check — claims and artifacts must
# tokenize identically or coverage matching breaks. `.` and `:` join digits (decimals,
# versions, times: 0.92 / 1.35.0 / 03:35 stay whole tokens); a comma SPLITS, because
# "1,440" (grouping) and "options 1,2" (a list) are indistinguishable — splitting is
# consistent on both sides, so either formatting of the same figure still matches.
NUMBER = re.compile(r"\d+(?:[.:]\d+)*")


class CitationError(ValueError):
    """A factual claim without covering banked evidence. Never downgrade to a warning:
    the whole point is that the string is refused BEFORE it exists to be sent."""


def factual_claims(text: str) -> list[str]:
    """Every digit-bearing number token in `text`, in reading order."""
    return NUMBER.findall(text or "")


@dataclass
class _Part:
    text: str
    refs: tuple  # empty for prose


class Composer:
    """Builds one reply from prose and cited claims; `render()` yields the string."""

    def __init__(self, bank):
        # Mapping of ref -> banked artifact content. Held, not copied: the caller may
        # keep banking evidence between claims.
        self.bank = bank
        self._parts: list[_Part] = []

    def prose(self, text: str) -> "Composer":
        numbers = factual_claims(text)
        if numbers:
            raise CitationError(
                f"prose carries factual claims ({', '.join(numbers)}) — a number can "
                "only enter a reply as a claim with a citation to a banked artifact "
                "(or rephrase without it)")
        self._parts.append(_Part(text, ()))
        return self

    def claim(self, text: str, cite) -> "Composer":
        refs = (cite,) if isinstance(cite, str) else tuple(cite or ())
        if not any(refs):
            raise CitationError(
                f"claim {text!r} has no citation — every claim must cite a banked "
                "artifact")
        unbanked = [r for r in refs if r not in self.bank]
        if unbanked:
            raise CitationError(
                f"claim {text!r} cites artifacts that are not banked: "
                f"{', '.join(map(repr, unbanked))} — bank the evidence first")
        # The ref is part of the banked entry ("commit 4b41428" names its own digits),
        # so it counts toward coverage alongside the content.
        banked = set()
        for r in refs:
            banked.update(factual_claims(r))
            banked.update(factual_claims(self.bank[r]))
        improvised = [n for n in factual_claims(text) if n not in banked]
        if improvised:
            raise CitationError(
                f"claim {text!r} carries numbers not found in the cited evidence "
                f"({', '.join(improvised)} missing from {', '.join(refs)}) — an exact "
                "figure must come from a banked artifact, not from memory")
        self._parts.append(_Part(text, refs))
        return self

    def render(self, sep: str = " ") -> str:
        """The reply string, citations written in — what gets handed to the outbox."""
        return sep.join(
            f"{p.text} (per {', '.join(p.refs)})" if p.refs else p.text
            for p in self._parts)
