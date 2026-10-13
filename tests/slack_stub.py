"""A loopback Slack Web API stub for hermetic tests (ENH-31 C).

Stateful, in-process, and bound to 127.0.0.1:0 — never any other interface. Channels
hold raw Slack-shaped messages; `chat.postMessage` appends one and returns its ts;
`conversations.history` serves TOP-LEVEL messages newest-first, as Slack does, with
message metadata only when `include_all_metadata` is set; `conversations.replies`
serves the parent plus its replies oldest-first; `auth.test` names the bot. A 429 can
be planted per method with a Retry-After, and an `ok:false` error likewise. Every
request is recorded with its Authorization header, so a test can assert the token
travelled in the header and never in the form.

Slack escapes `&`, `<` and `>` in stored text; `slack_escapes=True` reproduces that so
a read-back comparison is tested against both storage shapes. Nothing here ever talks
to slack.com: the adapter reaches the stub through COMMS_SLACK_API_BASE.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackStub:
    def __init__(self, bot_user="U_BOT", slack_escapes=False):
        self.channels: dict = {}          # channel id -> [raw message dicts, ts ascending]
        self.requests: list = []          # (method, form dict, authorization header)
        self.rate_limit: dict = {}        # method -> Retry-After string, consumed once
        self.fail_next: dict = {}         # method -> error code, consumed once
        self.bot_user = bot_user
        self.slack_escapes = slack_escapes
        self._ts = 1700000000.0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):            # noqa: N802 — http.server's name
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(
                    self.rfile.read(length).decode()).items()}
                method = self.path.lstrip("/").split("?", 1)[0]
                status, headers, body = stub.handle(method, form,
                                                    self.headers.get("Authorization"))
                payload = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the default stderr access log
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ---- lifecycle ----------------------------------------------------------------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # ---- fixture helpers ----------------------------------------------------------
    def next_ts(self) -> str:
        with self._lock:
            self._ts += 1.0
            return f"{self._ts:.6f}"

    def seed(self, channel, text, user="U_A", thread_ts=None, ts=None, **extra):
        """Plant a message as the platform would hold it (text stored as given)."""
        m = {"type": "message", "ts": ts or self.next_ts(), "user": user, "text": text}
        if thread_ts:
            m["thread_ts"] = thread_ts
        m.update(extra)
        self.channels.setdefault(channel, []).append(m)
        return m["ts"]

    def messages(self, channel):
        return list(self.channels.get(channel, []))

    def posted(self, channel):
        """Messages the bot posted through chat.postMessage, in order."""
        return [m for m in self.channels.get(channel, []) if m.get("bot_posted")]

    # ---- the API ------------------------------------------------------------------
    def handle(self, method, form, authorization):
        self.requests.append((method, dict(form), authorization))
        if method in self.rate_limit:
            wait = self.rate_limit.pop(method)
            return 429, {"Retry-After": str(wait)}, {"ok": False, "error": "ratelimited"}
        if method in self.fail_next:
            return 200, {}, {"ok": False, "error": self.fail_next.pop(method)}
        fn = getattr(self, "_m_" + method.replace(".", "_"), None)
        if fn is None:
            return 200, {}, {"ok": False, "error": "unknown_method"}
        return 200, {}, fn(form)

    def _m_auth_test(self, form):
        return {"ok": True, "user_id": self.bot_user, "user": "comms-bot", "team": "T_STUB"}

    def _m_conversations_info(self, form):
        ch = form.get("channel", "")
        return {"ok": True, "channel": {"id": ch, "name": None if ch.startswith("D") else ch.lower(),
                                        "is_im": ch.startswith("D")}}

    def _m_conversations_history(self, form):
        ch = form.get("channel", "")
        inclusive = form.get("inclusive") in ("1", "true", "True")
        oldest, latest = form.get("oldest"), form.get("latest")
        with_meta = form.get("include_all_metadata") in ("1", "true", "True")
        out = []
        for m in self.channels.get(ch, []):
            if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                continue                                   # history never serves replies
            t = float(m["ts"])
            if oldest is not None and (t < float(oldest) if inclusive else t <= float(oldest)):
                continue
            if latest is not None and (t > float(latest) if inclusive else t >= float(latest)):
                continue
            out.append(self._public(m, with_meta))
        out.sort(key=lambda m: float(m["ts"]), reverse=True)   # newest first, as Slack
        limit = int(form.get("limit") or 100)
        return {"ok": True, "messages": out[:limit], "has_more": False}

    def _m_conversations_replies(self, form):
        ch, root = form.get("channel", ""), form.get("ts", "")
        oldest = form.get("oldest")
        out = []
        for m in self.channels.get(ch, []):
            if m["ts"] == root or m.get("thread_ts") == root:
                if oldest is not None and m["ts"] != root and float(m["ts"]) <= float(oldest):
                    continue
                out.append(self._public(m, True))
        out.sort(key=lambda m: float(m["ts"]))
        return {"ok": True, "messages": out, "has_more": False}

    def _m_chat_postMessage(self, form):
        ch, text = form.get("channel", ""), form.get("text", "")
        if not ch or not text:
            return {"ok": False, "error": "no_text" if ch else "channel_not_found"}
        m = {"type": "message", "ts": self.next_ts(), "user": self.bot_user,
             "bot_id": "B_STUB", "bot_posted": True,
             "text": _escape(text) if self.slack_escapes else text}
        if form.get("thread_ts"):
            m["thread_ts"] = form["thread_ts"]
        if form.get("metadata"):
            m["metadata"] = json.loads(form["metadata"])
        self.channels.setdefault(ch, []).append(m)
        return {"ok": True, "channel": ch, "ts": m["ts"], "message": self._public(m, True)}

    @staticmethod
    def _public(m, with_meta):
        out = {k: v for k, v in m.items() if k not in ("bot_posted", "metadata")}
        if with_meta and "metadata" in m:
            out["metadata"] = m["metadata"]
        return out
