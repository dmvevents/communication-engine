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
from core.dashboard import open_ro  # noqa: E402
from core.outbox import (COMMITTED, DISCARDED, Outbox, PolicyError,  # noqa: E402
                         ReleaseError, SendBlocked, STAGED, VERIFIED)


def _channel_scope_policy(ch):
    """The channel-scope policy string for one configured channel (compose posts
    top-level; thread placement is the outbox row's business, not the form's)."""
    p = ch.policy()
    return p if isinstance(p, str) else p.get("channel", "never")


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


def _release_clicked(cfg, inst, key):
    """Button callback — runs BEFORE the render pass, so the surface the operator
    sees after the click is the post-action truth, never a stale card whose Send
    button belongs to a draft that already left."""
    outbox = _outbox_for(cfg, inst)
    try:
        res = outbox.release(key)
        _push_flash("success", f"sent and proven: state {res['state']}, receipt "
                               f"`{res['receipt']}`")
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


def composable(cfg):
    """[(instance, channel, policy)] a compose can stage to: policy not 'never' AND
    the instance's adapter declares send capability. Both halves are load-bearing:
    default-deny is the engine's rule, and offering a compose box whose adapter
    cannot deliver (every shipped live adapter — read-only by authorization) would
    stage drafts no click can ever send."""
    out = []
    for inst in cfg.instances:
        if not _adapter_for(cfg, inst).capabilities().get("send"):
            continue
        for ch in inst.channels:
            policy = _channel_scope_policy(ch)
            if policy != "never":
                out.append((inst, ch, policy))
    return out


def render(cfg):
    st.divider()
    st.header("write surface")
    st.caption("Composing STAGES a draft into the outbox — nothing sends without "
               "your click on that exact text below. Discard is terminal and kept "
               "(the audit trail records the refusal).")
    for kind, msg in st.session_state.pop("write_flash", []):
        getattr(st, kind)(msg)

    # ---- compose: stage only, never send -----------------------------------
    options = composable(cfg)
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
        with st.form("compose", clear_on_submit=True):
            where = st.selectbox("stage to", sorted(labels))
            text = st.text_area("message", height=100)
            submitted = st.form_submit_button("Stage draft")
        if submitted:
            if not text.strip():
                st.error("an empty draft stages nothing")
            else:
                inst, ch = labels[where]
                outbox = _outbox_for(cfg, inst)
                try:
                    # The compose moment IS the trigger: a deliberate second compose
                    # of the same text is a new draft, not a dedupe of the first.
                    res = outbox.stage(ch.id, f"ui-compose-{time.time():.6f}", text)
                finally:
                    outbox.close()
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
                "</code><div style='font-size:.9rem;margin-top:4px'>"
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
