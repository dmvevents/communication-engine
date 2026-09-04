#!/usr/bin/env python3
"""scripts/dashboard_config.py — connection + monitored-channel management (ENH-29; gated).

Loaded by scripts/dashboard.py ONLY when COMMS_UI_WRITE_ENABLED=true, exactly like the
message write surface (scripts/dashboard_write.py); with the gate off this module is
never imported and settings.json has no UI that can touch it. This is the ONE surface
allowed to reach core/reconfig, and core/reconfig.apply() is the ONE path that writes
the settings file.

The contract, in one sentence: **an edit STAGES an exact settings diff; only a human
click on that exact diff writes the file; discard is terminal and kept.** No form here
writes settings.json — every submit builds a candidate, hands it to
``ConfigStage.stage()`` (validated by the engine's own loader first), and the change
waits at the gate below with its old→new diff rendered in full. Policy WIDENINGS —
anything that makes the engine more able to post — are flagged in red on the exact card
the human clicks. New channels default to ``reply_policy: never`` by OMISSION (the
loader's own deny-by-default is the single copy of that rule).

Secrets: this surface accepts environment-variable NAMES only, shows only whether the
named variable is currently set, and never reads ``InstanceConfig.auth`` (which holds
RESOLVED values) — everything displayed comes from the raw settings text, where auth is
already required to be an ``env:NAME`` reference.

Rendering is READ-ONLY (core/dashboard.open_ro; a missing stage db reads as no rows) —
only the action handlers construct a writable ConfigStage. Apply tells the truth about
reload semantics (core/reconfig.RELOAD_TRUTH): the dashboard re-reads settings on every
rerun, but a running scheduler/watcher loads settings only at startup and must be
restarted — there is no hot-reload path, and this surface does not pretend one exists.
"""
import html
import json
import os
import sys
from pathlib import Path

import streamlit as st

# Same no-install-step rule as every other script here: the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import (ConfigError, VALID_POLICIES, discover_adapters,  # noqa: E402
                         ensure_dirs)
from core.dashboard import open_ro  # noqa: E402
from core.reconfig import (APPLIED, DISCARDED, KEEP, STAGED,  # noqa: E402
                           ConfigStage, StageError, add_channel, add_instance,
                           remove_auth, remove_channel, remove_instance,
                           set_adapter, set_auth, update_channel)

POLICIES = tuple(VALID_POLICIES)
UNCHANGED, SAME = "(keep)", "(same as channel)"


def _stage_db(cfg):
    return cfg.state_dir / "confstage.db"


def _stage_for(cfg, cfg_path):
    """A WRITABLE stage — constructed only inside an action handler, never while
    rendering (the dashboard_write rule: a surface that mints state by being looked
    at is the exact disease the viewer's tests forbid)."""
    ensure_dirs(cfg)
    return ConfigStage(_stage_db(cfg), cfg_path)


def _rows(db_path, states):
    """Staged-change rows for the listing, read-only; a missing stage db is the
    normal fresh state and reads as no rows."""
    if not Path(db_path).is_file():
        return []
    conn = open_ro(db_path)
    try:
        marks = ",".join("?" * len(states))
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM confstage WHERE state IN ({marks}) "
            "ORDER BY created_at DESC", states)]
    finally:
        conn.close()


def _push_flash(kind, msg):
    st.session_state.setdefault("config_flash", []).append((kind, msg))


def _apply_clicked(cfg, cfg_path, key):
    """Button callback — runs BEFORE the render pass, so the page the operator sees
    after the click is built from the settings file the click just changed."""
    stage = _stage_for(cfg, cfg_path)
    try:
        res = stage.apply(key)
        _push_flash("success", res["reload"])
    except (StageError, ConfigError) as ex:
        _push_flash("error", f"refused: {ex}")
    finally:
        stage.close()


def _discard_clicked(cfg, cfg_path, key):
    stage = _stage_for(cfg, cfg_path)
    try:
        stage.discard(key)
        _push_flash("success", "discarded — kept in the stage record as DISCARDED, "
                               "never applicable")
    except StageError as ex:
        _push_flash("error", f"refused: {ex}")
    finally:
        stage.close()


