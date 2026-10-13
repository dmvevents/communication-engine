#!/usr/bin/env python3
"""scripts/dashboard_write.py — the operator's WRITE surface (ENH-28; gated).

Loaded by scripts/dashboard.py ONLY when COMMS_UI_WRITE_ENABLED=true; with the gate
off this module is never imported, so the read dashboard stays the viewer it always
was. This is the ONE surface allowed to reach core/outbox, and it reaches nothing
else: every send funnels through the outbox ladder, which means every send is
durable, deduped, paced, and read-back-proven — the UI adds a human, not a bypass. The
Send handler also opens a writable Journal — only after a PROVEN send, only to tie the
answer to its ask.

The contract this surface renders, in one sentence: **composing stages; only a human
click on the exact staged text sends; discard is terminal and kept.** No handler
here calls the adapter's send() — stage() only asks the adapter whether it can PLACE
the draft — and release()/discard() are invoked only from a button on a specific
draft. There is no "send now" compose path
even for 'direct'-policy channels: a human composing on the operator surface is
asking for a reviewed send, and the review is the point.

Rendering is READ-ONLY (core/dashboard.open_ro, missing files reported rather than
minted — the same discipline as the viewer); only the button handlers construct a
writable Outbox. In demo mode (the fake adapter) the whole cycle is provable offline:
the fake records deliveries in-memory and answers read-back from them, so a released
draft lands COMMITTED with a receipt and no network. Of the shipped adapters only
`fake` declares send capability — the live adapters are read-only by authorization —
so flipping the gate gives no live platform a send path; the surface says so per
instance instead of rendering a compose box that cannot deliver.

Answering closes the ask (ENH-30). Each open ask of a channel that accepts a reply
(thread scope OR channel scope not 'never') offers Reply, which pre-fills the compose
form; the reply stages with the ask's ts as its trigger — under the ask's thread ROOT
when the thread policy allows AND the adapter can place it there, top-level when the
channel policy allows (the banner and the flash say which, and why), and no Reply at
all, with the reason named, when neither placement is open — and the Send click, the
moment the answer is PROVEN on the target, ties the
ask to the outbox key via Journal.mark_responded. Staging never ties (a draft is a
promise, not an answer); a free compose ties to nothing. Measured 2026-10-12 on the
aws-do-agent integration: with no tie at all, every answered ask stayed UNANSWERED.

Three rules the independent merge review (2026-10-13) made load-bearing:

* **every action acts on the config as it is NOW.** A button binds only the instance
  name and the row key; the handler reloads settings.json and builds the adapter and
  outbox fresh, so a channel demoted to 'never' after the page rendered refuses at the
  click instead of sending (review B2);
* **actions run in callbacks, before the render pass,** so the screen the operator
  sees afterwards is the post-action truth — never a stale "replying to B" banner over a
  form that will stage to A;
* **a handler never crashes the page.** Send is offered only where the adapter can
  deliver (a draft on a read-only adapter is Discard-only, and says why); Discard needs
  no adapter at all; anything unexpected is reported with the row's actual state.
"""
import html
import os
import sqlite3
import sys
import time
from pathlib import Path

import streamlit as st

# Same no-install-step rule as every other script here: the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ConfigError, ensure_dirs, load, load_adapter_class  # noqa: E402
from core.dashboard import html_text, open_ro, snapshot  # noqa: E402
from core.delivery import NotPosted  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.classify import ACTIONABLE_KINDS  # noqa: E402
from core.outbox import (COMMITTED, DISCARDED, Outbox, PolicyError,  # noqa: E402
                         MISMATCH, ReleaseError, SENT, SendBlocked, SETTLED, STAGED,
                         UNPROVEN, VERIFIED, thread_block_reason)


# ---- config, fresh at every action --------------------------------------------
def _settings_path():
    """The same resolution scripts/dashboard.py uses — one source of truth."""
    return Path(os.environ.get("COMMS_SETTINGS", "settings.json"))


def _fresh_cfg():
    """Reload settings.json NOW. A render-time config captured into a button is the
    stale-policy hole: a channel demoted to 'never' after render would still send."""
    return load(_settings_path())


def _instance(cfg, name):
    for inst in cfg.instances:
        if inst.name == name:
            return inst
    return None


def _channel_scope_policy(ch):
    """The channel-scope policy string: config's policy() is a plain string when both
    placements share one, a per-scope dict once they differ."""
    p = ch.policy()
    return p if isinstance(p, str) else p.get("channel", "never")


def _thread_scope_policy(ch):
    p = ch.policy()
    return p if isinstance(p, str) else p.get("thread", "never")


