"""Staged configuration changes (ENH-29): edit ops stage, only a human click applies.

The properties that carry the feature, each with a mutation in tests/mutation_check.sh
that removes it and requires a test here to go red:

* **Staging applies nothing.** stage() records the exact old→new durably and leaves
  settings.json byte-for-byte untouched — the config twin of "compose never sends".
* **apply() is the only writer, and it is honest.** It refuses a stale stage (the file
  changed since the diff was cut), re-validates against the CURRENT environment (the
  ENH-28 re-check-at-the-click rule), writes atomically, and states the reload truth:
  there is no hot-reload, a running scheduler/watcher must be restarted.
* **Discard is terminal and kept.** A refused diff can never be applied, and the row
  stays as the record of the refusal.
* **The engine's own loader is the validator.** A candidate the loader would refuse at
  startup (the planted top-level "taxonomy", an undiscovered adapter, a missing env
  var) is refused at the gate instead — and nothing is staged.
* **A raw secret is never accepted, stored, or echoed.** Auth is env-var NAMES only;
  a pasted token is refused with a message that names its length, never its content,
  and no byte of it reaches the stage record.
* **A new channel denies by OMISSION.** add_channel() with no stated policy writes no
  reply_policy key, so the loader's own DEFAULT DENY is the single copy of that rule.
* **A widening is detected.** Any policy rising toward 'direct' — either placement
  scope, including a brand-new channel born above 'never' — lands on the staged row.
"""
import copy
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import ConfigError, from_dict  # noqa: E402
from core.reconfig import (APPLIED, DISCARDED, KEEP, RELOAD_TRUTH, STAGED,  # noqa: E402
                           ConfigStage, StageError, StaleStage, add_channel,
                           add_instance, remove_auth, remove_channel,
                           remove_instance, set_adapter, set_auth, update_channel,
                           widenings)

# Non-platform-shaped on purpose: core/config's own literal-credential refusal only
# fires on known token shapes, so THIS is the value that proves the reconfig guard —
# without it, the loader's missing-env-var refusal would echo the pasted value.
SECRET = "hunter2-super-secret-pasted-value"
# Identifier-shaped (letters+digits only): the one shape that slips the env-NAME
# check and relies on the sweep's env:-prefix refusal alone — each guard needs a
# value only IT can catch, or removing one guard leaves every test green.
SECRET_PLAIN = "supersecretpastedtoken0000"


def adopter_tree(tmp):
    """A minimal adopter: the fake adapter and a two-channel instance (one staged,
    one at the default deny)."""
    base = Path(tmp)
    shutil.copytree(ROOT / "channels" / "fake", base / "channels" / "fake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (base / "settings.json").write_text(json.dumps({
        "engine": {"state_dir": "state"},
        "instances": [{"name": "team", "adapter": "fake",
                       "channels": [{"id": "C_CUST", "reply_policy": "staged"},
                                    {"id": "C_RO"}]}]}, indent=2) + "\n")
    return base


class EditOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = adopter_tree(self.tmp.name)
        self.raw = json.loads((self.base / "settings.json").read_text())

    def load(self, candidate):
        return from_dict(copy.deepcopy(candidate), base_dir=self.base, env={})

    def test_ops_build_loadable_candidates_and_never_mutate_their_input(self):
        before = copy.deepcopy(self.raw)
        cand = add_instance(self.raw, "alerts", "fake")
        cand = add_channel(cand, "alerts", "C_NEW", label="ops")
        cand = set_adapter(cand, "alerts", "fake")
        cand = remove_channel(cand, "team", "C_RO")
        cfg = self.load(cand)
        self.assertEqual({i.name for i in cfg.instances}, {"team", "alerts"})
        self.assertEqual([c.id for c in cfg.instance("team").channels], ["C_CUST"])
        self.assertEqual(self.raw, before,
                         "an edit op mutated its input — the candidate and the "
                         "current config became one object")

    def test_duplicate_names_are_refused(self):
        with self.assertRaises(StageError):
            add_instance(self.raw, "team", "fake")
        with self.assertRaises(StageError):
            add_channel(self.raw, "team", "C_CUST")

    def test_unknown_targets_are_refused_naming_what_exists(self):
        for op, kwargs in ((remove_instance, dict(name="ghost")),
                           (set_adapter, dict(name="ghost", adapter="fake")),
                           (remove_channel, dict(instance="team", channel_id="C_X")),
                           (update_channel, dict(instance="team", channel_id="C_X"))):
            with self.assertRaises(StageError) as ctx:
                op(self.raw, **kwargs)
            self.assertIn("team" if "instance" not in kwargs else "C_CUST",
                          str(ctx.exception),
                          "the refusal must name what IS configured, or the operator "
                          "hunts a typo blind")

    def test_add_channel_without_a_stated_policy_denies_by_omission(self):
        cand = add_channel(self.raw, "team", "C_NEW")
        spec = next(s for s in cand["instances"] if s["name"] == "team")
        ch = next(c for c in spec["channels"] if c["id"] == "C_NEW")
        self.assertNotIn("reply_policy", ch,
                         "an unstated policy must be OMITTED — a written 'never' "
                         "would be a second copy of the loader's default, free to "
                         "drift from the one the engine enforces")
        cfg = self.load(cand)
        got = next(c for c in cfg.instance("team").channels if c.id == "C_NEW")
        self.assertEqual(got.policy(), "never")

    def test_update_channel_keep_and_remove_semantics(self):
        cand = update_channel(self.raw, "team", "C_CUST", label="customers")
        spec = next(s for s in cand["instances"] if s["name"] == "team")
        ch = next(c for c in spec["channels"] if c["id"] == "C_CUST")
        self.assertEqual((ch["label"], ch["reply_policy"]), ("customers", "staged"),
                         "editing the label must KEEP the untouched policy")
        cand = update_channel(cand, "team", "C_CUST", reply_policy=None)
        ch = next(c for c in next(s for s in cand["instances"]
                                  if s["name"] == "team")["channels"]
                  if c["id"] == "C_CUST")
        self.assertNotIn("reply_policy", ch,
                         "reply_policy=None must REMOVE the key — back to the "
                         "loader's deny — not write a null")

    def test_auth_ops_write_env_references_and_drop_empty_dicts(self):
        cand = set_auth(self.raw, "team", "token", "MY_TOKEN")
        spec = next(s for s in cand["instances"] if s["name"] == "team")
        self.assertEqual(spec["auth"], {"token": "env:MY_TOKEN"})
        # The reference must refuse to LOAD until the named var exists (R17)...
        with self.assertRaises(ConfigError):
            from_dict(copy.deepcopy(cand), base_dir=self.base, env={})
        from_dict(copy.deepcopy(cand), base_dir=self.base, env={"MY_TOKEN": "x"})
        cand = remove_auth(cand, "team", "token")
        spec = next(s for s in cand["instances"] if s["name"] == "team")
        self.assertNotIn("auth", spec, "the last removed entry must drop the dict")
        with self.assertRaises(StageError):
            remove_auth(cand, "team", "token")

    def test_a_pasted_secret_is_refused_unechoed_by_every_auth_op(self):
        for build in (lambda: add_instance(self.raw, "s", "fake",
                                           auth={"token": SECRET}),
                      lambda: set_auth(self.raw, "team", "token", SECRET)):
            with self.assertRaises(StageError) as ctx:
                build()
            msg = str(ctx.exception)
            self.assertNotIn(SECRET, msg, "the refusal echoed the pasted secret")
            self.assertNotIn(SECRET[:12], msg,
                             "the refusal echoed a PREFIX of the pasted secret")
            self.assertIn(f"{len(SECRET)}-char", msg,
                          "the refusal must describe the value by length, so the "
                          "operator can recognise what they pasted without it being "
                          "repeated back")


class WideningsTest(unittest.TestCase):
    def setUp(self):
        self.old = {"instances": [{"name": "team", "adapter": "fake",
                                   "channels": [{"id": "C_CUST",
                                                 "reply_policy": "staged"},
                                                {"id": "C_RO"}]}]}

    def test_promoting_a_default_deny_channel_widens_both_scopes(self):
        new = update_channel(self.old, "team", "C_RO", reply_policy="staged")
        self.assertEqual(widenings(self.old, new), [
            "instance 'team' channel 'C_RO' (channel): never -> staged",
            "instance 'team' channel 'C_RO' (thread): never -> staged"],
            "an unset thread policy follows the channel (ENH-3), so promoting the "
            "channel widens the thread placement too — both must be said")

    def test_a_new_channel_born_above_never_is_a_widening(self):
        new = add_channel(self.old, "team", "C_HOT", reply_policy="direct")
        self.assertEqual(len(widenings(self.old, new)), 2)
        self.assertIn("never -> direct", widenings(self.old, new)[0],
                      "a channel that did not exist could not be posted to: its old "
                      "policy ranks as never, not as 'no comparison'")

    def test_a_thread_only_promotion_widens_only_the_thread_scope(self):
        new = update_channel(self.old, "team", "C_RO",
                             thread_reply_policy="staged")
        self.assertEqual(widenings(self.old, new), [
            "instance 'team' channel 'C_RO' (thread): never -> staged"])

    def test_narrowings_and_removals_are_not_widenings(self):
        for new in (update_channel(self.old, "team", "C_CUST", reply_policy=None),
                    remove_channel(self.old, "team", "C_CUST"),
                    remove_instance(self.old, "team")):
            self.assertEqual(widenings(self.old, new), [],
                             "only the direction that grants the engine a voice "
                             "is loud")


class StageLadderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = adopter_tree(self.tmp.name)
        self.settings = self.base / "settings.json"
        self.db = self.base / "confstage.db"

    def stage_for(self, env=None):
        return ConfigStage(self.db, self.settings, env={} if env is None else env)

    def raw(self):
        return json.loads(self.settings.read_text())

    def rows(self):
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM confstage")]
        finally:
            conn.close()

    def test_stage_records_durably_and_touches_nothing(self):
        before = self.settings.read_text()
        s = self.stage_for()
        res = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="add C_NEW")
        s.close()
        self.assertEqual(self.settings.read_text(), before,
                         "STAGING WROTE THE FILE — the click gate does not exist")
        rows = self.rows()
        self.assertEqual([r["state"] for r in rows], [STAGED])
        self.assertIn("C_NEW", rows[0]["diff"])
        self.assertIn(str(self.settings), rows[0]["diff"],
                      "the diff must name the exact file it describes")
        self.assertFalse(res["deduped"])

    def test_the_same_change_staged_twice_is_one_card(self):
        s = self.stage_for()
        cand = add_channel(self.raw(), "team", "C_NEW")
        first = s.stage(cand, summary="x")
        again = s.stage(cand, summary="x")
        s.close()
        self.assertTrue(again["deduped"])
        self.assertEqual(again["key"], first["key"])
        self.assertEqual(len(self.rows()), 1)

    def test_a_noop_stage_is_refused(self):
        s = self.stage_for()
        with self.assertRaises(StageError):
            s.stage(self.raw(), summary="nothing")
        s.close()
        self.assertEqual(self.rows(), [], "an empty diff reached the gate")

    def test_a_loader_refusal_surfaces_at_stage_time_and_stages_nothing(self):
        """The measured mistake, verbatim: the top-level 'taxonomy' the first
        non-author adoption run planted (ENH-17). The engine's loader refuses it at
        startup; the gate must refuse it at STAGE time, with the loader's own
        message, and record nothing."""
        bad = self.raw()
        bad["taxonomy"] = {"exec_verbs": ["deploy"]}
        s = self.stage_for()
        with self.assertRaises(ConfigError) as ctx:
            s.stage(bad, summary="planted")
        s.close()
        self.assertIn("taxonomy", str(ctx.exception))
        self.assertEqual(self.rows(), [])
        # The undiscovered-adapter refusal is the loader's too (R11).
        bad2 = set_adapter(self.raw(), "team", "outlook")
        s = self.stage_for()
        with self.assertRaises(ConfigError):
            s.stage(bad2, summary="typo")
        s.close()

    def test_a_raw_secret_never_reaches_the_stage_record_or_an_error(self):
        for secret in (SECRET, SECRET_PLAIN):
            bad = self.raw()
            bad["instances"][0]["auth"] = {"token": secret}
            s = self.stage_for()
            with self.assertRaises(StageError) as ctx:
                s.stage(bad, summary="pasted")
            s.close()
            msg = str(ctx.exception)
            self.assertNotIn(secret[:12], msg, "a prefix of the secret was echoed — "
                             "core/config's own refusal does this, which is why the "
                             "sweep must run BEFORE the loader")
            blob = self.db.read_bytes() if self.db.is_file() else b""
            self.assertNotIn(secret.encode(), blob,
                             "a byte of the pasted secret reached the stage record")

    def test_apply_writes_the_exact_text_atomically_and_states_the_truth(self):
        s = self.stage_for()
        res = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="add")
        applied = s.apply(res["key"])
        s.close()
        row = self.rows()[0]
        self.assertEqual(self.settings.read_text(), row["new_text"],
                         "the file differs from the staged text the human approved")
        self.assertEqual(row["state"], APPLIED)
        from_dict(json.loads(self.settings.read_text()), base_dir=self.base, env={})
        self.assertEqual(applied["reload"], RELOAD_TRUTH)
        for word in ("restart", "hot-reload", "startup"):
            self.assertIn(word, RELOAD_TRUTH,
                          "the reload truth stopped stating the apply semantics — "
                          "'applied' alone reads as 'live everywhere', which is "
                          "false for a running scheduler/watcher")
        self.assertEqual(list(self.base.glob("*.tmp")), [],
                         "the atomic-write temp file was left behind")

    def test_reapply_reports_instead_of_writing_twice(self):
        s = self.stage_for()
        key = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="x")["key"]
        s.apply(key)
        after = self.settings.read_text()
        again = s.apply(key)
        s.close()
        self.assertTrue(again["deduped"])
        self.assertEqual(self.settings.read_text(), after)

    def test_a_stale_stage_is_refused_and_the_newer_edit_survives(self):
        s = self.stage_for()
        key = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="x")["key"]
        # Someone edits the file between the stage and the click.
        newer = update_channel(self.raw(), "team", "C_CUST", label="edited")
        self.settings.write_text(json.dumps(newer, indent=2) + "\n")
        hand_edit = self.settings.read_text()
        with self.assertRaises(StaleStage):
            s.apply(key)
        s.close()
        self.assertEqual(self.settings.read_text(), hand_edit,
                         "the stale apply overwrote the newer edit — the exact "
                         "silent destruction the stale check exists to refuse")

    def test_apply_recovers_a_crash_between_the_write_and_the_mark(self):
        s = self.stage_for()
        key = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="x")["key"]
        # Simulate the crash seam: the file write happened, the APPLIED mark did not.
        self.settings.write_text(self.rows()[0]["new_text"])
        res = s.apply(key)
        s.close()
        self.assertTrue(res.get("recovered"),
                        "the file already carries the exact staged text; that is a "
                        "truth to record, not a stale state to refuse")
        self.assertEqual(self.rows()[0]["state"], APPLIED)

    def test_discard_is_terminal_and_kept(self):
        s = self.stage_for()
        key = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="x")["key"]
        before = self.settings.read_text()
        s.discard(key)
        self.assertEqual([r["state"] for r in self.rows()], [DISCARDED],
                         "the refused card must be KEPT as the record")
        with self.assertRaises(StageError):
            s.apply(key)
        with self.assertRaises(StageError):
            s.discard(key)
        s.close()
        self.assertEqual(self.settings.read_text(), before)

    def test_an_applied_change_cannot_be_discarded(self):
        s = self.stage_for()
        key = s.stage(add_channel(self.raw(), "team", "C_NEW"), summary="x")["key"]
        s.apply(key)
        with self.assertRaises(StageError):
            s.discard(key)
        s.close()

    def test_unknown_keys_are_refused(self):
        s = self.stage_for()
        for act in (s.apply, s.discard):
            with self.assertRaises(StageError):
                act("0" * 64)
        s.close()

    def test_the_click_revalidates_against_the_current_environment(self):
        """The ENH-28 rule, on config: the environment is re-checked when the human
        acts. An env var that vanished between the stage and the click must refuse
        at the click — not at the engine's next startup."""
        env = {"TEAM_TOKEN": "present"}
        cand = set_auth(self.raw(), "team", "token", "TEAM_TOKEN")
        s = self.stage_for(env=env)
        key = s.stage(cand, summary="auth")["key"]
        before = self.settings.read_text()
        del env["TEAM_TOKEN"]
        with self.assertRaises(ConfigError) as ctx:
            s.apply(key)
        s.close()
        self.assertIn("TEAM_TOKEN", str(ctx.exception))
        self.assertEqual(self.settings.read_text(), before,
                         "a half-configured settings file was written anyway")

    def test_the_staged_row_carries_its_widenings(self):
        s = self.stage_for()
        res = s.stage(update_channel(self.raw(), "team", "C_RO",
                                     reply_policy="direct"), summary="promote")
        s.close()
        self.assertEqual(len(res["widenings"]), 2)
        self.assertEqual(json.loads(self.rows()[0]["widenings"]), res["widenings"],
                         "the widening list must be ON the durable row — the card "
                         "the human clicks is rendered from the record, not from "
                         "the stage-time return value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