def _stage_edit(cfg, cfg_path, summary, op, **kwargs):
    """Run ONE edit op against the file's current content and stage the candidate.
    Staging touches nothing: the success message says so, and the diff waits below."""
    try:
        candidate = op(json.loads(Path(cfg_path).read_text()), **kwargs)
        stage = _stage_for(cfg, cfg_path)
        try:
            res = stage.stage(candidate, summary=summary)
        finally:
            stage.close()
    except (StageError, ConfigError) as ex:
        st.error(f"refused: {ex}")
        return
    if res.get("deduped"):
        st.warning(f"already at the gate: `{res['key'][:12]}…` is {res['state']}")
        return
    note = (f" — {len(res['widenings'])} policy WIDENING(s) flagged on its card"
            if res["widenings"] else "")
    st.success(f"staged `{res['key'][:12]}…` — settings.json is unchanged until your "
               f"click on that exact diff below{note}")


def _env_mark(name):
    """Whether the named variable is set — NEVER its value."""
    return "set" if name in os.environ else "NOT SET"


def _current_config(raw):
    """The connections and channels as the FILE states them (auth refs come from the
    raw text, where they are env:NAME references by construction — the resolved
    values in InstanceConfig.auth are never read by this module)."""
    st.subheader("configured connections")
    for spec in raw.get("instances", []):
        name, adapter = spec.get("name", "?"), spec.get("adapter", "?")
        st.markdown(f"**{html.escape(name)}** · adapter `{html.escape(adapter)}`")
        auth = spec.get("auth") or {}
        if auth:
            refs = " · ".join(
                f"`{html.escape(k)}` → `{html.escape(str(v))}` "
                f"({_env_mark(str(v)[4:]) if str(v).startswith('env:') else '?'})"
                for k, v in sorted(auth.items()))
            st.markdown(f"auth: {refs}", unsafe_allow_html=True)
        chans = spec.get("channels") or []
        if chans:
            body = "".join(
                "<tr>"
                f"<td style='padding:2px 8px'><code>{html.escape(str(c.get('id')))}"
                "</code></td>"
                f"<td style='padding:2px 8px'>{html.escape(c.get('label', ''))}</td>"
                f"<td style='padding:2px 8px'>{html.escape(c.get('reply_policy', 'never'))}"
                "</td>"
                f"<td style='padding:2px 8px'>"
                f"{html.escape(c.get('thread_reply_policy') or '(same as channel)')}</td>"
                "</tr>" for c in chans)
            st.markdown(
                "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
                "<thead><tr><th align='left' style='padding:2px 8px'>channel</th>"
                "<th align='left' style='padding:2px 8px'>label</th>"
                "<th align='left' style='padding:2px 8px'>reply_policy</th>"
                "<th align='left' style='padding:2px 8px'>thread_reply_policy</th></tr>"
                f"</thead><tbody>{body}</tbody></table>", unsafe_allow_html=True)
        else:
            st.markdown("_no monitored channels_")


