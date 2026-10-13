#!/usr/bin/env python3
"""scripts/dashboard_write.py — the operator's WRITE surface (ENH-28; gated).

Loaded by scripts/dashboard.py ONLY when COMMS_UI_WRITE_ENABLED=true; with the gate
off this module is never imported, so the read dashboard stays the viewer it always
was. This is the ONE surface allowed to reach core/outbox, and it reaches nothing
else: every send funnels through the outbox ladder, which means every send is
durable, deduped, paced, and read-back-proven — the UI adds a human, not a bypass.

The contract this surface renders, in one sentence: **composing stages; only a human
click on the exact staged text sends; discard is terminal and kept.** No handler
here calls the adapter — stage() never touches it, and release()/discard() are
invoked only from a button on a specific draft. There is no "send now" compose path
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

Answering closes the ask (ENH-30). Each open ask of a composable channel offers a
Reply that pre-fills the compose form; the reply stages with the ask's ts as its
trigger (in the ask's thread when the thread policy allows, top-level otherwise —
never refused, never silently threaded), and the Send click — the moment the answer
is PROVEN on the target — ties the ask to the outbox key via Journal.mark_responded.
Staging never ties (a draft is a promise, not an answer) and a free compose ties to
nothing. Measured 2026-10-12 on the aws-do-agent integration: with no tie at all,
every answered ask stayed UNANSWERED forever.
"""
import html
import sys
import time
from pathlib import Path

import streamlit as st

# Same no-install-step rule as every other script here: the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ensure_dirs, load_adapter_class  # noqa: E402
from core.dashboard import open_ro, snapshot  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import (COMMITTED, DISCARDED, Outbox, PolicyError,  # noqa: E402
                         ReleaseError, SendBlocked, STAGED, VERIFIED)


def _channel_scope_policy(ch):
    """The channel-scope policy string for one configured channel (compose posts
    top-level; thread placement is the outbox row's business, not the form's)."""
    p = ch.policy()
    return p if isinstance(p, str) else p.get("channel", "never")


def _thread_scope_policy(ch):
    """The thread-scope policy string: config's policy() is a plain string when both
    placements share one, a per-scope dict once they differ."""
    p = ch.policy()
    return p if isinstance(p, str) else p.get("thread", "never")


def _adapter_for(cfg, inst):
    return load_adapter_class(cfg.channels_dir, inst.adapter)(auth=inst.auth)


def _outbox_for(cfg, inst):
    """A WRITABLE outbox — constructed only inside an action handler, never while
    rendering: building one creates the db file, and a surface that mints state by
    being looked at is the exact disease the viewer's tests forbid. An ACTION may
    create the state it writes, so this is also where the config's directories are
    ensured (the same ensure_dirs the scheduler runs before its first write)."""
    ensure_dirs(cfg)
    return Outbox(cfg.outbox_path_for(inst.name), _adapter_for(cfg, inst),
                  inst.policies())


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
        journal.mark_responded(row["target"], row["trigger_ts"], key)
        return row["trigger_ts"]
    finally:
        journal.close()


def _release_clicked(cfg, inst, key):
    """Button callback — runs BEFORE the render pass, so the surface the operator
    sees after the click is the post-action truth, never a stale card whose Send
    button belongs to a draft that already left."""
    outbox = _outbox_for(cfg, inst)
    try:
        res = outbox.release(key)
        _push_flash("success", f"sent and proven: state {res['state']}, receipt "
                               f"`{res['receipt']}`")
        if res.get("receipt") and res["state"] in (COMMITTED, VERIFIED):
            tied = _tie_answer(cfg, outbox.get(key), key)
            if tied is not None:
                _push_flash("success", f"the ask at ts {tied} is now recorded as "
                                       "answered — it leaves the unanswered queue")
    except (PolicyError, ReleaseError, SendBlocked) as ex:
        _push_flash("error", f"refused: {ex}")
    finally:
        outbox.close()


def _discard_clicked(cfg, inst, key):
    outbox = _outbox_for(cfg, inst)
    try:
        outbox.discard(key)
        _push_flash("success", "discarded — kept in the outbox as DISCARDED, "
                               "never sendable")
    except ReleaseError as ex:
        _push_flash("error", f"refused: {ex}")
    finally:
        outbox.close()


