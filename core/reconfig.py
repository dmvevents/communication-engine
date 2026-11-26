"""core/reconfig.py — staged configuration changes (ENH-29; the settings write surface).

ENH-28 gave the operator a gated write path for MESSAGES: composing stages, only a human
click on the exact staged text sends. This module is the same trust shape for
CONFIGURATION — connections (``instances[]``) and monitored channels (``channels[]``) —
because a config write is at least as consequential as a message: one edited line can
promote a customer channel from ``never`` to ``direct`` and hand every later send to it.

The ladder, in order:

    an edit op    builds a CANDIDATE settings dict from the current file — pure functions,
                  nothing touched on disk.
    stage()       validates the candidate with the ENGINE'S OWN loader (core/config.from_dict
                  — the exact code that will read the file later, so a refusal here is the
                  refusal the adopter would otherwise meet at startup, e.g. the top-level
                  "taxonomy" that ENH-17 exists for), then records the exact old→new text
                  with its unified diff and every POLICY WIDENING, durably, state STAGED.
                  The settings file is not touched.
    apply(key)    the human click on that exact diff. Re-checks that the file still reads
                  byte-for-byte as it did at stage time (a diff staged against yesterday's
                  file describes nothing), re-validates the candidate against the CURRENT
                  environment (the ENH-28 rule: the policy is re-resolved at the click),
                  then writes atomically (temp file + os.replace) and marks APPLIED.
    discard(key)  the terminal rejection. The row is KEPT — deleting it would erase the
                  record that this exact diff reached the gate and a human refused it.

Rules this module enforces, each with a mutation in tests/mutation_check.sh:

* **Staging never applies.** No edit op and no stage() call may reach the settings file;
  only apply(), on a specific staged row, writes — the config twin of "compose never
  sends".
* **A raw secret is never accepted, stored, echoed, or written.** Auth values are
  ``env:NAME`` references and the NAME must be a valid environment-variable identifier,
  checked BEFORE anything persists and before the loader can echo a prefix of a pasted
  token into an error message. The refusal names the length of the offending string,
  never its content.
* **A new channel denies by default BY OMISSION.** add_channel() with no stated policy
  writes no ``reply_policy`` key at all, so the loader's own DEFAULT DENY is what holds —
  a "never" this module wrote would be a second copy of that rule, free to drift.
* **A widening is loud.** Any change that makes the engine MORE able to post — a policy
  rising toward ``direct``, on either placement scope, including a brand-new channel born
  above ``never`` — is detected at stage time and carried on the staged row, so the
  surface can flag it on the exact card the human clicks.
* **The applied truth is stated.** There is NO hot-reload: scripts/scheduler.py and every
  watcher load settings once at startup. apply() says so (RELOAD_TRUTH) instead of letting
  "applied" read as "live everywhere".
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from core.config import from_dict

STAGED, APPLIED, DISCARDED = "STAGED", "APPLIED", "DISCARDED"

# How much a policy lets the engine post. Absence ranks with "never" on purpose: a
# channel that did not exist could not be posted to, so a new channel born at "staged"
# or "direct" IS a widening, not a neutral addition.
_POLICY_RANK = {"never": 0, "staged": 1, "direct": 2}

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The honest apply semantics, stated once so the surface and the tests pin the same
# sentence. The dashboard re-reads settings.json on every rerun, so it shows the new
# configuration immediately — but a RUNNING scheduler or watcher loaded its config at
# startup and holds it until it exits. Saying "applied" without this would let the
# operator believe a live loop already obeys the new policy.
RELOAD_TRUTH = ("applied — this dashboard re-reads settings.json on every rerun, but a "
                "running scheduler or watcher loads settings only at startup: restart it "
                "to pick this change up (there is no hot-reload path)")


class StageError(ValueError):
    """The staged-config request is unusable. Refuse loudly; never guess."""


class StaleStage(StageError):
    """The settings file changed since this diff was staged — the staged old→new no
    longer describes reality, so applying it would silently destroy the newer edit."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS confstage (
    key         TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    old_text    TEXT NOT NULL,
    new_text    TEXT NOT NULL,
    diff        TEXT NOT NULL,
    widenings   TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    applied_at  REAL
);
"""


# ---------------------------------------------------------------------------
# Edit operations — pure candidate builders. Each takes the raw parsed settings
# dict, deep-copies it (the caller's dict is never mutated: an op that edited its
# input would make "the candidate" and "the current config" one object, and the
# staged diff would compare a thing to itself), applies ONE change, and returns
# the candidate. Nothing here reads or writes any file.
# ---------------------------------------------------------------------------

def _shown(value) -> str:
    """Describe an offending value by LENGTH and type only — never by content, because
    the most likely offender is a pasted token, and echoing even a prefix of it into a
    flash message or a log is the leak."""
    return (f"a {len(value)}-char string" if isinstance(value, str)
            else type(value).__name__)


def _bare_env_name(instance: str, key: str, var) -> str:
    """An auth entry names an environment VARIABLE, never a value: the bare NAME must
    be a valid identifier. Anything else is refused unstored and unechoed."""
    if isinstance(var, str) and _ENV_NAME.fullmatch(var):
        return var
    raise StageError(
        f"instance {instance!r} auth {key!r}: expected the NAME of an environment "
        f"variable (letters, digits, '_'); got {_shown(var)} — raw secrets never enter "
        "configuration, the staged record, or an error message")


def _check_auth_refs(raw: dict) -> None:
    """Sweep every auth value BEFORE the loader sees the candidate: core/config's own
    refusal echoes the first characters of a non-env value into its message, which is
    correct for a settings file an operator already wrote but wrong for a UI where the
    value may be a secret pasted seconds ago."""
    for spec in raw.get("instances") or []:
        name = spec.get("name", "?")
        for key, value in (spec.get("auth") or {}).items():
            if not (isinstance(value, str) and value.startswith("env:")):
                raise StageError(
                    f"instance {name!r} auth {key!r}: the value must be an 'env:NAME' "
                    f"reference; got {_shown(value)}, refused unstored and unechoed")
            _bare_env_name(name, key, value[4:])


def _instance(raw: dict, name: str) -> dict:
    for spec in raw.get("instances") or []:
        if spec.get("name") == name:
            return spec
    known = sorted(s.get("name", "?") for s in raw.get("instances") or [])
    raise StageError(f"no instance named {name!r} — configured: {known or '(none)'}")


def _channel(spec: dict, channel_id: str) -> dict:
    for ch in spec.get("channels") or []:
        if ch.get("id") == channel_id:
            return ch
    known = sorted(c.get("id", "?") for c in spec.get("channels") or [])
    raise StageError(f"instance {spec.get('name', '?')!r} has no channel {channel_id!r} "
                     f"— configured: {known or '(none)'}")


def add_instance(raw: dict, name: str, adapter: str, auth: dict | None = None) -> dict:
    """A new connection. `auth` maps auth keys to environment-variable NAMES (bare,
    e.g. {"token": "MY_SLACK_TOKEN"}) — the op writes the ``env:`` form itself, so a
    caller cannot hand a literal value through even by mistake. The adapter name is
    validated by the loader at stage time against the DISCOVERED set (R11), so a type
    this filesystem does not ship is refused there by name."""
    raw = copy.deepcopy(raw)
    if any(s.get("name") == name for s in raw.get("instances") or []):
        raise StageError(f"an instance named {name!r} already exists — the name is the "
                         "tenant-isolation key (ENH-7); edit that instance instead")
    spec: dict = {"name": name, "adapter": adapter}
    if auth:
        spec["auth"] = {k: f"env:{_bare_env_name(name, k, v)}"
                        for k, v in auth.items()}
    raw.setdefault("instances", []).append(spec)
    return raw


def remove_instance(raw: dict, name: str) -> dict:
    raw = copy.deepcopy(raw)
    _instance(raw, name)                      # refuse an unknown name loudly
    raw["instances"] = [s for s in raw["instances"] if s.get("name") != name]
    return raw


def set_adapter(raw: dict, name: str, adapter: str) -> dict:
    raw = copy.deepcopy(raw)
    _instance(raw, name)["adapter"] = adapter
    return raw


def set_auth(raw: dict, name: str, key: str, env_var: str) -> dict:
    """Set or replace ONE auth entry. `env_var` is the bare variable NAME; the op
    writes the ``env:`` reference form."""
    raw = copy.deepcopy(raw)
    value = _bare_env_name(name, key, env_var)
    _instance(raw, name).setdefault("auth", {})[key] = f"env:{value}"
    return raw


def remove_auth(raw: dict, name: str, key: str) -> dict:
    raw = copy.deepcopy(raw)
    spec = _instance(raw, name)
    if key not in (spec.get("auth") or {}):
        raise StageError(f"instance {name!r} has no auth entry {key!r} — configured: "
                         f"{sorted(spec.get('auth') or {}) or '(none)'}")
    del spec["auth"][key]
    if not spec["auth"]:
        del spec["auth"]
    return raw


KEEP = object()           # sentinel: "leave this field as it is"


def add_channel(raw: dict, instance: str, channel_id: str, label: str = "",
                reply_policy: str | None = None,
                thread_reply_policy: str | None = None) -> dict:
    """A new monitored channel. DEFAULT DENY BY OMISSION: with no stated policy the op
    writes no ``reply_policy`` key at all, so the loader's own default — ``never`` —
    is the single copy of that rule. Writing "never" here would be a second copy, free
    to drift from the one the engine actually enforces."""
    raw = copy.deepcopy(raw)
    spec = _instance(raw, instance)
    if any(c.get("id") == channel_id for c in spec.get("channels") or []):
        raise StageError(f"instance {instance!r} already monitors {channel_id!r} — two "
                         "rows for one id would make every later edit ambiguous; edit "
                         "the existing row instead")
    ch: dict = {"id": channel_id}
    if label:
        ch["label"] = label
    if reply_policy is not None:
        ch["reply_policy"] = reply_policy
    if thread_reply_policy is not None:
        ch["thread_reply_policy"] = thread_reply_policy
    spec.setdefault("channels", []).append(ch)
    return raw


def remove_channel(raw: dict, instance: str, channel_id: str) -> dict:
    raw = copy.deepcopy(raw)
    spec = _instance(raw, instance)
    _channel(spec, channel_id)                # refuse an unknown id loudly
    spec["channels"] = [c for c in spec["channels"] if c.get("id") != channel_id]
    return raw


def update_channel(raw: dict, instance: str, channel_id: str, label=KEEP,
                   reply_policy=KEEP, thread_reply_policy=KEEP) -> dict:
    """Edit one monitored channel. KEEP leaves a field untouched; None REMOVES the
    key — meaningful on both policies: a removed ``reply_policy`` falls back to the
    loader's DENY, a removed ``thread_reply_policy`` back to "same as the channel"."""
    raw = copy.deepcopy(raw)
    ch = _channel(_instance(raw, instance), channel_id)
    for field, value in (("label", label), ("reply_policy", reply_policy),
                         ("thread_reply_policy", thread_reply_policy)):
        if value is KEEP:
            continue
        if value is None:
            ch.pop(field, None)
        else:
            ch[field] = value
    return raw


