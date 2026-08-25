"""Portability + adoptability tests (gate G8; requirements R17, R18, R19).

The incumbent cannot be adopted because it is welded to one host: tmux pane names,
`/home/<user>/slack`, specific systemd units, one workspace's channel IDs. These tests make
that failure mode impossible to reintroduce:

* no absolute home path may appear anywhere in the shipped package
* no module may depend on tmux or systemd
* the ENTIRE pipeline must be constructible from a config in a temporary directory, and must
  write nothing outside it
* a fresh adopter's default must be DENY, so they cannot accidentally post as anyone
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.classify import Taxonomy, classify  # noqa: E402
from core.config import ConfigError, from_dict, ensure_dirs, load, resolve_secret  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.outbox import Outbox, PolicyError  # noqa: E402
from core.store import Store  # noqa: E402

SHIPPED = ("core", "channels", "scripts", "watchers", "docs")


def seed_fake_adapter(base):
    """Adapter types are DISCOVERED on disk (R11), so a hermetic temp base that names
    'fake' in its config must contain it — exactly what a real adopter's tree looks like."""
    d = Path(base) / "channels" / "fake"
    d.mkdir(parents=True, exist_ok=True)
    (d / "adapter.py").write_text(
        "class Adapter:\n"
        "    def __init__(self, auth=None):\n"
        "        self.auth = auth or {}\n")
# An absolute path into somebody's home directory is the signature of a non-portable tool.
HOME_PATH = re.compile(r"(/home/[a-z0-9_-]+/|/Users/[A-Za-z0-9_-]+/|C:\\Users\\)")
HOST_TOOLS = re.compile(r"\b(tmux|systemctl|journalctl)\b")


def shipped_files(exts=(".py", ".sh", ".md", ".json", ".yml")):
    for d in SHIPPED:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in exts:
                yield p


