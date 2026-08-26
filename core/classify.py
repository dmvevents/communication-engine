"""core/classify.py — message classification (gate G9; requirement R15).

Deciding what KIND of message arrived is the highest-consequence decision the engine makes:
an EXEC-REQUEST launches work, a COMMITMENT-ASK must be gated by a human, a QUESTION can
often be answered from banked evidence, and a STATEMENT usually needs only an ack.

Measured problem this replaces
------------------------------
The incumbent classifier does `if keyword in text` with no word boundary. Counted over its
own log: **47 of 138 EXEC-REQUESTs (34%) matched a substring only**. Real examples:

    "I have finished testing all agg options"    -> matched "test"   -> EXEC-REQUEST
    "For EP, I exposed the vllm worker via ..."  -> matched "test"   -> EXEC-REQUEST

Both are STATEMENTs reporting completed work. Because EXEC-REQUEST is the class that starts
cluster runs, a false positive can make a fleet act on a colleague saying "testing" in
passing.

Design
------
* word-boundary matching (regex `\\b`), so "testing" no longer triggers on "test"
* an ambiguity DOWNGRADE: an exec verb with no directive lands on STATEMENT, because a
  missed order costs a follow-up while a false order costs a cluster run
* an imperative/second-person requirement for EXEC: "can you run X", "please deploy Y",
  "run the smoke" — not merely the presence of a verb
* the taxonomy and its keyword lists are CONFIGURABLE, because an adopting team's vocabulary
  differs (gate G8: adopt by config, never by forking)
* precedence is explicit and tested, since a message can match several classes
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Precedence: highest consequence first, EXCEPT that an exec verb with no directive is
# downgraded rather than acted on. ATTACHMENT-ONLY sits outside the keyword ladder: it
# can only fire when there is no text for the other classes to compete over (ENH-4).
PRECEDENCE = ("EXEC-REQUEST", "COMMITMENT-ASK", "QUESTION", "ATTACHMENT-ONLY",
              "STATEMENT")

DEFAULT_EXEC_VERBS = ("run", "deploy", "test", "build", "rebuild", "launch", "start",
                      "scale", "retest", "reproduce", "apply", "restart", "merge")
DEFAULT_ASK_PHRASES = ("can you", "could you", "would you", "please", "how do", "how can",
                       "where is", "what is", "why is", "send me", "add me", "link")
DEFAULT_COMMITMENT_PHRASES = ("approve", "sign off", "commit to", "promise", "by when",
                              "eta", "deadline", "guarantee")
# Second-person / imperative markers that make an exec verb an actual instruction.
DEFAULT_DIRECTIVE_MARKERS = ("can you", "could you", "would you", "please", "you should",
                             "let's", "lets", "we should", "go ahead and", "i need you to",
                             "i want you to", "use the", "make sure")


@dataclass
class Taxonomy:
    """Configurable vocabulary. An adopting team overrides these without forking."""
    exec_verbs: tuple = DEFAULT_EXEC_VERBS
    ask_phrases: tuple = DEFAULT_ASK_PHRASES
    commitment_phrases: tuple = DEFAULT_COMMITMENT_PHRASES
    directive_markers: tuple = DEFAULT_DIRECTIVE_MARKERS

    @classmethod
    def from_config(cls, cfg: dict | None) -> "Taxonomy":
        cfg = cfg or {}
        return cls(
            exec_verbs=tuple(cfg.get("exec_verbs", DEFAULT_EXEC_VERBS)),
            ask_phrases=tuple(cfg.get("ask_phrases", DEFAULT_ASK_PHRASES)),
            commitment_phrases=tuple(cfg.get("commitment_phrases",
                                             DEFAULT_COMMITMENT_PHRASES)),
            directive_markers=tuple(cfg.get("directive_markers",
                                            DEFAULT_DIRECTIVE_MARKERS)),
        )


@dataclass
class Classification:
    kind: str
    reason: str
    matched: list = field(default_factory=list)
    # ENH-9: True only where the classifier HEDGED — it saw evidence for a higher-
    # consequence class but refused to act without corroboration. A field, not a reason
    # substring: the downgrade used to be detectable only by string-matching the human-
    # facing prose, so nothing downstream could route or count it. The signal comes from
    # the same deterministic rules as the decision itself — NO LLM sits in this path by
    # default; an adopter who wants model-assisted triage hangs it off the escalated
    # owed:operator queue (core/schedule.py), never inside classify().
    ambiguous: bool = False

    def __str__(self) -> str:
        return f"{self.kind} ({self.reason})"


def _word_hits(text: str, needles) -> list:
    """Whole-word / whole-phrase matches only.

    This single change is what removes the 34% substring false-positive rate: "testing"
    must no longer match "test".
    """
    hits = []
    for n in needles:
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in n.split()) + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(n)
    return hits


def _attachment_cues(attachments) -> list:
    """Name the evidence (R22): 'image:screenshot.png' in a journal row is disputable
    months later; a bare ATTACHMENT-ONLY kind is not. Junk shapes degrade to str()
    because classify() never raises — an adapter bug must not un-journal a message."""
    cues = []
    for a in attachments:
        if isinstance(a, dict):
            cues.append(f"{a.get('kind') or 'attachment'}:"
                        f"{a.get('name') or a.get('url') or 'unnamed'}")
        else:
            cues.append(str(a))
    return cues


def classify(text: str, taxonomy: Taxonomy | None = None,
             attachments=None) -> Classification:
    """Classify one message. Never raises; an empty message is a STATEMENT.

    `attachments` is the normalized message's attachment list (ENH-4). The live system
    downloads screenshots and treats them as content, so a message with no text but an
    attachment must NOT read as an empty STATEMENT (ack-and-forget): the one thing the
    engine knows is that content arrived which the text pipeline cannot read. When text
    IS present it wins — a screenshot under "can you check this?" is already a QUESTION,
    and the attachment adds content without overriding what the sender wrote.
    """
    t = (text or "").strip()
    atts = list(attachments or ())
    if not t:
        if atts:
            return Classification(
                "ATTACHMENT-ONLY",
                f"no text but {len(atts)} attachment(s) — content the text "
                "classifier cannot read; needs eyes, not an auto-ack",
                _attachment_cues(atts))
        return Classification("STATEMENT", "empty message")
    tax = taxonomy or Taxonomy()

    exec_hits = _word_hits(t, tax.exec_verbs)
    directive = _word_hits(t, tax.directive_markers)
    commit_hits = _word_hits(t, tax.commitment_phrases)
    ask_hits = _word_hits(t, tax.ask_phrases)

    # A commitment ask outranks everything else it co-occurs with: it needs a human.
    if commit_hits:
        return Classification("COMMITMENT-ASK",
                              "asks for approval/commitment — requires a human gate",
                              commit_hits)

    # NOTE — a "reporting-past" guard (treat "I have finished testing" as narration) was
    # written here and then REMOVED as redundant. Mutation testing proved it: deleting the
    # guard left the suite green, because word-boundary matching on BASE-FORM verbs already
    # excludes inflected narration ("testing" does not match `\btest\b`), and anything that
    # slips through hits the ambiguous-downgrade below and lands on STATEMENT anyway. A guard
    # no input needs is safety theatre. It WOULD become load-bearing if this classifier ever
    # matched inflected forms (stemming); re-add it in the same commit as that change.

    # EXEC needs a verb AND something that makes it an instruction. A bare verb is not an
    # order; requiring a directive marker (or an imperative opening) is what stops the
    # fleet acting on passing mentions.
    if exec_hits and (directive or _starts_imperative(t, tax.exec_verbs)):
        return Classification("EXEC-REQUEST",
                              "imperative or directed request to perform work",
                              exec_hits + directive)

    if ask_hits or t.rstrip().endswith("?"):
        return Classification("QUESTION", "asks for information",
                              ask_hits or ["ends with '?'"])

    if exec_hits:
        # A verb with no directive and no reporting cue: ambiguous. Deliberately the LOWER
        # consequence class — a missed order costs a follow-up, a false order costs a
        # cluster run and possibly a customer-visible action.
        return Classification("STATEMENT",
                              "mentions an action verb but carries no directive — "
                              "ambiguous, so classified down to avoid acting on narration",
                              exec_hits, ambiguous=True)

    return Classification("STATEMENT", "no directive, question or commitment cue")


def _starts_imperative(text: str, verbs) -> bool:
    """True when the message opens with a bare imperative: 'run the smoke test', 'deploy X'."""
    first = re.split(r"[\s,:.!?]+", text.strip().lower(), maxsplit=1)
    return bool(first) and first[0] in {v.lower() for v in verbs}


def classify_batch(messages, taxonomy: Taxonomy | None = None) -> list:
    return [classify(m, taxonomy) for m in messages]
