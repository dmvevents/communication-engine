"""core/config.py — build the whole engine from configuration (gate G8; R17, R18).

This module is what makes the engine *adoptable*. A colleague must be able to point it at
their workspace, channels, principals and reply policies **by config alone** — no code edits,
no fork. The incumbent cannot be adopted precisely because it hardcodes one host: tmux pane
names, `/home/<user>/slack`, specific systemd units, one workspace's channel IDs.

Rules enforced here:

* **Every path comes from config**, resolved against an explicit base directory. Nothing in
  this package may reference an absolute home path (a test greps for that and fails).
* **Every credential is an `env:NAME` reference.** A literal-looking secret is rejected, so a
  newcomer cannot paste a token into a file that might get committed.
* **A missing env var fails loudly at load time**, not at first send. Half-configured is the
  worst state to discover during an incident.
* **Default DENY.** A channel with no explicit `reply_policy` is `never`, so a fresh adopter
  cannot accidentally post as anyone.
* **Every key is read or refused** (ENH-17). A key the loader does not read configures
  nothing, and a load that accepts it anyway lies to the adopter — the first non-author
  adoption run planted a top-level "taxonomy" and believed the classifier retuned when
  nothing had changed. Keys starting with `_` are comments (JSON has none; the shipped
  files use `_note`).
* **Channel types are DISCOVERED, never enumerated** (R11). A channel type is a directory
  under the configured `channels_dir` containing `adapter.py` (channels/CONTRACT.md), so a
  new channel lands with zero core/ changes — the incumbent's second channel type was a
  hand-mirrored copy of its first because nothing enforced this. Unknown adapter types are
  still refused loudly, against the discovered set: an adopter who misspells an adapter
  name must be told, not left with a silently inert instance.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_POLICIES = ("never", "staged", "direct")

# ENH-17: every key the loader reads, level by level. A key outside these sets configures
# NOTHING, and silently inert configuration is the same defect class as the silently inert
# instance (R11): the first non-author adoption run (2026-08-25) planted "taxonomy" at the
# top level, watched the load succeed, and believed the classifier retuned when nothing
# had changed. Growing a set here is how a new key is declared read.
_TOP_KEYS = frozenset(("engine", "instances"))
_ENGINE_KEYS = frozenset(("state_dir", "channels_dir", "store", "journal", "outbox"))
_INSTANCE_KEYS = frozenset(("name", "adapter", "channels", "principals", "auth",
                            "taxonomy"))
_CHANNEL_KEYS = frozenset(("id", "label", "poll_interval_s", "reply_policy",
                           "thread_reply_policy", "triggers"))

# Shapes that indicate a real credential pasted where an env reference belongs.
_SECRET_SHAPES = (
    re.compile(r"^xox[abprs]-"),          # Slack tokens
    re.compile(r"^xapp-"),
    re.compile(r"^\d{8,10}:AA"),          # Telegram bot tokens
    re.compile(r"^(AKIA|ASIA)[0-9A-Z]{16}$"),
)


class ConfigError(ValueError):
    """The configuration is unusable. Never fall back to a default and continue."""


@dataclass
class ChannelConfig:
    id: str
    label: str = ""
    poll_interval_s: int = 60
    reply_policy: str = "never"          # DEFAULT DENY
    # ENH-3: None means "same policy in a thread as in the channel". Set it to express
    # a placement policy such as "answer in thread, never the main channel", which is
    # a visible behavioural difference an adopter could not previously configure.
    thread_reply_policy: str | None = None
    triggers: tuple = ()

    def __post_init__(self):
        declared = [("reply_policy", self.reply_policy)]
        if self.thread_reply_policy is not None:      # absent means "as the channel"
            declared.append(("thread_reply_policy", self.thread_reply_policy))
        for key, value in declared:
            if value not in VALID_POLICIES:
                raise ConfigError(
                    f"channel {self.id}: {key} {value!r} is not one of "
                    f"{VALID_POLICIES}")

    def policy(self):
        """The policy value core.outbox indexes by target.

        A plain string when both placements share a policy — so every config written
        before thread awareness produces the byte-identical policy map — and a
        per-scope dict only once the two differ.
        """
        if self.thread_reply_policy is None:
            return self.reply_policy
        return {"channel": self.reply_policy, "thread": self.thread_reply_policy}


@dataclass
class InstanceConfig:
    name: str
    adapter: str
    channels: tuple = ()
    principals: tuple = ()
    auth: dict = field(default_factory=dict)
    taxonomy: dict = field(default_factory=dict)

    def policies(self) -> dict:
        """target id -> policy, for core.outbox. Absent targets are denied by default."""
        return {c.id: c.policy() for c in self.channels}


@dataclass
class EngineConfig:
    base_dir: Path
    state_dir: Path
    store_path: Path
    journal_path: Path
    outbox_path: Path
    channels_dir: Path
    instances: tuple = ()

    def instance(self, name: str) -> InstanceConfig:
        for i in self.instances:
            if i.name == name:
                return i
        raise ConfigError(f"no instance named {name!r}")

    def outbox_path_for(self, name: str) -> Path:
        """The outbox file for ONE instance (ENH-7). Outbox state must be per instance:
        recovery re-sends whatever unfinished rows it finds through ITS OWN adapter, so
        two tenants sharing a file would let tenant A's recovery post tenant B's message
        with A's credentials — and A's delivery would dedupe B's identical reply away,
        because the idempotency key is deliberately instance-free (it is the
        platform-visible identity). `outbox_path` stays as the base the per-instance
        files derive from. The name is resolved first so a typo refuses loudly instead
        of minting a fresh empty outbox that hides every pending draft from view."""
        self.instance(name)
        p = self.outbox_path
        return p.with_name(f"{p.stem}-{name}{p.suffix}")


def resolve_secret(value: str, env: dict | None = None) -> str:
    """Resolve an `env:NAME` reference. Refuses literal secrets and missing vars."""
    env = os.environ if env is None else env
    if not isinstance(value, str):
        raise ConfigError(f"credential must be a string 'env:NAME' reference, got "
                          f"{type(value).__name__}")
    for shape in _SECRET_SHAPES:
        if shape.match(value):
            raise ConfigError(
                "a literal credential was found in configuration. Use 'env:NAME' and keep "
                "the value in the environment — settings files get committed by accident.")
    if not value.startswith("env:"):
        raise ConfigError(f"credential {value[:12]!r}... must be an 'env:NAME' reference")
    name = value[4:]
    if not name:
        raise ConfigError("empty env var name in credential reference")
    if name not in env:
        raise ConfigError(
            f"environment variable {name} is not set — the engine refuses to start "
            "half-configured rather than fail at first send")
    return env[name]


def discover_adapters(channels_dir: str | Path) -> dict:
    """Map adapter type -> its adapter.py, by scanning the configured channels directory.

    The directory IS the registry (R11): core learns what channel types exist from the
    filesystem, so landing a new type is a dir-drop plus config — never a core/ edit.
    A directory without `adapter.py` (docs-only, like a design stub) is NOT a channel
    type: offering it would create the silently inert instance the loud-refusal rule
    exists to prevent.
    """
    channels_dir = Path(channels_dir)
    if not channels_dir.is_dir():
        return {}
    found = {}
    try:
        for d in sorted(channels_dir.iterdir()):
            entry = d / "adapter.py"
            if entry.is_file():
                found[d.name] = entry
    except OSError as ex:
        # ENH-20: channels_dir=/etc in the adoption test escaped as a raw
        # PermissionError traceback mid-walk — an errno names no config key. Discovery
        # stats <type>/adapter.py inside every subdirectory, so the whole tree must be
        # readable.
        raise ConfigError(
            f"channels_dir {channels_dir} is not readable ({ex}) — discovery must stat "
            "adapter.py in every subdirectory; point channels_dir at the engine's "
            "channel tree") from ex
    return found


def load_adapter_class(channels_dir: str | Path, name: str):
    """Import `<channels_dir>/<name>/adapter.py` and return its `Adapter` class.

    The entry point is pinned by convention (class named `Adapter`) so the engine can load
    any conforming type without naming it. Missing entry point fails HERE, at load —
    half-configured is the worst state to discover during an incident.
    """
    entry = Path(channels_dir) / name / "adapter.py"
    if not entry.is_file():
        raise ConfigError(
            f"adapter {name!r}: no adapter.py under {Path(channels_dir) / name} — "
            f"discovered types: {sorted(discover_adapters(channels_dir)) or '(none)'}")
    spec = importlib.util.spec_from_file_location(f"channel_adapter_{name}", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, "Adapter", None)
    if cls is None:
        raise ConfigError(
            f"{entry} defines no class named 'Adapter' — that class is the contract "
            "entry point (channels/CONTRACT.md); refusing to leave an inert instance")
    return cls


def load(path: str | Path, base_dir: str | Path | None = None,
         env: dict | None = None) -> EngineConfig:
    """Load and VALIDATE a settings file. Raises ConfigError on anything ambiguous."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as ex:
        raise ConfigError(f"config is not valid JSON: {ex}") from ex
    return from_dict(raw, base_dir=base_dir or path.parent, env=env)


