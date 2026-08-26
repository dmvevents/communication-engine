#!/usr/bin/env python3
"""scripts/dashboard.py — the operator dashboard (ENH-8; quickstart step 9).

The Streamlit shell over core/dashboard.py: YOUR settings.json resolves YOUR
journal.db and per-instance outboxes (config's `outbox_path_for`, the same split the
write side uses), and everything on screen comes from that one read-only snapshot.

Read-only twice over: core/outbox is never imported (tests/test_dashboard.py holds an
AST check on this file, like the scheduler's), and every database connection is
core/dashboard's sqlite mode=ro — so no bug here can post as anyone or touch the
audit trail it displays.

Run it through scripts/dashboard-serve.sh, which binds 127.0.0.1 ONLY; a remote
operator tunnels in (`ssh -L 8502:127.0.0.1:8502 <host>`). Config comes from the
COMMS_SETTINGS environment variable, default ./settings.json — an env var rather
than argv because `streamlit run` owns the command line.

UX carried over from the UI this ports (each lesson was paid for):
  * attention first — what needs a human is stated before anything scrollable;
  * severity + glyph + word — colour is the redundant channel, never the only one,
    and the glyphs live in ordinary text fonts (emoji render as tofu boxes wherever
    no emoji font exists, which silently destroys the non-colour channel);
  * real markup only — canvas grids (st.dataframe) are invisible to screen readers,
    copy/paste and search, so every table here is a real DOM table.
"""
import html
import os
import sys
import time
from pathlib import Path

# Runs from a fresh clone with no install step, so the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ConfigError, load  # noqa: E402
from core.dashboard import SEVERITY, snapshot  # noqa: E402

try:
    import streamlit as st
except ImportError:                                    # pragma: no cover
    sys.exit("scripts/dashboard.py needs streamlit — the one optional extra; the "
             "engine core never imports it. pip install streamlit, then run "
             "scripts/dashboard-serve.sh")

st.set_page_config(page_title="communication engine — operator", page_icon="◉",
                   layout="wide")

# Glyph + word + colour per severity; the glyphs are text-font characters on purpose.
GLYPH = {"in_flight": "✖", "edited_after_response": "▲",
         "staged": "◔", "unanswered": "?"}
COLOR = {"in_flight": "#d03b3b", "edited_after_response": "#ec835a",
         "staged": "#fab219", "unanswered": "#898781"}
LABEL = {"in_flight": "IN-FLIGHT SEND", "edited_after_response": "EDITED AFTER ANSWER",
         "staged": "STAGED DRAFT", "unanswered": "UNANSWERED"}


def dom_table(headers, rows):
    """A real <table>: present in the DOM, the accessibility tree, and ctrl-F."""
    head = "<thead><tr>" + "".join(
        f"<th align='left' style='padding:4px 8px'>{html.escape(h)}</th>"
        for h in headers) + "</tr></thead>"
    body = "".join(
        "<tr>" + "".join(
            "<td style='padding:4px 8px;vertical-align:top;"
            f"border-top:1px solid rgba(128,128,128,.25)'>{c}</td>"
            for c in row) + "</tr>"
        for row in rows)
    st.markdown("<table style='width:100%;border-collapse:collapse;font-size:13px'>"
                + head + "<tbody>" + body + "</tbody></table>",
                unsafe_allow_html=True)


def badge(severity):
    return (f"<span style='color:{COLOR[severity]};font-weight:700'>"
            f"{GLYPH[severity]}</span> <b>{LABEL[severity]}</b>")


# ---- config: the adopter's, never a hardwired path --------------------------
cfg_path = Path(os.environ.get("COMMS_SETTINGS", "settings.json"))
if not cfg_path.is_file():
    st.error(f"no config at {cfg_path.resolve()} — set "
             "COMMS_SETTINGS=/path/to/settings.json (or run from the directory "
             "holding it; quickstart step 2 creates one)")
    st.stop()
try:
    cfg = load(cfg_path)
except ConfigError as ex:
    st.error(f"config refused: {ex}")
    st.stop()

outbox_paths = {inst.name: cfg.outbox_path_for(inst.name) for inst in cfg.instances}
snap = snapshot(cfg.journal_path, outbox_paths)