def _connection_forms(cfg, cfg_path, raw, adapters, instances):
    with st.expander("add connection"):
        with st.form("conf-add-inst", clear_on_submit=True):
            name = st.text_input("instance name", key="confaddinst-name")
            adapter = st.selectbox("adapter (discovered from channels/)", adapters,
                                   key="confaddinst-adapter")
            st.caption("optional auth entry — the NAME of an environment variable, "
                       "never a value")
            akey = st.text_input("auth key (e.g. token)", key="confaddinst-authkey")
            avar = st.text_input("environment variable name",
                                 key="confaddinst-authvar")
            submitted = st.form_submit_button("Stage: add connection")
        if submitted:
            if not name.strip():
                st.error("an instance needs a name")
            else:
                auth = {akey.strip(): avar.strip()} if akey.strip() else None
                _stage_edit(cfg, cfg_path, f"add connection {name.strip()!r} "
                            f"(adapter {adapter})", add_instance,
                            name=name.strip(), adapter=adapter, auth=auth)

    if not instances:
        return
    with st.expander("edit connection (adapter / auth)"):
        with st.form("conf-set-adapter"):
            inst = st.selectbox("instance", instances, key="confsetadapter-inst")
            adapter = st.selectbox("adapter", adapters, key="confsetadapter-adapter")
            submitted = st.form_submit_button("Stage: change adapter")
        if submitted:
            _stage_edit(cfg, cfg_path, f"connection {inst!r}: adapter -> {adapter}",
                        set_adapter, name=inst, adapter=adapter)
        with st.form("conf-set-auth", clear_on_submit=True):
            inst = st.selectbox("instance", instances, key="confsetauth-inst")
            akey = st.text_input("auth key", key="confsetauth-key")
            avar = st.text_input("environment variable name", key="confsetauth-var")
            submitted = st.form_submit_button("Stage: set auth entry")
        if submitted:
            if not akey.strip():
                st.error("an auth entry needs a key")
            else:
                _stage_edit(cfg, cfg_path,
                            f"connection {inst!r}: auth {akey.strip()!r} -> "
                            f"env:{avar.strip()}", set_auth, name=inst,
                            key=akey.strip(), env_var=avar.strip())
        auth_pairs = [f"{s.get('name')} · {k}" for s in raw.get("instances", [])
                      for k in sorted(s.get("auth") or {})]
        if auth_pairs:
            with st.form("conf-del-auth"):
                pair = st.selectbox("auth entry", auth_pairs, key="confdelauth-pair")
                submitted = st.form_submit_button("Stage: remove auth entry")
            if submitted:
                inst, akey = pair.split(" · ", 1)
                _stage_edit(cfg, cfg_path, f"connection {inst!r}: remove auth "
                            f"{akey!r}", remove_auth, name=inst, key=akey)

    with st.expander("remove connection"):
        with st.form("conf-del-inst"):
            inst = st.selectbox("instance", instances, key="confdelinst-inst")
            submitted = st.form_submit_button("Stage: remove connection")
        if submitted:
            _stage_edit(cfg, cfg_path, f"remove connection {inst!r}",
                        remove_instance, name=inst)


def _channel_forms(cfg, cfg_path, raw, instances):
    if not instances:
        return
    with st.expander("add monitored channel"):
        with st.form("conf-add-chan", clear_on_submit=True):
            inst = st.selectbox("instance", instances, key="confaddchan-inst")
            cid = st.text_input("channel id", key="confaddchan-id")
            label = st.text_input("label", key="confaddchan-label")
            # index 0 = never: the DEFAULT the form offers is the deny the loader
            # enforces; picking anything wider is an explicit act and stages a
            # flagged WIDENING below.
            policy = st.selectbox("reply_policy", POLICIES, index=0,
                                  key="confaddchan-policy")
            thread = st.selectbox("thread_reply_policy", (SAME,) + POLICIES,
                                  index=0, key="confaddchan-thread")
            submitted = st.form_submit_button("Stage: add channel")
        if submitted:
            if not cid.strip():
                st.error("a channel needs an id")
            else:
                _stage_edit(
                    cfg, cfg_path, f"instance {inst!r}: add channel {cid.strip()!r}",
                    add_channel, instance=inst, channel_id=cid.strip(),
                    label=label.strip(),
                    # "never" is passed as None so the KEY IS OMITTED and the
                    # loader's own default-deny is what holds (core/reconfig rule).
                    reply_policy=None if policy == "never" else policy,
                    thread_reply_policy=None if thread == SAME else thread)

    pairs = [f"{s.get('name')} · {c.get('id')}" for s in raw.get("instances", [])
             for c in (s.get("channels") or [])]
    if not pairs:
        return
    with st.expander("edit monitored channel"):
        with st.form("conf-edit-chan"):
            pair = st.selectbox("channel", pairs, key="confeditchan-pair")
            label = st.text_input("label (blank = keep)", key="confeditchan-label")
            policy = st.selectbox("reply_policy", (UNCHANGED,) + POLICIES,
                                  key="confeditchan-policy")
            thread = st.selectbox("thread_reply_policy",
                                  (UNCHANGED, SAME) + POLICIES,
                                  key="confeditchan-thread")
            submitted = st.form_submit_button("Stage: edit channel")
        if submitted:
            inst, cid = pair.split(" · ", 1)
            _stage_edit(
                cfg, cfg_path, f"instance {inst!r}: edit channel {cid!r}",
                update_channel, instance=inst, channel_id=cid,
                label=label.strip() if label.strip() else KEEP,
                reply_policy=KEEP if policy == UNCHANGED else
                (None if policy == "never" else policy),
                thread_reply_policy=KEEP if thread == UNCHANGED else
                (None if thread == SAME else thread))
    with st.expander("remove monitored channel"):
        with st.form("conf-del-chan"):
            pair = st.selectbox("channel", pairs, key="confdelchan-pair")
            submitted = st.form_submit_button("Stage: remove channel")
        if submitted:
            inst, cid = pair.split(" · ", 1)
            _stage_edit(cfg, cfg_path, f"instance {inst!r}: remove channel {cid!r}",
                        remove_channel, instance=inst, channel_id=cid)


