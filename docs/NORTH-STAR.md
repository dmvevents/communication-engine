# North star — what this product is, and what it refuses to be

One paragraph, then the priorities that break ties, then the things that would falsify it.

## The product

**A channel-agnostic engine that lets an autonomous agent fleet take part in human
communication channels without ever silently losing an incoming message or silently sending an
outgoing one.** It polls, stores, classifies, journals and *stages* — across any messaging
platform, one adapter directory per platform. Sending is a gated exception, never a default.
It is not a chatbot, not a framework, and not a Slack tool: nothing in it is named after a
platform except the adapters.

The problem it exists for: a fleet already talks through several channels, and each grew its own
poller, watchdog, send path and staging discipline — mirrored copies of the same five ideas, each
with its own silent-failure mode. This turns the mirroring into a contract.

## Priorities, in strict order

When two of these conflict, the earlier one wins. That ordering is the actual design.

1. **Never send what nobody authorized.** One code path reaches a platform's `send()`. Reply
   policy is configuration and defaults to `never`; an unlisted target cannot be sent to.
   A staged reply stops at a human.
2. **Never lose an incoming message silently.** Gap-free cursored polling, a pinned message
   schema that refuses unknown *and* missing fields, and no check that can neither pass nor fail.
3. **Every load-bearing property has a test that fails if the property is removed.** Mutation
   testing is how that claim is kept honest — a surviving mutation is a defect in the tests.
   Read as scoped, not absolute: it covers every property that *ships*. Requirements still open
   are open, and the gate count says so.
4. **Adoptable by configuration alone.** A new channel type is a directory drop with zero `core/`
   changes; a new adopter edits one config file and exports one env var.
5. **Surfaces tell the truth.** No number on any surface is hand-maintained. Understating reality
   is treated as the same defect as overstating it.

## What "done" is measured against

Twelve gates, each backed by requirements with acceptance criteria, tracked on a board that is
synced from machine-readable sources of truth rather than edited by hand. A gate passes only when
its requirements do, and a requirement is met only with cited evidence (commit + test + where
applicable a live run).

## What this deliberately does NOT do

Stated because a north star that only lists ambitions is unfalsifiable:

* **No live send path ships.** The exactly-once outbox ladder is complete and fault-injected, but
  the only adapter exposing `send()` is the in-memory test double. Both real adapters have no
  send primitive at any layer.
* **No non-loopback listeners.** The engine itself makes outbound calls only. The one surface that
  *does* listen — the operator dashboard — binds `127.0.0.1` and is reached over an SSH tunnel,
  never an open port. Stated this way because the older phrasing ("no listening ports") next to
  "loopback-bound surfaces" is a contradiction two independent reviewers derived unprompted: a
  browser-tested UI plainly serves HTTP, and a claim a reader can falsify in one step damages the
  claims that are true.
* **The operator dashboard is IN scope, as a control plane — not a product surface.** It is
  read-only over the adopter's own journal and outboxes, and it exists because a human-gated
  product whose human has nowhere to look is not gated, it is stalled. It is deliberately not a
  place where messages are composed.
* **It is not a mirror of the platform.** The store is an **archive**: it keeps what it ingested
  even after the platform deletes it. This is a deliberate choice with consequences — see below.
* **It does not decide.** Classification routes and stages; a human gates anything outward-facing.

## The three findings that most shaped it

Each is a measured incident, not a principle someone liked.

1. **A check that can neither pass nor fail is worse than no check.** The incumbent watchdog read
   a field its events never carried and reported "OK — 7 checks passed" for weeks. So a PASS must
   name how many items it inspected, and inspecting zero is refused at construction.
2. **Detection after the fact is not a fix for a dual write.** A send that lands but is not
   recorded reconciled itself 24 times in the incumbent. So durability comes before the
   observable action: INTENT is committed before the adapter is touched, and recovery resumes
   from INTENT via read-back, never from a cursor.
3. **The incumbent is an archive, not ground truth.** A parity run reported losing two-thirds of a
   channel; the platform's own answer was that the engine held 100% of retrievable history and the
   "missing" messages had been deleted upstream. So comparison has three sides — what the platform
   serves *now*, what the incumbent archived, what the engine stored — and only one class of
   divergence is a defect. That class can never be waived by configuration.

Finding 3 has a consequence the product is stuck with: **history the platform has deleted is
unrecoverable by any engine.** So a cutover cannot be "backfill, then switch" — the incumbent
store must be retained as the archive of record for everything before switchover.

## What an independent panel said this document gets wrong (2026-08-27)

Three frontier models were given this document, a neutral repo summary and an abstracted directive
history, and asked to **refute** it. Recorded here because a north star that hides its own review is
back to being unfalsifiable.

* **All three refused the framing above.** None called this a "communication engine"; each
  independently led with *safety layer* / *staging layer* / *defensive proxy*. They are right, and
  priority 1 above already says so — the opening sentence leads with participation when the
  identity is what it **withholds**.
* **"Channel-agnostic" was unproven at review time** (3/3, and the panel's #1 risk). It is now
  proven: a second platform that disagrees structurally with the first landed as a true directory
  drop with **zero `core/` changes**. A third, with a non-orderable id space, needed one *generic*
  capability generalized in the comparison layer — recorded as finding F-4, and the reason priority 4
  is scoped to the ingestion path rather than claimed for every layer.
* **"GA" is not yet defined** for a product whose differentiating capability does not ship, and
  there is no stated default for what happens when an operator decision goes unanswered. Open.
* **The human approval surface is the least-specified, highest-stakes seam.** Drafts are listed and a
  human is pointed at them, but **nothing here promotes a staged draft to sent**. Priority 1's
  enforcement point is therefore still a design, not an implementation. Open, and tracked.

## How to falsify this document

* Find a way to reach a platform `send()` without passing the outbox stage-gate.
* Find a configuration under which a message the platform still serves, inside the window the
  engine's cursor claims, is absent from the store and the parity run still reports OK.
* Delete any safety property and watch the suite stay green.
* Land a new channel type that requires editing `core/`.
* Find a number on a surface that is typed rather than derived.

If any of those succeeds, this document is wrong and the code is what needs changing.