def _placement_for(ch, adapter):
    """Where a reply to an ask on this channel goes, decided with the ADAPTER in hand
    (independent review of ENH-34, F6): ("thread", None) when the thread scope allows
    and the adapter can place it there; ("top-level", why) when the channel scope allows
    — `why` names what keeps it out of the thread, a thread_reply_policy of 'never' or
    an adapter that cannot thread, and the banner and the flash repeat it; (None, why)
    when neither placement is open, so no Reply is offered and the row says why."""
    tpol, cpol = _thread_scope_policy(ch), _channel_scope_policy(ch)
    if ch.is_dm:
        # ENH-31 guard 6: placement follows the human. A 1:1 DM is answered in the
        # conversation, top-level — a thread inside a DM is where nobody looks
        # (measured: 535 DM messages, 0 in-thread).
        if cpol != "never":
            return "top-level", "a direct message — replies post in the conversation, top-level"
        return None, f"{ch.id} is a direct message whose reply_policy is 'never'"
    blocked = thread_block_reason(adapter)
    if tpol != "never" and blocked is None:
        return "thread", None
    why = ("thread_reply_policy is 'never' for this channel" if tpol == "never"
           else f"this connection's adapter cannot place a reply in a thread: {blocked}")
    if cpol != "never":
        return "top-level", why
    return None, f"{ch.id} answers only in threads, and {why}"


def _seen_ts_for(cfg, channel_id, thread, ask_ts=None):
    """Guard 2's baseline: the newest platform ts this page SHOWED the human for the
    conversation they are answering (review round 2, R2-5: the outbox's own fallback is
    the platform's newest at staging time, which may be a message no page displayed).

    The page lists OPEN ASKS — never statements, which are journaled but routed `logged`
    and not shown (round 3, R3-4: the newest journaled row took a polled "never mind" as
    seen). So a top-level reply is baselined on the newest open ask the page lists for the
    channel, and at least on the ask being answered; a thread reply on the ask itself (the
    page shows the ask and never the thread, so any later reply in the thread is unseen and
    the first click names it — round 3, R3-8: baselining on the root named the very ask
    being answered). None (the outbox falls back to the platform) only for a free compose
    with no journal yet."""
    if thread is not None:
        return str(ask_ts) if ask_ts is not None else str(thread)
    if not Path(cfg.journal_path).is_file():
        return str(ask_ts) if ask_ts is not None else None
    conn = open_ro(cfg.journal_path)
    try:
        marks = ",".join("?" * len(ACTIONABLE_KINDS))
        row = conn.execute(
            f"SELECT ts FROM journal WHERE channel=? AND responded_at IS NULL AND kind IN ({marks}) "
            "ORDER BY CAST(ts AS REAL) DESC LIMIT 1", (channel_id, *ACTIONABLE_KINDS)).fetchone()
    finally:
        conn.close()
    newest_listed = str(row[0]) if row else None
    if ask_ts is None:
        return newest_listed
    if newest_listed is None:
        return str(ask_ts)
    try:
        return newest_listed if float(newest_listed) > float(ask_ts) else str(ask_ts)
    except (TypeError, ValueError):
        return str(ask_ts)


def _adapter_for(cfg, inst):
    return load_adapter_class(cfg.channels_dir, inst.adapter)(auth=inst.auth)


def _can_send(adapter):
    caps = getattr(adapter, "capabilities", None)
    if callable(caps):
        return bool(caps().get("send"))
    return callable(getattr(adapter, "send", None))


class _NoAdapter:
    """Stands in where no adapter call can happen — discard() never touches one — so
    a half-configured connection can still have its drafts discarded (S5). Not for
    stage(): the compose path builds the REAL adapter, because stage() asks it whether
    a thread draft can be placed at all (review D1)."""

    def capabilities(self):
        return {"read": False, "history": False, "search": False, "send": False,
                "react": False, "threads": False}


def _outbox_for(cfg, inst, adapter):
    """A WRITABLE outbox — constructed only inside an action handler, never while
    rendering: building one creates the db file, and a surface that mints state by
    being looked at is the exact disease the viewer's tests forbid. An ACTION may
    create the state it writes, so this is also where the config's directories are
    ensured (the same ensure_dirs the scheduler runs before its first write)."""
    ensure_dirs(cfg)
    # ENH-31: the instance's marker travels with the outbox, so every guard the
    # outbox runs (marker, size, redaction, re-read) runs on this surface's sends too.
    return Outbox(cfg.outbox_path_for(inst.name), adapter, inst.policies(),
                  marker=inst.send_marker)


def _rows(outbox_path, states):
    """Read rows for the listing, read-only; a missing outbox file is the normal
    fresh state and reads as no rows (the read tab already reports it missing)."""
    if not Path(outbox_path).is_file():
        return []
    conn = open_ro(outbox_path)
    try:
        marks = ",".join("?" * len(states))
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM outbox WHERE state IN ({marks}) "
            "ORDER BY updated_at DESC", states)]
    finally:
        conn.close()


def _push_flash(kind, msg):
    st.session_state.setdefault("write_flash", []).append((kind, msg))