# ---------------------------------------------------------------------------
# Widening detection — the config change that must never pass quietly.
# ---------------------------------------------------------------------------

def _effective(raw: dict) -> dict:
    """(instance, channel, scope) -> the policy the ENGINE would enforce, from a raw
    settings dict: an absent reply_policy is ``never`` (the loader's DEFAULT DENY) and
    an absent thread_reply_policy is the channel's own policy (ENH-3)."""
    out = {}
    for spec in raw.get("instances") or []:
        for ch in spec.get("channels") or []:
            channel_policy = ch.get("reply_policy", "never")
            out[(spec.get("name"), ch.get("id"), "channel")] = channel_policy
            out[(spec.get("name"), ch.get("id"), "thread")] = \
                ch.get("thread_reply_policy") or channel_policy
    return out


def widenings(old_raw: dict, new_raw: dict) -> list:
    """Every (instance, channel, scope) whose effective reply policy became MORE
    permissive. A key absent on the old side ranks as ``never`` — a channel that did
    not exist could not be posted to, so arriving at ``direct`` is the widest widening
    there is. Removals and narrowings return nothing: the loud direction is the one
    that grants the engine a voice it did not have."""
    old, new = _effective(old_raw), _effective(new_raw)
    out = []
    for key in sorted(new, key=str):
        before, after = old.get(key, "never"), new[key]
        if _POLICY_RANK.get(after, 0) > _POLICY_RANK.get(before, 0):
            inst, cid, scope = key
            out.append(f"instance {inst!r} channel {cid!r} ({scope}): "
                       f"{before} -> {after}")
    return out