def _reply_clicked(channel, ts, text):
    """Button callback: remember which ask the next compose answers."""
    st.session_state["reply_to"] = {"channel": channel, "ts": str(ts),
                                    "text": text or ""}


def _cancel_reply_clicked():
    st.session_state.pop("reply_to", None)


def _open_asks(cfg, composable_ids):
    """Open asks on channels the operator can compose to — read-only, through the
    same snapshot the viewer renders (one definition of "unanswered", not two)."""
    snap = snapshot(cfg.journal_path, {})
    return [a for a in snap["attention"]
            if a["severity"] == "unanswered" and a["where"] in composable_ids]


def composable(cfg):
    """([(instance, channel, policy)], [(instance_name, reason)]) — the compose
    targets and the connections that could not even be constructed.

    A compose target is a channel whose policy is not 'never' AND whose instance's
    adapter declares send capability. Both halves are load-bearing: default-deny is
    the engine's rule, and offering a compose box whose adapter cannot deliver (every
    shipped live adapter — read-only by authorization) would stage drafts no click
    can ever send.

    Constructing the adapter can itself fail: a half-configured connection — e.g. a
    slack instance whose token is set but whose `channels` auth entry has not been
    added yet, the exact state the add-connection form leaves before the second auth
    entry is staged — makes the adapter's __init__ refuse (it will not run
    half-configured). Such an instance is not a compose target, and it must not take
    the whole surface down: core/doctor already guards this same construction call and
    surfaces the failure per-check; this surface does the same, skipping the instance
    and returning it so render() can NAME what still needs configuring instead of
    showing the operator a crashed page."""
    out, unconfigured = [], []
    for inst in cfg.instances:
        try:
            adapter = _adapter_for(cfg, inst)
        except Exception as ex:  # noqa: BLE001 — surfaced per-instance by render()
            unconfigured.append((inst.name, str(ex)))
            continue
        if not adapter.capabilities().get("send"):
            continue
        for ch in inst.channels:
            policy = _channel_scope_policy(ch)
            if policy != "never":
                out.append((inst, ch, policy))
    return out, unconfigured