with st.sidebar:
    st.markdown("### communication engine")
    st.caption(f"config: `{cfg_path.resolve()}`")
    st.caption(f"instances: {', '.join(sorted(outbox_paths))}")
    if st.button("↻ Re-read state"):
        st.rerun()
    st.caption(f"read {time.strftime('%H:%M:%SZ', time.gmtime())} · every connection "
               "is read-only (sqlite mode=ro); this surface cannot send or edit")

st.title("operator dashboard")
st.caption("What needs a human, from your journal and outbox — severity first, "
           "re-read from the files on every refresh.")

if snap["missing"]:
    st.warning("state not created yet — " + "; ".join(
        f"`{m}`" for m in snap["missing"]) +
        ". A fresh adopter grows these by running the engine: "
        "scripts/first-poll.py writes the journal (quickstart step 5), and the "
        "outbox appears on the first staged or sent draft. Missing is reported, "
        "never rendered as zero.")

# ---- triage first ------------------------------------------------------------
attention = snap["attention"]
if not attention and not snap["missing"]:
    st.markdown("<div style='border:1px solid rgba(128,128,128,.3);"
                "border-left:3px solid #0ca30c;border-radius:8px;padding:10px 14px'>"
                "<span style='color:#0ca30c;font-weight:700'>✓</span> <b>ALL CLEAR</b>"
                " — no in-flight sends, no stale answers, no drafts waiting, no open "
                "asks.</div>", unsafe_allow_html=True)
for item in attention:
    where = item["where"] + (f" · instance {item['instance']}"
                             if item["instance"] else "")
    st.markdown(
        f"<div style='border:1px solid rgba(128,128,128,.3);border-left:3px solid "
        f"{COLOR[item['severity']]};border-radius:8px;padding:10px 14px;"
        f"margin:2px 0 8px'>{badge(item['severity'])} · {html.escape(where)} · "
        f"ts {html.escape(str(item['ts']))}"
        f"<div style='opacity:.75;font-size:.85rem'>{html.escape(item['why'])}</div>"
        f"<div style='font-size:.9rem;margin-top:4px'>"
        f"{html.escape(item['text'] or '')}</div></div>",
        unsafe_allow_html=True)

tiles = st.columns(len(SEVERITY) + 1)
j = snap["journal"]
# None means the file is missing: UNKNOWN, deliberately not 0.
tiles[0].metric("Distinct messages", "—" if j is None else j["distinct"])
for i, sev in enumerate(SEVERITY, start=1):
    tiles[i].metric(LABEL[sev].title(),
                    sum(1 for a in attention if a["severity"] == sev))

# ---- the detail tabs ----------------------------------------------------------
journal_tab, outbox_tab = st.tabs(["Journal", "Outbox"])

with journal_tab:
    if j is None:
        st.markdown("_No journal file yet — nothing has been polled._")
    else:
        st.markdown(f"**{j['distinct']}** distinct messages · "
                    f"**{j['unanswered']}** unanswered · **{j['answered']}** answered")
        dom_table(("kind", "count"),
                  [(html.escape(str(k)), n)
                   for k, n in sorted(j["by_kind"].items(), key=lambda kv: str(kv[0]))])
        open_asks = [a for a in attention if a["severity"] in
                     ("unanswered", "edited_after_response")]
        if open_asks:
            st.markdown("##### Needs an answer")
            dom_table(("", "channel", "ts", "kind", "text"),
                      [(badge(a["severity"]), html.escape(a["where"]),
                        html.escape(str(a["ts"])), html.escape(str(a["kind"] or "—")),
                        html.escape(a["text"] or ""))
                       for a in open_asks])
    st.caption(f"source: `{cfg.journal_path}`")

with outbox_tab:
    for name in sorted(snap["outbox"]):
        counts = snap["outbox"][name]
        st.markdown(f"##### instance `{html.escape(name)}`")
        if counts is None:
            st.markdown("_No outbox file yet — it is created on the first staged or "
                        "sent draft._")
            continue
        dom_table(("state", "count"), sorted(counts.items()))
        gate = [a for a in attention
                if a["severity"] in ("staged", "in_flight") and a["instance"] == name]
        if gate:
            st.markdown("**Waiting on you** — drafts to approve, crashed sends to "
                        "recover:")
            dom_table(("", "target", "trigger ts", "exact text"),
                      [(badge(a["severity"]), html.escape(a["where"]),
                        html.escape(str(a["ts"])), html.escape(a["text"] or ""))
                       for a in gate])
        st.caption(f"source: `{outbox_paths[name]}`")