# ---------------------------------------------------------------------------
# The staged-apply store.
# ---------------------------------------------------------------------------

def _render(raw: dict) -> str:
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _key(old_text: str, new_text: str) -> str:
    h = hashlib.sha256()
    h.update(old_text.encode()); h.update(b"\x00"); h.update(new_text.encode())
    return h.hexdigest()


class ConfigStage:
    """Durable staged settings changes for ONE settings file. Constructed only by an
    action (the dashboard_write rule: rendering reads via mode=ro helpers; a surface
    that mints state by being looked at is the disease the viewer's tests forbid)."""

    def __init__(self, db_path, settings_path, env: dict | None = None):
        self.settings_path = Path(settings_path)
        self.env = os.environ if env is None else env
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- validation --------------------------------------------------------
    def _validate(self, new_raw: dict) -> None:
        """The candidate must satisfy the ENGINE'S OWN loader — core/config.from_dict,
        the exact code that reads the file at startup — so every refusal the adopter
        would meet later (unknown keys, undiscovered adapters, invalid policies,
        missing env vars) surfaces HERE, before anything is staged. The auth sweep runs
        first so a pasted secret is refused before the loader can echo any of it."""
        _check_auth_refs(new_raw)
        from_dict(copy.deepcopy(new_raw), base_dir=self.settings_path.parent,
                  env=self.env)

    # -- the ladder ---------------------------------------------------------
    def stage(self, new_raw: dict, summary: str = "") -> dict:
        """Validate and durably record old→new. The settings file is NOT touched:
        staging a config change applies nothing, exactly as composing sends nothing."""
        self._validate(new_raw)
        old_text = self.settings_path.read_text()
        if json.loads(old_text) == new_raw:
            raise StageError("no effective change — the candidate parses identical to "
                             "the current settings; staging it would put an empty diff "
                             "at the gate for a human to approve")
        new_text = _render(new_raw)
        key = _key(old_text, new_text)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=str(self.settings_path), tofile=f"{self.settings_path} (staged)"))
        wide = widenings(json.loads(old_text), new_raw)
        now = time.time()
        # INSERT OR IGNORE: the same exact old→new staged twice is one decision, not
        # two cards (the outbox's concurrent-claim arbitration, R2).
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO confstage (key, summary, old_text, new_text, diff, "
            "widenings, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, summary, old_text, new_text, diff, json.dumps(wide), STAGED, now, now))
        self.conn.commit()
        if cur.rowcount == 0:
            row = self._row(key)
            return {"key": key, "state": row["state"], "deduped": True}
        return {"key": key, "state": STAGED, "staged": True, "deduped": False,
                "diff": diff, "widenings": wide}

    def apply(self, key: str) -> dict:
        """The human click on the exact staged diff — the ONLY path that writes the
        settings file."""
        row = self._row(key)
        if row is None:
            raise StageError(f"no staged change {key[:12]}… — nothing to apply")
        if row["state"] == APPLIED:
            # Already on the file: report it, never write twice (the outbox's
            # already-delivered dedupe).
            return {"key": key, "state": APPLIED, "deduped": True,
                    "reload": RELOAD_TRUTH}
        if row["state"] == DISCARDED:
            raise StageError("this diff was DISCARDED — a human refused it, and that "
                             "refusal is terminal; stage the change again if it is "
                             "wanted after all")
        current = self.settings_path.read_text()
        if current == row["new_text"]:
            # The file already carries the exact staged text: a crash between the
            # write and the APPLIED mark, or an identical hand edit. Record the truth
            # instead of refusing it (the outbox recovery rule: prove, then mark —
            # never re-do).
            self._mark(key, APPLIED, applied=True)
            return {"key": key, "state": APPLIED, "deduped": False, "recovered": True,
                    "reload": RELOAD_TRUTH}
        if current != row["old_text"]:
            raise StaleStage(
                "settings.json changed since this diff was staged — applying it now "
                "would silently overwrite the newer edit. Discard this card and make "
                "the change again from the current file.")
        # Re-validated at the CLICK, against the CURRENT environment: an env var that
        # vanished since staging must refuse here, not at the engine's next startup
        # (the ENH-28 rule — the policy is re-resolved when the human acts, not when
        # the draft was written).
        self._validate(json.loads(row["new_text"]))
        tmp = self.settings_path.with_name(self.settings_path.name + ".staged.tmp")
        tmp.write_text(row["new_text"])
        os.replace(tmp, self.settings_path)   # atomic: readers see old or new, never half
        self._mark(key, APPLIED, applied=True)
        return {"key": key, "state": APPLIED, "deduped": False, "reload": RELOAD_TRUTH}

    def discard(self, key: str) -> dict:
        """Terminal, and KEPT — the record that this exact diff was refused."""
        row = self._row(key)
        if row is None:
            raise StageError(f"no staged change {key[:12]}… — nothing to discard")
        if row["state"] != STAGED:
            raise StageError(f"cannot discard a {row['state']} change — an applied "
                             "diff is on the file (revert it with a new staged edit) "
                             "and a discarded one is already refused")
        self._mark(key, DISCARDED)
        return {"key": key, "state": DISCARDED}

    # -- plumbing ------------------------------------------------------------
    def _row(self, key: str):
        return self.conn.execute("SELECT * FROM confstage WHERE key=?",
                                 (key,)).fetchone()

    def _mark(self, key: str, state: str, applied: bool = False) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE confstage SET state=?, updated_at=?, applied_at=COALESCE(?, "
            "applied_at) WHERE key=?", (state, now, now if applied else None, key))
        self.conn.commit()