def _refuse_unknown(obj: dict, known: frozenset, where: str) -> None:
    """Refuse any key the loader will not read (ENH-17). '_'-prefixed keys are comments:
    JSON has no comment syntax, and the shipped settings files document themselves with
    '_note' — refusing those would make every shipped config unloadable."""
    unknown = sorted(k for k in obj if k not in known and not k.startswith("_"))
    if not unknown:
        return
    # The right key at the wrong level is the measured mistake (top-level taxonomy), so
    # point at the real home instead of only rejecting the guess.
    hints = "".join(f" ({k!r} is read per-instance: instances[].{k})"
                    for k in unknown if k in _INSTANCE_KEYS and known is not _INSTANCE_KEYS)
    raise ConfigError(
        f"{where}: unknown key(s) {unknown} are read by nothing — refused so no part of "
        f"the configuration can be silently inert.{hints} Keys read here: "
        f"{sorted(known)}; '_'-prefixed keys are comments.")


def from_dict(raw: dict, base_dir: str | Path, env: dict | None = None) -> EngineConfig:
    base = Path(base_dir)
    _refuse_unknown(raw, _TOP_KEYS, "top level")
    eng = raw.get("engine", {})
    _refuse_unknown(eng, _ENGINE_KEYS, "engine")
    # Relative paths resolve against base_dir, so the same config works on any machine.
    state_dir = _resolve(base, eng.get("state_dir", "state"))
    channels_dir = _resolve(base, eng.get("channels_dir", "channels"))
    cfg = EngineConfig(
        base_dir=base,
        state_dir=state_dir,
        store_path=_resolve(base, eng.get("store", state_dir / "messages.db")),
        journal_path=_resolve(base, eng.get("journal", state_dir / "journal.db")),
        outbox_path=_resolve(base, eng.get("outbox", state_dir / "outbox.db")),
        channels_dir=channels_dir,
    )

    # ENH-20: a state_dir pointing at an existing FILE used to load clean and surface
    # only as ensure_dirs()'s bare FileExistsError at first write — the exact
    # fail-at-load-not-later violation this module's docstring promises against.
    # Validate every directory ensure_dirs() will create, naming the key behind it.
    for key, d in (("state_dir", cfg.state_dir), ("store", cfg.store_path.parent),
                   ("journal", cfg.journal_path.parent),
                   ("outbox", cfg.outbox_path.parent)):
        if d.exists() and not d.is_dir():
            raise ConfigError(
                f"{key}: {d} exists and is not a directory — the engine refuses to "
                "start half-configured rather than fail at first write")

    discovered = discover_adapters(channels_dir)
    instances = []
    for spec in raw.get("instances", []):
        name = spec.get("name")
        adapter = spec.get("adapter")
        if not name:
            raise ConfigError("every instance needs a 'name'")
        # ENH-7: the name IS the isolation key — it namespaces cursors and derives
        # per-instance state filenames — so a collision or a path-shaped name breaks
        # every tenant boundary at once, silently.
        if any(i.name == name for i in instances):
            raise ConfigError(
                f"duplicate instance name {name!r} — the name namespaces cursors and "
                "per-instance state files, so two instances sharing it would silently "
                "merge into one tenant")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ConfigError(
                f"instance name {name!r} cannot name per-instance state files: it must "
                "start with a letter or digit and contain only letters, digits, "
                "'.', '_' or '-'")
        _refuse_unknown(spec, _INSTANCE_KEYS, f"instance {name!r}")
        if adapter not in discovered:
            if not discovered:
                # ENH-20: "unknown adapter ... Discovered: (none)" sent the adoption
                # tester hunting a typo in the adapter NAME when the fault was the
                # DIRECTORY. Nothing discovered is never a spelling problem.
                detail = (("is not a directory" if channels_dir.exists()
                           else "does not exist") if not channels_dir.is_dir()
                          else "contains no channel types (no <type>/adapter.py in it)")
                raise ConfigError(
                    f"instance {name!r}: channels_dir {channels_dir} {detail}, so no "
                    f"adapter (including {adapter!r}) can be offered — fix "
                    "channels_dir, not the adapter name")
            raise ConfigError(
                f"instance {name!r}: unknown adapter {adapter!r}. Discovered under "
                f"{channels_dir}: {sorted(discovered)}. "
                "A misspelled adapter must fail loudly, not leave an inert instance.")
        channels = []
        for ch in spec.get("channels", []):
            if not ch.get("id"):
                raise ConfigError(f"instance {name!r}: a channel is missing 'id'")
            # A typo'd reply_policy here is the nastiest inert-key shape: without the
            # refusal the channel silently stays at default DENY and the adopter's
            # 'staged' never takes effect anywhere.
            _refuse_unknown(ch, _CHANNEL_KEYS,
                            f"instance {name!r} channel {ch['id']!r}")
            channels.append(ChannelConfig(
                id=ch["id"], label=ch.get("label", ""),
                poll_interval_s=int(ch.get("poll_interval_s", 60)),
                reply_policy=ch.get("reply_policy", "never"),   # DEFAULT DENY
                thread_reply_policy=ch.get("thread_reply_policy"),
                triggers=tuple(ch.get("triggers", ()))))
        auth = {k: resolve_secret(v, env) for k, v in (spec.get("auth") or {}).items()}
        instances.append(InstanceConfig(
            name=name, adapter=adapter, channels=tuple(channels),
            principals=tuple(spec.get("principals", ())), auth=auth,
            taxonomy=spec.get("taxonomy", {}) or {}))

    if not instances:
        raise ConfigError("no instances configured — the engine would poll nothing")
    cfg.instances = tuple(instances)
    return cfg


def _resolve(base: Path, p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (base / p)


def ensure_dirs(cfg: EngineConfig) -> None:
    """Create only the directories the config names, under its own base."""
    for d in {cfg.state_dir, cfg.store_path.parent, cfg.journal_path.parent,
              cfg.outbox_path.parent}:
        d.mkdir(parents=True, exist_ok=True)