class NoHostDependencyTest(unittest.TestCase):
    def test_no_absolute_home_paths_in_the_shipped_package(self):
        """R18. A hardcoded /home/<user>/... path is why the incumbent cannot be adopted."""
        offenders = []
        for p in shipped_files():
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if HOME_PATH.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
        self.assertEqual(offenders, [],
                         "absolute home paths found — the engine would only run on one "
                         f"machine:\n" + "\n".join(offenders[:12]))

    def test_core_does_not_depend_on_tmux_or_systemd(self):
        """The incumbent's watchers scrape tmux panes and query systemd units.

        Scans EXECUTABLE string literals via the AST, not raw lines: docstrings in this
        package legitimately discuss tmux/systemd when explaining why the incumbent is not
        portable. A line-based grep flagged that prose, which measured text rather than
        dependency — the code was right and the test was wrong.
        """
        import ast
        offenders = []
        for p in (ROOT / "core").rglob("*.py"):
            tree = ast.parse(p.read_text(errors="replace"))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    body = getattr(node, "body", [])
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings
                        and HOST_TOOLS.search(node.value)):
                    offenders.append(
                        f"{p.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}: "
                        f"{node.value[:70]!r}")
                if isinstance(node, ast.Name) and HOST_TOOLS.search(node.id):
                    offenders.append(f"{p.relative_to(ROOT)}: name {node.id}")
        self.assertEqual(offenders, [],
                         "core/ has a host-specific tool dependency:\n" + "\n".join(offenders))

    def test_core_imports_cleanly_without_any_env_vars(self):
        """Importing the engine must not require this host's environment."""
        r = subprocess.run(
            [sys.executable, "-c",
             "import core.store, core.journal, core.outbox, core.classify, core.checks, "
             "core.owed, core.config, core.parity, core.escalate, core.ratelimit; "
             "print('ok')"],
            cwd=ROOT, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(r.returncode, 0, f"import failed: {r.stderr[-400:]}")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        seed_fake_adapter(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg_dict(self, **over):
        d = {
            "engine": {"state_dir": "state"},
            "instances": [{
                "name": "adopter-workspace",
                "adapter": "fake",
                "auth": {"token": "env:ADOPTER_TOKEN"},
                "channels": [
                    {"id": "C_THEIR_CHANNEL", "label": "team", "reply_policy": "staged"},
                    {"id": "C_UNSET_POLICY", "label": "no policy given"},
                ],
                "principals": ["U_THEIR_LEAD"],
            }],
        }
        d.update(over)
        return d

    def test_relative_paths_resolve_against_the_config_directory(self):
        """R17: the same config file works on any machine."""
        c = from_dict(self.cfg_dict(), base_dir=self.base, env={"ADOPTER_TOKEN": "x"})
        self.assertTrue(str(c.store_path).startswith(str(self.base)))
        self.assertTrue(str(c.journal_path).startswith(str(self.base)))

    def test_a_channel_without_a_policy_defaults_to_deny(self):
        """R19: a fresh adopter cannot accidentally post as anyone."""
        c = from_dict(self.cfg_dict(), base_dir=self.base, env={"ADOPTER_TOKEN": "x"})
        pol = c.instance("adopter-workspace").policies()
        self.assertEqual(pol["C_UNSET_POLICY"], "never")

    def test_invalid_policy_is_refused(self):
        d = self.cfg_dict()
        d["instances"][0]["channels"][0]["reply_policy"] = "yolo"
        with self.assertRaises(ConfigError):
            from_dict(d, base_dir=self.base, env={"ADOPTER_TOKEN": "x"})

    def test_unknown_adapter_is_refused_not_silently_ignored(self):
        d = self.cfg_dict()
        d["instances"][0]["adapter"] = "slakc"
        with self.assertRaises(ConfigError) as ctx:
            from_dict(d, base_dir=self.base, env={"ADOPTER_TOKEN": "x"})
        self.assertIn("slakc", str(ctx.exception))

    def test_missing_env_var_fails_at_load_not_at_first_send(self):
        with self.assertRaises(ConfigError) as ctx:
            from_dict(self.cfg_dict(), base_dir=self.base, env={})
        self.assertIn("ADOPTER_TOKEN", str(ctx.exception))

    def test_a_literal_token_in_config_is_refused(self):
        d = self.cfg_dict()
        d["instances"][0]["auth"]["token"] = "xox" + "b-9999-literal-token"
        with self.assertRaises(ConfigError) as ctx:
            from_dict(d, base_dir=self.base, env={})
        self.assertIn("literal credential", str(ctx.exception))

    def test_config_with_no_instances_is_refused(self):
        with self.assertRaises(ConfigError):
            from_dict({"engine": {}, "instances": []}, base_dir=self.base)

    def test_missing_config_file_raises_clearly(self):
        with self.assertRaises(ConfigError):
            load(self.base / "nope.json")

    def test_malformed_json_raises_clearly(self):
        p = self.base / "bad.json"
        p.write_text("{not json")
        with self.assertRaises(ConfigError):
            load(p)

    def test_resolve_secret_requires_the_env_prefix(self):
        with self.assertRaises(ConfigError):
            resolve_secret("just-a-value", env={})

    def test_shipped_example_config_is_loadable(self):
        """The example an adopter copies must actually parse."""
        example = ROOT / "settings.example.json"
        self.assertTrue(example.is_file(), "settings.example.json is missing")
        import json
        raw = json.loads(example.read_text())
        self.assertIn("instances", raw, "example config has no instances to copy")


class EndToEndFromConfigTest(unittest.TestCase):
    """R17: the whole pipeline, built from a temp-dir config, on a fake adapter."""

    class FakeAdapter:
        def __init__(self):
            self.delivered = []

        def send(self, target, text, key=None):
            self.delivered.append((target, text, key))
            return {"ts": "r1", "key": key}

        def read_back(self, target, key):
            return any(t == target and k == key for t, _, k in self.delivered)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        seed_fake_adapter(self.base)
        self.cfg = from_dict({
            "engine": {"state_dir": "state"},
            "instances": [{
                "name": "adopter",
                "adapter": "fake",
                "auth": {"token": "env:ADOPTER_TOKEN"},
                "channels": [
                    {"id": "C_TEAM", "reply_policy": "direct"},
                    {"id": "C_CUSTOMER", "reply_policy": "staged"},
                    {"id": "C_READONLY"},
                ],
                "taxonomy": {"exec_verbs": ["provision", "deploy"]},
            }],
        }, base_dir=self.base, env={"ADOPTER_TOKEN": "token-from-env"})
        ensure_dirs(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_cycle_ingest_classify_journal_route(self):
        inst = self.cfg.instance("adopter")
        store = Store(self.cfg.store_path)
        journal = Journal(self.cfg.journal_path)
        adapter = self.FakeAdapter()
        outbox = Outbox(self.cfg.outbox_path, adapter, inst.policies())
        tax = Taxonomy.from_config(inst.taxonomy)

        msg = {"channel_type": "fake", "channel_id": "C_TEAM",
               "sender_id": "U_THEIR_LEAD", "ts": "1.1",
               "text": "Please provision the staging environment."}
        store.upsert_messages([msg])
        c = classify(msg["text"], tax)
        first = journal.record("C_TEAM", "1.1", sender_id=msg["sender_id"],
                               text=msg["text"], kind=c.kind, reason=c.reason)

        self.assertEqual(store.count("C_TEAM"), 1)
        self.assertTrue(first)
        self.assertEqual(c.kind, "EXEC-REQUEST",
                         "the adopter's OWN configured verb list was not honoured")

        r = outbox.send("C_TEAM", "1.1", "[AGENT] provisioning now.")
        journal.mark_responded("C_TEAM", "1.1", r["key"])
        self.assertEqual(r["state"], "COMMITTED")
        self.assertEqual(journal.unanswered("C_TEAM"), [])

        store.close(); journal.close(); outbox.close()

    def test_customer_channel_stages_and_readonly_channel_refuses(self):
        inst = self.cfg.instance("adopter")
        adapter = self.FakeAdapter()
        outbox = Outbox(self.cfg.outbox_path, adapter, inst.policies())

        staged = outbox.send("C_CUSTOMER", "2.2", "draft for a human to gate")
        self.assertEqual(staged["state"], "STAGED")
        self.assertEqual(adapter.delivered, [],
                         "a staged customer draft reached the adapter")

        with self.assertRaises(PolicyError):
            outbox.send("C_READONLY", "3.3", "should never send")
        outbox.close()

    def test_the_engine_writes_nothing_outside_its_configured_base(self):
        """A tool that scatters state outside its config is not adoptable."""
        before = {p for p in Path.cwd().iterdir()}
        inst = self.cfg.instance("adopter")
        Store(self.cfg.store_path).close()
        Journal(self.cfg.journal_path).close()
        Outbox(self.cfg.outbox_path, self.FakeAdapter(), inst.policies()).close()
        after = {p for p in Path.cwd().iterdir()}
        self.assertEqual(before, after,
                         f"the engine created files in the working directory: {after - before}")
        for p in (self.cfg.store_path, self.cfg.journal_path, self.cfg.outbox_path):
            self.assertTrue(p.is_file(), f"{p} was not created under the configured base")
            self.assertTrue(str(p).startswith(str(self.base)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