def _staged_card(cfg, cfg_path, row, current_text):
    wide = json.loads(row["widenings"])
    st.markdown(
        "<div style='border:1px solid rgba(128,128,128,.3);border-left:"
        "3px solid #fab219;border-radius:8px;padding:10px 14px;margin:2px 0'>"
        f"<b>STAGED</b> · {html.escape(row['summary'])} · key "
        f"<code>{html.escape(row['key'][:12])}…</code></div>",
        unsafe_allow_html=True)
    if wide:
        st.error("POLICY WIDENING — this diff makes the engine MORE able to post:\n\n"
                 + "\n".join(f"- {w}" for w in wide))
    if row["old_text"] != current_text:
        st.warning("STALE — settings.json changed since this was staged; Apply will "
                   "refuse. Discard this card and re-stage from the current file.")
    st.code(row["diff"], language="diff")
    approve, reject = st.columns(2)
    approve.button("Apply — this exact diff", key=f"confapply-{row['key']}",
                   on_click=_apply_clicked, args=(cfg, cfg_path, row["key"]))
    reject.button("Discard", key=f"confdiscard-{row['key']}",
                  on_click=_discard_clicked, args=(cfg, cfg_path, row["key"]))


def render(cfg, cfg_path):
    st.divider()
    st.header("connections & monitored channels")
    st.caption("Editing STAGES an exact settings.json diff — nothing is written until "
               "your click on that diff below. Discard is terminal and kept. New "
               "channels default to reply_policy 'never' (deny); a widened policy is "
               "flagged in red on its card. Auth is environment-variable NAMES only — "
               "values never appear on this surface.")
    for kind, msg in st.session_state.pop("config_flash", []):
        getattr(st, kind)(msg)

    cfg_path = Path(cfg_path)
    raw = json.loads(cfg_path.read_text())     # the display truth is the FILE
    adapters = sorted(discover_adapters(cfg.channels_dir))
    instances = [s.get("name") for s in raw.get("instances", [])]

    _current_config(raw)
    st.subheader("stage a change")
    _connection_forms(cfg, cfg_path, raw, adapters, instances)
    _channel_forms(cfg, cfg_path, raw, instances)

    # ---- the gate: staged diffs, one click each ----------------------------
    st.subheader("staged config changes — waiting for you")
    current_text = cfg_path.read_text()
    staged = _rows(_stage_db(cfg), (STAGED,))
    for row in staged:
        _staged_card(cfg, cfg_path, row, current_text)
    if not staged:
        st.markdown("_no staged config changes_")

    applied = len(_rows(_stage_db(cfg), (APPLIED,)))
    discarded = len(_rows(_stage_db(cfg), (DISCARDED,)))
    if applied or discarded:
        st.caption(f"{applied} applied · {discarded} discarded — every card is kept "
                   "in the stage record as the audit trail of what reached this gate")