# ---- the tie: a PROVEN answer closes its ask --------------------------------------
def _tie_answer(cfg, row, key):
    """ENH-30: a PROVEN send whose trigger is a journaled ask answers that ask.
    Returns the ask ts tied, or None. Stage never calls this (a draft is a promise);
    a free compose carries a synthetic trigger no journal row has, so it ties to
    nothing; an ask already answered keeps its first answer."""
    if row is None or not Path(cfg.journal_path).is_file():
        return None
    journal = Journal(cfg.journal_path)
    try:
        ask = journal.get(row["target"], row["trigger_ts"])
        if ask is None or ask["responded_at"] is not None:
            return None
        # Stamped at the time the ANSWER WAS WRITTEN (the draft's created_at), never at
        # send time: a draft may sit at the gate for hours, and an edit the asker made
        # in between must read as edited AFTER the answer (R23), not be laundered into
        # a clean tie (independent review F9).
        journal.mark_responded(row["target"], row["trigger_ts"], key,
                               at=row["created_at"] if "created_at" in row.keys() else None)
        return row["trigger_ts"]
    finally:
        journal.close()


def _tie_or_warn(cfg, row, key):
    """Tie a PROVEN send to its ask, and tell the truth if the journal refuses.
    The send already happened; a journal lock or disk error here must not crash the
    page (the operator would re-send) and must not pass silently (the ask would read
    unanswered with nothing saying why). The retry is Reply-with-the-same-text: the
    stage dedupes to the proven row and the compose handler ties it then."""
    try:
        tied = _tie_answer(cfg, row, key)
    except sqlite3.Error as ex:
        _push_flash("warning", "sent and proven, but recording the ask as answered "
                               f"FAILED: {ex} — it stays in the unanswered queue; "
                               "Reply with the same text to retry the tie")
        return None
    if tied is not None:
        _push_flash("success", f"the ask at ts {tied} is now recorded as answered — "
                               "it leaves the unanswered queue")
    return tied


# ---- targets: where a draft may go ------------------------------------------------
def _targets(cfg, adapters=None):
    """(free, replyable, unconfigured).

    free       {label: (inst, ch)} — channels that accept a top-level compose: the
               channel-scope policy is not 'never' AND the adapter declares send.
    replyable  {channel_id: (inst, ch)} — channels an open ask can be answered on:
               the THREAD scope or the channel scope is not 'never', and the adapter
               declares send. "Answer in the thread, never the main channel" is a
               policy the config can express; it must be answerable here too.
    unconfigured  [(instance, reason)] — connections whose adapter could not even be
               constructed (a slack instance with its token but no `channels` entry
               yet, the state the add-connection form leaves). Skipped, and NAMED, so
               a half-configured connection never takes the whole surface down.
    unanswerable  [(instance, channel_id, reason)] — channels whose policy opens a reply
               that this adapter cannot place (thread-only on an adapter that cannot
               thread): no Reply offered, and the reason is rendered (review F6).

    `adapters`, when given, is filled with every adapter this call constructed, by
    instance name, so one render builds each adapter once and never outside this
    guard (review R2-3).

    Both maps require send capability: offering a compose or a Reply whose adapter
    cannot deliver would stage drafts no click can ever send (every shipped live
    adapter is read-only by authorization)."""
    free, replyable, unconfigured, unanswerable = {}, {}, [], []
    for inst in cfg.instances:
        try:
            adapter = _adapter_for(cfg, inst)
        except Exception as ex:  # noqa: BLE001 — surfaced per-instance by render()
            unconfigured.append((inst.name, str(ex)))
            continue
        if adapters is not None:
            adapters[inst.name] = adapter
        if not _can_send(adapter):
            continue
        for ch in inst.channels:
            cpol, tpol = _channel_scope_policy(ch), _thread_scope_policy(ch)
            if cpol != "never":
                free[f"{inst.name} · {ch.id} (policy: {cpol})"] = (inst, ch)
            if cpol == "never" and tpol == "never":
                continue
            where, why = _placement_for(ch, adapter)
            if where is None:
                unanswerable.append((inst.name, ch.id, why))
            else:
                replyable[ch.id] = (inst, ch)
    return free, replyable, unconfigured, unanswerable


def composable(cfg):
    """([(instance, channel, policy)], [(instance_name, reason)]) — the free compose
    targets and the connections that could not be constructed (kept for callers of
    the ENH-28 shape; render() uses _targets directly)."""
    free, _, unconfigured, _ = _targets(cfg)
    return ([(inst, ch, _channel_scope_policy(ch)) for inst, ch in free.values()],
            unconfigured)


def _open_asks(cfg, replyable):
    """Open asks on channels that accept a reply — read-only, through the same
    snapshot the viewer renders (one definition of "unanswered", not two)."""
    snap = snapshot(cfg.journal_path, {})
    return [a for a in snap["attention"]
            if a["severity"] == "unanswered" and a["where"] in replyable]