def render(cfg):
    st.divider()
    st.header("write surface")
    st.caption("Composing STAGES a draft into the outbox — nothing sends without "
               "your click on that exact text below. Discard is terminal and kept "
               "(the audit trail records the refusal).")
    for kind, msg in st.session_state.pop("write_flash", []):
        getattr(st, kind)(msg)

    # ---- compose: stage only, never send -----------------------------------
    options, unconfigured = composable(cfg)
    for name, reason in unconfigured:
        st.warning(f"connection **{html.escape(name)}** is not fully configured yet, "
                   f"so it cannot be watched or composed to: {html.escape(reason)} "
                   "Finish it under “edit connection (adapter / auth)” below — add the "
                   "missing auth entry as an env:NAME reference — then it becomes "
                   "available. (The connection is kept; only this surface skips it.)")
    if not options:
        st.info("The write gate is on, but no channel is composable: a channel "
                "needs `reply_policy` (or `thread_reply_policy`) of 'staged' or "
                "'direct' in settings.json — every channel defaults to 'never' "
                "(deny) — AND its instance's adapter must declare send capability. "
                "Of the shipped adapters only `fake` does; the live adapters are "
                "read-only by authorization (see the quickstart's honest limits).")
    else:
        labels = {f"{inst.name} · {ch.id} (policy: {policy})": (inst, ch)
                  for inst, ch, policy in options}
        label_of = {ch.id: label for label, (inst, ch) in labels.items()}
        composable_ids = {ch.id: (inst, ch) for inst, ch, _ in options}
        # ---- open asks: Reply pre-fills the form (ENH-30) ----------------
        asks = _open_asks(cfg, composable_ids)
        if asks:
            st.markdown("##### open asks you can answer from here")
            for a in asks:
                col_text, col_btn = st.columns([5, 1])
                col_text.markdown(
                    f"**{html.escape(a['where'])}** · ts {html.escape(str(a['ts']))}"
                    f" · {html.escape(str(a['kind'] or 'unclassified'))}<br>"
                    f"<span style='font-size:.9rem'>{html.escape(a['text'] or '')}"
                    "</span>", unsafe_allow_html=True)
                col_btn.button("Reply", key=f"reply-{a['where']}-{a['ts']}",
                               on_click=_reply_clicked,
                               args=(a["where"], a["ts"], a["text"]))
        reply_to = st.session_state.get("reply_to")
        if reply_to is not None and reply_to["channel"] not in composable_ids:
            # The channel was demoted since Reply was clicked: never stage to it.
            st.session_state.pop("reply_to", None)
            reply_to = None
        if reply_to is not None:
            _, reply_ch = composable_ids[reply_to["channel"]]
            placement = ("in its thread" if _thread_scope_policy(reply_ch) != "never"
                         else "top-level in the channel (thread_reply_policy is "
                              "'never' for this channel)")
            st.info(f"replying to **{html.escape(reply_to['channel'])}** · ts "
                    f"{html.escape(reply_to['ts'])} — {placement}. Sending will "
                    "record that ask as answered.")
            st.button("cancel reply", key="reply-cancel", on_click=_cancel_reply_clicked)
        with st.form("compose", clear_on_submit=True):
            choices = sorted(labels)
            preset = (choices.index(label_of[reply_to["channel"]])
                      if reply_to is not None else 0)
            where = st.selectbox("stage to", choices, index=preset,
                                 disabled=reply_to is not None)
            text = st.text_area("message", height=100)
            submitted = st.form_submit_button("Stage draft")
        if submitted:
            if not text.strip():
                st.error("an empty draft stages nothing")
            else:
                if reply_to is not None:
                    inst, ch = composable_ids[reply_to["channel"]]
                    # The ASK is the trigger: the release will tie the two.
                    trigger = reply_to["ts"]
                    thread = (reply_to["ts"]
                              if _thread_scope_policy(ch) != "never" else None)
                else:
                    inst, ch = labels[where]
                    # The compose moment IS the trigger: a deliberate second compose
                    # of the same text is a new draft, not a dedupe of the first.
                    trigger, thread = f"ui-compose-{time.time():.6f}", None
                outbox = _outbox_for(cfg, inst)
                try:
                    res = outbox.stage(ch.id, trigger, text, thread_id=thread)
                finally:
                    outbox.close()
                st.session_state.pop("reply_to", None)
                if res.get("deduped"):
                    st.warning(f"already known: `{res['key'][:12]}…` is {res['state']}")
                else:
                    st.success(f"staged `{res['key'][:12]}…` — waiting for your "
                               "click below; nothing has been sent")

    # ---- the gate: staged drafts, one click each ----------------------------
    st.subheader("staged — waiting for you")
    any_staged = False
    for inst in cfg.instances:
        outbox_path = cfg.outbox_path_for(inst.name)
        for row in _rows(outbox_path, (STAGED,)):
            any_staged = True
            st.markdown(
                "<div style='border:1px solid rgba(128,128,128,.3);border-left:"
                "3px solid #fab219;border-radius:8px;padding:10px 14px;margin:2px 0'>"
                f"<b>STAGED</b> · {html.escape(row['target'])} · instance "
                f"{html.escape(inst.name)} · key <code>{html.escape(row['key'][:12])}…"
                "</code>"
                + ("" if str(row["trigger_ts"]).startswith("ui-compose-") else
                   f" · <b>in reply to ts {html.escape(str(row['trigger_ts']))}</b>"
                   + (" (in thread)" if row["thread_id"] else " (top-level)"))
                + "<div style='font-size:.9rem;margin-top:4px'>"
                f"{html.escape(row['text'])}</div></div>", unsafe_allow_html=True)
            approve, reject = st.columns(2)
            approve.button("Send — this exact text", key=f"send-{row['key']}",
                           on_click=_release_clicked, args=(cfg, inst, row["key"]))
            reject.button("Discard", key=f"discard-{row['key']}",
                          on_click=_discard_clicked, args=(cfg, inst, row["key"]))
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
            f"<td style='padding:4px 8px'>{html.escape(row['text'])}</td>"
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