def _thread_root(cfg, channel, ts):
    """The thread an ask lives in: its own thread_id when the stored message is a
    reply, else itself — the same rule core/store.thread_root applies for the sweep,
    read here through a read-only connection so rendering never mints the store. A
    reply placed under a reply's ts is one the sweep never looks under (review F4)."""
    if not Path(cfg.store_path).is_file():
        return str(ts)
    conn = open_ro(cfg.store_path)
    try:
        row = conn.execute("SELECT thread_id FROM messages WHERE channel_id=? AND ts=?",
                           (channel, str(ts))).fetchone()
        return row[0] if row and row[0] else str(ts)
    finally:
        conn.close()


# ---- callbacks: every action acts BEFORE the render pass, on fresh config ----------
def _reply_clicked(channel, ts, text, label):
    """Remember which ask the next compose answers; park the free-compose selector on
    that channel too (when it accepts top-level composes) so the draft AFTER the reply
    goes where the operator was just working, visibly."""
    st.session_state["reply_to"] = {"channel": channel, "ts": str(ts),
                                    "text": text or ""}
    if label:
        st.session_state["compose_where"] = label


def _cancel_reply_clicked():
    st.session_state.pop("reply_to", None)


def _stage_clicked():
    """Form submit — runs before the rerun, so the page the operator sees next shows
    the staged draft at the gate and no leftover reply banner."""
    text = (st.session_state.get("compose_text", "") or "").strip()
    where = st.session_state.get("compose_where")
    reply_to = st.session_state.get("reply_to")
    if not text:
        _push_flash("error", "an empty draft stages nothing")
        return
    try:
        cfg = _fresh_cfg()
    except ConfigError as ex:
        _push_flash("error", f"settings refused: {ex} — nothing staged")
        return
    free, replyable, _, _ = _targets(cfg)
    thread = why = None
    if reply_to is not None:
        pair = replyable.get(reply_to["channel"])
        if pair is None:
            st.session_state.pop("reply_to", None)
            _push_flash("error", f"channel {reply_to['channel']} no longer accepts a reply "
                                 "(policy or adapter changed since you clicked Reply) — "
                                 "nothing staged")
            return
        inst, ch = pair
        trigger = reply_to["ts"]                    # the ASK is the trigger: the tie key
    else:
        pair = free.get(where)
        if pair is None:
            _push_flash("error", "pick a channel to stage to — the selected one no longer "
                                 "accepts a top-level compose")
            return
        inst, ch = pair
        # The compose moment IS the trigger: a deliberate second compose of the same
        # text is a new draft, not a dedupe of the first.
        trigger = f"ui-compose-{time.time():.6f}"
    outbox = None
    try:
        # The REAL adapter, though stage() never calls its send(): the placement is
        # decided with it in hand (review F6), and stage() itself refuses a thread
        # draft the adapter cannot place (review D1) — the backstop for a connection
        # edited between that decision and this write.
        adapter = _adapter_for(cfg, inst)
        # ENH-31 guard 1: the connection's marker is PART of the draft the human will
        # approve — prepended here, shown on the card, verified again at the click.
        if inst.send_marker and not text.startswith(inst.send_marker):
            text = f"{inst.send_marker} {text}"
        if reply_to is not None:
            placement, why = _placement_for(ch, adapter)
            if placement is None:
                st.session_state.pop("reply_to", None)
                _push_flash("error", f"channel {ch.id} has no place for a reply now "
                                     f"({why}) — nothing staged")
                return
            thread = _thread_root(cfg, ch.id, trigger) if placement == "thread" else None
        outbox = _outbox_for(cfg, inst, adapter)
        ask_ts = reply_to["ts"] if reply_to is not None else None
        res = outbox.stage(ch.id, trigger, text, thread_id=thread,
                           seen_ts=_seen_ts_for(cfg, ch.id, thread, ask_ts))
    except PolicyError as ex:
        _push_flash("error", f"refused: {ex}")
        return
    except Exception as ex:  # noqa: BLE001 — a crashed page hides what happened
        _push_flash("error", f"{type(ex).__name__}: {ex} — nothing staged")
        return
    finally:
        if outbox is not None:
            outbox.close()
    st.session_state.pop("reply_to", None)
    st.session_state["compose_text"] = ""
    if (reply_to is not None and res.get("deduped") and res.get("receipt")
            and res["state"] in (COMMITTED, VERIFIED)):
        # The answer is already PROVEN on the target (a Send whose tie failed, or a
        # row recovered by Outbox.recover()): tie it now. Staging a NEW draft never
        # ties — a draft is a promise, not an answer.
        proven = _outbox_for(cfg, inst, _NoAdapter())
        try:
            _tie_or_warn(cfg, proven.get(res["key"]), res["key"])
        finally:
            proven.close()
    if res.get("re_read"):
        _push_flash("success", f"re-read acknowledged: draft `{res['key'][:12]}…` is "
                               "current again — the same text waits for your click below")
        return
    if res.get("deduped"):
        _push_flash("warning", f"already known: `{res['key'][:12]}…` is {res['state']}")
        return
    placement = ("" if reply_to is None else
                 (f" as a reply to ts {trigger}, in its thread" if thread
                  else f" as a reply to ts {trigger}, top-level ({why})"))
    _push_flash("success", f"staged `{res['key'][:12]}…`{placement} — waiting for your "
                           "click below; nothing has been sent")


def _release_clicked(inst_name, key):
    """The Send click. Binds only the instance NAME and the row key; everything else
    is resolved now, from the settings file as it is at this moment."""
    try:
        cfg = _fresh_cfg()
    except ConfigError as ex:
        _push_flash("error", f"settings refused: {ex} — nothing sent")
        return
    inst = _instance(cfg, inst_name)
    if inst is None:
        _push_flash("error", f"connection {inst_name} no longer exists in settings.json "
                             "— nothing sent")
        return
    outbox = None
    try:
        outbox = _outbox_for(cfg, inst, _adapter_for(cfg, inst))
        res = outbox.release(key)
        if res.get("in_flight"):
            # Not proven: a claim is in flight or died mid-flight. Saying "sent and
            # proven" here would be false; only recovery's read-back can tell.
            _push_flash("warning", f"already in flight — state {res['state']}, not "
                                   "proven yet; run Outbox.recover() before anything "
                                   "else touches this draft")
            return
        if res.get("deduped"):
            _push_flash("info", f"already delivered — state {res['state']}, receipt "
                                f"`{res['receipt']}`; nothing was re-sent")
            _tie_or_warn(cfg, outbox.get(key), key)
            return
        if res.get("unresolved"):
            # A Send drawn while the row was STAGED, clicked after a recovery run parked
            # it: nothing was sent, and saying "sent and proven" here would be the reverse
            # of the truth (round 3, R3-6). The card below has the actions.
            _push_flash("warning", f"nothing sent — this draft is parked as {res['state']}; "
                                   "see its card below (settle it, or send again once you "
                                   "have looked at the platform)")
            return
        _push_flash("success", f"sent and proven: state {res['state']}, receipt "
                               f"`{res['receipt']}`")
        if res.get("receipt") and res["state"] in (COMMITTED, VERIFIED):
            _tie_or_warn(cfg, outbox.get(key), key)
    except (PolicyError, ReleaseError) as ex:
        _push_flash("error", f"refused: {ex}")
    except SendBlocked as ex:
        # After SENT is durable a failed read-back is NOT a refusal: the message may
        # well be on the platform, and "refused" would invite a second one.
        state = outbox.get(key)["state"] if outbox is not None else "?"
        if state == MISMATCH:
            # ENH-31 guard 3: the message is on the platform with text that is not the
            # approved text. Nothing more will be sent; a human must look.
            _push_flash("error", f"delivered with DIFFERENT text (state {state}): {ex} — "
                                 "nothing more will be sent; the card below needs a human")
        elif state in (SENT, VERIFIED):
            _push_flash("warning", f"SENT but delivery unproven (state {state}): {ex} — "
                                   "do NOT re-send; run Outbox.recover()")
        else:
            _push_flash("error", f"refused: {ex}")
    except NotPosted as ex:
        # The platform ANSWERED the post with a refusal (the bot is not in the channel,
        # the channel is archived, the text is too long for it): nothing was posted, and
        # the outbox handed the draft back to the gate (review round 2, R2-8).
        state = outbox.get(key)["state"] if outbox is not None else "?"
        _push_flash("error", f"the platform refused the post — {ex}; nothing was posted, "
                             f"the draft is {state} again: fix the cause, then Send or Discard")
    except Exception as ex:  # noqa: BLE001 — the page must say what the row is now
        state = "?"
        if outbox is not None:
            row = outbox.get(key)
            state = row["state"] if row is not None else "?"
        _push_flash("error", f"{type(ex).__name__}: {ex} — row state is now {state}; "
                             "nothing else changed")
    finally:
        if outbox is not None:
            outbox.close()


def _reapprove_clicked(inst_name, key):
    """The card's "send again" on an UNPROVEN row: the human has looked at the platform and
    the reply is not there. One more post, through the same guards — an explicit second
    approval, never a stale Send (round 3, R3-5 / R3-7)."""
    try:
        cfg = _fresh_cfg()
    except ConfigError as ex:
        _push_flash("error", f"settings refused: {ex} — nothing sent")
        return
    inst = _instance(cfg, inst_name)
    if inst is None:
        _push_flash("error", f"connection {inst_name} no longer exists in settings.json "
                             "— nothing sent")
        return
    outbox = None
    try:
        outbox = _outbox_for(cfg, inst, _adapter_for(cfg, inst))
        res = outbox.release(key, reapprove=True)
        if res.get("unresolved") or res.get("in_flight"):
            _push_flash("warning", f"nothing sent — the row is {res['state']} now")
            return
        _push_flash("success", f"sent again and proven: state {res['state']}, receipt "
                               f"`{res['receipt']}`")
        if res.get("receipt") and res["state"] in (COMMITTED, VERIFIED):
            _tie_or_warn(cfg, outbox.get(key), key)
    except (PolicyError, ReleaseError) as ex:
        _push_flash("error", f"refused: {ex}")
    except Exception as ex:  # noqa: BLE001 — the page must say what the row is now
        state = "?"
        if outbox is not None:
            row = outbox.get(key)
            state = row["state"] if row is not None else "?"
        _push_flash("error", f"{type(ex).__name__}: {ex} — row state is now {state}")
    finally:
        if outbox is not None:
            outbox.close()


def _settle_clicked(inst_name, key):
    """The card's "settled": the human looked at the platform and resolved it there. The
    row is kept as SETTLED and leaves triage (round 3, R3-7). Needs no adapter."""
    try:
        cfg = _fresh_cfg()
    except ConfigError as ex:
        _push_flash("error", f"settings refused: {ex} — nothing changed")
        return
    inst = _instance(cfg, inst_name)
    if inst is None:
        _push_flash("error", f"connection {inst_name} no longer exists in settings.json "
                             "— nothing changed")
        return
    outbox = None
    try:
        outbox = _outbox_for(cfg, inst, _NoAdapter())
        res = outbox.settle(key)
        _push_flash("success", f"settled — kept in the outbox as {res['state']}, no longer "
                               "listed; nothing was sent")
    except ReleaseError as ex:
        _push_flash("error", f"refused: {ex}")
    except Exception as ex:  # noqa: BLE001
        _push_flash("error", f"{type(ex).__name__}: {ex} — nothing changed")
    finally:
        if outbox is not None:
            outbox.close()


def _discard_clicked(inst_name, key):
    """Discard never calls an adapter, so it must never need one: a draft on a
    half-configured connection stays discardable (review S5)."""
    try:
        cfg = _fresh_cfg()
    except ConfigError as ex:
        _push_flash("error", f"settings refused: {ex} — nothing changed")
        return
    inst = _instance(cfg, inst_name)
    if inst is None:
        _push_flash("error", f"connection {inst_name} no longer exists in settings.json "
                             "— nothing changed")
        return
    outbox = None
    try:
        outbox = _outbox_for(cfg, inst, _NoAdapter())
        outbox.discard(key)
        _push_flash("success", "discarded — kept in the outbox as DISCARDED, "
                               "never sendable")
    except ReleaseError as ex:
        _push_flash("error", f"refused: {ex}")
    except Exception as ex:  # noqa: BLE001
        _push_flash("error", f"{type(ex).__name__}: {ex} — nothing changed")
    finally:
        if outbox is not None:
            outbox.close()


# ---- render ---------------------------------------------------------------------
def render(cfg):
    st.divider()
    st.header("write surface")
    st.caption("Composing STAGES a draft into the outbox — nothing sends without "
               "your click on that exact text below. Discard is terminal and kept "
               "(the audit trail records the refusal).")
    for kind, msg in st.session_state.pop("write_flash", []):
        getattr(st, kind)(msg)

    adapters = {}
    free, replyable, unconfigured, unanswerable = _targets(cfg, adapters)
    for name, reason in unconfigured:
        st.warning(f"connection **{html.escape(name)}** is not fully configured yet, "
                   f"so it cannot be watched or composed to: {html.escape(reason)} "
                   "Finish it under “edit connection (adapter / auth)” below — add the "
                   "missing auth entry as an env:NAME reference — then it becomes "
                   "available. (The connection is kept; only this surface skips it.)")
    for name, ch_id, why in unanswerable:
        st.warning(f"no Reply for **{html.escape(ch_id)}** (connection "
                   f"**{html.escape(name)}**): {html.escape(why)} — answer it on the "
                   "platform, or widen the channel's reply_policy, or move the connection "
                   "to an adapter that can thread.")
    label_of = {ch.id: label for label, (inst, ch) in free.items()}

    # ---- open asks: Reply pre-fills the form (ENH-30) -------------------------
    asks = _open_asks(cfg, replyable)
    if asks:
        st.markdown("##### open asks you can answer from here")
        for a in asks:
            col_text, col_btn = st.columns([5, 1])
            # An HTML BLOCK (`<div`-led, no blank line inside): ask text is
            # attacker-controlled, and inline markdown stays inert only there.
            col_text.markdown(
                "<div style='padding:2px 0'>"
                f"<b>{html.escape(a['where'])}</b> · ts {html.escape(str(a['ts']))}"
                f" · {html.escape(str(a['kind'] or 'unclassified'))}<br>"
                f"<span style='font-size:.9rem'>{html_text(a['text'])}</span>"
                "</div>", unsafe_allow_html=True)
            # '|' cannot appear in a platform channel id or ts, so the key is
            # unambiguous ('-' could be, and a collision crashes the page).
            col_btn.button("Reply", key=f"reply|{a['where']}|{a['ts']}",
                           on_click=_reply_clicked,
                           args=(a["where"], a["ts"], a["text"], label_of.get(a["where"])))

    reply_to = st.session_state.get("reply_to")
    if reply_to is not None and reply_to["channel"] not in replyable:
        # The channel was demoted or its adapter changed since Reply was clicked.
        st.session_state.pop("reply_to", None)
        reply_to = None
    if reply_to is not None and (reply_to["channel"], reply_to["ts"]) not in {
            (a["where"], str(a["ts"])) for a in asks}:
        # Answered elsewhere since Reply was clicked (the sweep, another tab): the
        # banner must not keep promising to record an ask that is already closed.
        st.session_state.pop("reply_to", None)
        st.info(f"the ask at ts {html.escape(reply_to['ts'])} in "
                f"{html.escape(reply_to['channel'])} is no longer open — it was answered "
                "elsewhere; nothing to reply to")
        reply_to = None
    if reply_to is not None:
        reply_inst, reply_ch = replyable[reply_to["channel"]]
        where, why = _placement_for(reply_ch, adapters[reply_inst.name])
        placement = ("in its thread" if where == "thread"
                     else f"top-level in the channel ({html.escape(why)})")
        st.info(f"replying to **{html.escape(reply_to['channel'])}** · ts "
                f"{html.escape(reply_to['ts'])} — {placement}. Sending will record "
                "that ask as answered.")
        st.button("cancel reply", key="reply_cancel", on_click=_cancel_reply_clicked)

    # ---- compose: stage only, never send ----------------------------------------
    if reply_to is None and not free:
        if replyable:
            st.info("The write gate is on, but no channel accepts a top-level compose "
                    "(every channel-scope policy is 'never'). Channels whose "
                    "thread_reply_policy allows it are answered through **Reply** on an "
                    "open ask above — in the thread, never the main channel.")
        else:
            st.info("The write gate is on, but no channel is composable: a channel "
                    "needs `reply_policy` (or `thread_reply_policy`) of 'staged' or "
                    "'direct' in settings.json — every channel defaults to 'never' "
                    "(deny) — AND its instance's adapter must declare send capability. "
                    "Of the shipped adapters only `fake` does; the live adapters are "
                    "read-only by authorization (see the quickstart's honest limits).")
    else:
        if st.session_state.get("compose_where") not in free:
            st.session_state.pop("compose_where", None)
        with st.form("compose"):
            if reply_to is None:
                st.selectbox("stage to", sorted(free), key="compose_where")
            else:
                st.caption(f"stage to: **{html.escape(reply_to['channel'])}** — in reply "
                           f"to ts {html.escape(reply_to['ts'])}")
            st.text_area("message", height=100, key="compose_text")
            markers = sorted({inst.send_marker for inst in cfg.instances if inst.send_marker})
            if markers:
                st.caption("messages from a connection with a `send_marker` are prefixed "
                           f"with it when staged ({', '.join(f'`{m}`' for m in markers)}) — "
                           "the card below shows the exact text a Send would post")
            st.form_submit_button("Stage draft", on_click=_stage_clicked)

    # ---- the gate: staged drafts, one click each ----------------------------
    st.subheader("staged — waiting for you")
    any_staged = False
    for inst in cfg.instances:
        outbox_path = cfg.outbox_path_for(inst.name)
        drafts = _rows(outbox_path, (STAGED,))
        mismatched = _rows(outbox_path, (MISMATCH, UNPROVEN))
        if not drafts and not mismatched:
            continue
        # Send is offered only where the adapter can deliver: a click on a draft whose
        # adapter has no send() used to flip the row to INTENT and crash — a wedge no
        # Discard could clear (review B3). The reason is on the card, not hidden.
        adapter = adapters.get(inst.name)
        try:
            if adapter is None:                 # not built above: half-configured
                adapter = _adapter_for(cfg, inst)
            inst_no_send = (None if _can_send(adapter)
                            else "this instance's adapter cannot send (read-only by "
                                 "authorization) — Discard is the only action")
        except Exception as ex:  # noqa: BLE001 — half-configured connection
            inst_no_send = (f"this connection is not fully configured ({html_text(str(ex))})"
                            " — finish it or Discard")
        for row in drafts:
            any_staged = True
            # Per ROW too (review F7): a thread draft on an adapter that cannot place it
            # is B3's case on the thread dimension — Discard-only, and the card says why.
            no_send = inst_no_send
            if no_send is None and row["thread_id"]:
                blocked = thread_block_reason(adapter)
                if blocked is not None:
                    no_send = (f"this connection's adapter cannot place a reply in thread "
                               f"{html.escape(str(row['thread_id']))}: {html.escape(blocked)}"
                               " — Discard is the only action")
            st.markdown(
                "<div style='border:1px solid rgba(128,128,128,.3);border-left:"
                "3px solid #fab219;border-radius:8px;padding:10px 14px;margin:2px 0'>"
                f"<b>STAGED</b> · {html.escape(row['target'])} · instance "
                f"{html.escape(inst.name)} · key <code>{html.escape(row['key'][:12])}…"
                "</code>"
                + ("" if str(row["trigger_ts"]).startswith("ui-compose-") else
                   f" · <b>in reply to ts {html.escape(str(row['trigger_ts']))}</b>"
                   + (f" (in thread {html.escape(str(row['thread_id']))})"
                      if row["thread_id"] else " (top-level)"))
                + "<div style='font-size:.9rem;margin-top:4px'>"
                f"{html_text(row['text'])}</div>"
                + ("" if no_send is None else
                   f"<div style='color:#8f1d1d;font-size:.85rem;margin-top:4px'>no Send: "
                   f"{no_send}</div>")
                + "</div>", unsafe_allow_html=True)
            approve, reject = st.columns(2)
            if no_send is None:
                approve.button("Send — this exact text", key=f"send-{row['key']}",
                               on_click=_release_clicked, args=(inst.name, row["key"]))
            reject.button("Discard", key=f"discard-{row['key']}",
                          on_click=_discard_clicked, args=(inst.name, row["key"]))
        # ENH-31 guard 3: rows the platform holds with text that is not the approved
        # text (MISMATCH), and rows recovery could not settle — re-sent once and still
        # unprovable, or a read-back that keeps failing (UNPROVEN, round 2 R2-6). Terminal
        # until a human acts — no Send, no Discard; the card says what to do.
        for row in mismatched:
            any_staged = True
            if row["state"] == MISMATCH:
                why = ("delivered with DIFFERENT text — the platform shows a message carrying "
                       "this draft's key whose text is not the text above. Nothing more will "
                       "be sent and nothing is recorded as answered; compare the two on the "
                       "platform, then edit or delete there. Needs a human.")
            else:
                why = ("UNPROVEN — recovery could not settle this approved text: the platform "
                       "showed no message carrying its key on the run after its one re-send, "
                       "or the target is gone, or an unproven post could not be re-sent. "
                       "Nothing more is sent by itself and nothing is recorded as answered. "
                       "Look for the text above on the platform: if it is there, or no longer "
                       "needed, mark it settled; if it is not there and still needed, send "
                       "it again. Needs a human.")
            st.markdown(
                "<div style='border:1px solid rgba(128,128,128,.3);border-left:"
                "3px solid #8f1d1d;border-radius:8px;padding:10px 14px;margin:2px 0'>"
                f"<b>{html.escape(row['state'])}</b> · {html.escape(row['target'])} · instance "
                f"{html.escape(inst.name)} · key <code>{html.escape(row['key'][:12])}…</code>"
                "<div style='font-size:.9rem;margin-top:4px'>approved text: "
                f"{html_text(row['text'])}</div>"
                f"<div style='color:#8f1d1d;font-size:.85rem;margin-top:4px'>{why}</div></div>",
                unsafe_allow_html=True)
            again, settle = st.columns([1, 1])
            if row["state"] == UNPROVEN and inst_no_send is None:
                again.button("Send again (I looked; it is not there)",
                             key=f"reapprove-{row['key']}", on_click=_reapprove_clicked,
                             args=(inst.name, row["key"]))
            settle.button("Settled on the platform", key=f"settle-{row['key']}",
                          on_click=_settle_clicked, args=(inst.name, row["key"]))
    if not any_staged:
        st.markdown("_no drafts at the gate_")

    # ---- sent: the other half of the staged-vs-sent distinction -------------
    st.subheader("sent — proven on the target")
    sent_rows = []
    for inst in cfg.instances:
        for row in _rows(cfg.outbox_path_for(inst.name), (VERIFIED, COMMITTED)):
            sent_rows.append((inst.name, row))
    if sent_rows:
        body = "".join(
            "<tr>"
            f"<td style='padding:4px 8px'><b>{html.escape(row['state'])}</b></td>"
            f"<td style='padding:4px 8px'>{html.escape(row['target'])}</td>"
            f"<td style='padding:4px 8px'>{html.escape(name)}</td>"
            f"<td style='padding:4px 8px'>{html_text(row['text'])}</td>"
            f"<td style='padding:4px 8px'><code>{html.escape(str(row['receipt']))}"
            "</code></td></tr>"
            for name, row in sent_rows)
        st.markdown("<table style='width:100%;border-collapse:collapse;font-size:"
                    "13px'><thead><tr><th align='left' style='padding:4px 8px'>state"
                    "</th><th align='left' style='padding:4px 8px'>target</th>"
                    "<th align='left' style='padding:4px 8px'>instance</th>"
                    "<th align='left' style='padding:4px 8px'>text</th>"
                    "<th align='left' style='padding:4px 8px'>receipt</th></tr>"
                    f"</thead><tbody>{body}</tbody></table>",
                    unsafe_allow_html=True)
    else:
        st.markdown("_nothing sent from this outbox yet_")
    discarded = sum(len(_rows(cfg.outbox_path_for(i.name), (DISCARDED,)))
                    for i in cfg.instances)
    if discarded:
        st.caption(f"{discarded} discarded draft(s) kept in the outbox as the "
                   "record of a human refusal")
