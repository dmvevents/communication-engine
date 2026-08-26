"""ENH-5 — the doctor preflight command.

Most adoption failures are configuration failures, and before this command they surfaced
as a stack trace from the middle of the first poll. One command must answer, BEFORE the
engine runs: does the config load, do the credentials resolve, is every configured
channel actually readable, and what reply policy is in force where — and it must refuse
to look healthy when it is not (the incumbent watchdog read "OK — 7 checks passed" for
weeks while one check was inert; docs/PROVENANCE.md).

The probes here are staged through on-disk adapters (the R11 discovery path), because
that is exactly what the doctor examines for a real adopter: whatever landed in their
channels_dir, not a class this test could quietly make friendlier.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.doctor import main, run  # noqa: E402
from core.store import Store  # noqa: E402

HEALTHY = "return {'reachable': True, 'auth_ok': True, 'detail': 'probe ok'}"


def write_adapter(base, name="probe", *, init=(), poll="return [], cursor",
                  health=HEALTHY, read=True):
    """Land a contract-shaped adapter as a dir-drop under the temp channels_dir."""
    d = Path(base) / "channels" / name
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(f"        {line}\n" for line in init)
    (d / "adapter.py").write_text(
        "class Adapter:\n"
        "    def __init__(self, auth=None):\n"
        "        self.auth = auth or {}\n"
        + body +
        "    def capabilities(self):\n"
        "        return {'read': " + repr(read) + ", 'history': True, 'search': False,\n"
        "                'send': False, 'react': False, 'threads': False}\n"
        "    def poll(self, cursor):\n"
        "        " + poll + "\n"
        "    def resolve(self, ref):\n"
        "        return ref\n"
        "    def health(self):\n"
        "        " + health + "\n")


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write_config(self, instances, engine=None):
        cfg = {"engine": engine or {"state_dir": "state"}, "instances": instances}
        path = self.base / "settings.json"
        path.write_text(json.dumps(cfg))
        return path

    def instance(self, channels, name="probe-inst", adapter="probe", auth=None):
        spec = {"name": name, "adapter": adapter, "channels": channels}
        if auth is not None:
            spec["auth"] = auth
        return spec

    def doctor(self, env=None):
        lines = []
        code = run(self.base / "settings.json", env=env if env is not None else {},
                   echo=lines.append)
        return code, "\n".join(lines)


class HealthyRunTest(DoctorTestCase):
    def setUp(self):
        super().setUp()
        write_adapter(self.base)
        self.write_config([self.instance([
            {"id": "C_A", "label": "team", "reply_policy": "staged"},
            {"id": "C_B"},
        ])])

    def test_a_healthy_config_exits_0_and_confirms_each_channel(self):
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS] probe-inst/C_A:readable", out)
        self.assertIn("[PASS] probe-inst/C_B:readable", out)
        self.assertIn("DOCTOR OK", out)

    def test_the_effective_reply_policy_is_printed_per_channel(self):
        _, out = self.doctor()
        self.assertIn("probe-inst/C_A [team]: channel=staged thread=staged", out)
        self.assertIn("probe-inst/C_B: channel=never thread=never", out)

    def test_unlisted_targets_are_declared_denied_by_default(self):
        _, out = self.doctor()
        self.assertIn("denied by default", out)

    def test_a_thread_scoped_policy_prints_each_placement_truthfully(self):
        """The printout is resolved by core/outbox's own policy_for, per scope — a
        doctor that prints the channel policy for both placements would report
        'answer in thread, never the main channel' as fully read-only (ENH-3)."""
        write_adapter(self.base)
        self.write_config([self.instance([
            {"id": "C_T", "reply_policy": "never", "thread_reply_policy": "staged"},
        ])])
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("probe-inst/C_T: channel=never thread=staged", out)

    def test_the_doctor_creates_no_state(self):
        """A preflight that mutates state is itself something to preflight: no state
        dir, no databases, no cursor — only the interpreter's bytecode cache."""
        before = {p for p in self.base.rglob("*") if "__pycache__" not in p.parts}
        code, _ = self.doctor()
        self.assertEqual(code, 0)
        after = {p for p in self.base.rglob("*") if "__pycache__" not in p.parts}
        self.assertEqual(before, after, f"doctor created: {after - before}")
        self.assertFalse((self.base / "state").exists(),
                         "the doctor created the engine's state directory")


class ConfigRefusalTest(DoctorTestCase):
    def test_a_config_error_exits_2_and_never_looks_healthy(self):
        """The acceptance's teeth: config failures were stack traces before; now they
        are a named refusal — and NEVER a pass of any kind."""
        write_adapter(self.base)
        self.write_config([self.instance([{"id": "C_A"}],
                                         auth={"token": "env:DOCTOR_TEST_UNSET_VAR"})])
        code, out = self.doctor(env={})
        self.assertEqual(code, 2)
        self.assertIn("DOCTOR REFUSED", out)
        self.assertIn("DOCTOR_TEST_UNSET_VAR", out,
                      "the refusal must name the credential that failed to resolve")
        self.assertNotIn("[PASS]", out)
        self.assertNotIn("DOCTOR OK", out)


class CredentialReportTest(DoctorTestCase):
    def test_resolved_reference_names_are_printed_values_never(self):
        """The doctor proves the credentials resolve by NAMING the references — a
        preflight that echoes a token value turns a diagnostic paste into a leak."""
        write_adapter(self.base)
        self.write_config([self.instance([{"id": "C_A"}],
                                         auth={"token": "env:DOCTOR_TEST_TOKEN"})])
        code, out = self.doctor(env={"DOCTOR_TEST_TOKEN": "hunter2-super-secret-value"})
        self.assertEqual(code, 0, out)
        self.assertIn("DOCTOR_TEST_TOKEN", out)
        self.assertNotIn("hunter2-super-secret-value", out,
                         "a resolved credential VALUE appeared in doctor output")

    def test_a_config_without_references_says_so_instead_of_passing_silently(self):
        write_adapter(self.base)
        self.write_config([self.instance([{"id": "C_A"}])])
        _, out = self.doctor()
        self.assertIn("no env references", out)

    def test_prose_mentions_of_env_are_not_reported_as_credentials(self):
        """Measured on this command's first live run: the shipped example's _note says
        \"values of the form 'env:NAME'...\" and a text scan reported NAME as a resolved
        credential. The report must come from the parsed auth blocks, not a grep."""
        write_adapter(self.base)
        inst = self.instance([{"id": "C_A"}])
        inst["_note"] = "credentials look like env:BOGUS_PROSE_REF in this file"
        self.write_config([inst])
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("BOGUS_PROSE_REF", out,
                         "a prose mention of env: was reported as a resolved credential")
        self.assertIn("no env references", out)


class HealthProbeTest(DoctorTestCase):
    def probe(self, health):
        write_adapter(self.base, health=health)
        self.write_config([self.instance([{"id": "C_A"}])])
        return self.doctor()

    def test_an_unreachable_adapter_fails_the_run(self):
        code, out = self.probe(
            "return {'reachable': False, 'auth_ok': True, 'detail': 'conn refused'}")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:health", out)
        self.assertIn("conn refused", out)
        self.assertNotIn("DOCTOR OK", out)

    def test_refused_credentials_fail_the_run(self):
        code, out = self.probe(
            "return {'reachable': True, 'auth_ok': False, 'detail': 'invalid_auth'}")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:health", out)
        self.assertIn("invalid_auth", out)

    def test_a_health_surface_missing_the_contract_keys_is_a_failure(self):
        """A health() that answers neither reachable nor auth_ok cannot register
        failure — the incumbent's inert-check defect wearing an adapter's name. The
        refusal must NAME the contract keys: without the shape guard this still fails,
        but as a raw KeyError — the stack-trace experience ENH-5 exists to end."""
        code, out = self.probe("return {'status': 'fine'}")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:health", out)
        self.assertIn("reachable/auth_ok", out,
                      "the failure does not name the contract keys the adapter "
                      "omitted — an errno-grade message, not a fix instruction")

    def test_an_admitted_delivery_gap_fails_the_run(self):
        """ENH-16: complete=False is a loss admission, not a detail string."""
        code, out = self.probe("return {'reachable': True, 'auth_ok': True,"
                               " 'complete': False, 'detail': 'gap [10,70)'}")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:health", out)


class ReadProbeTest(DoctorTestCase):
    def test_a_poll_failure_fails_every_configured_channel(self):
        """'Each channel is readable' is proven by a live poll; a poll that raises
        leaves every channel unconfirmed and the run unhealthy."""
        write_adapter(self.base, poll="raise RuntimeError('not_in_channel')")
        self.write_config([self.instance([{"id": "C_A"}, {"id": "C_B"}])])
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"\[FAIL\] probe-inst/C_A:readable")
        self.assertRegex(out, r"\[FAIL\] probe-inst/C_B:readable")
        self.assertIn("not_in_channel", out)

    def test_a_channel_absent_from_the_adapters_watch_set_fails(self):
        """The two-place footgun (measured, fire=11): an id present in config but
        absent from the adapter's own channel list polls NOTHING and looks successful.
        The doctor is exactly where that silent zero must become loud."""
        write_adapter(self.base, init=["self.channels = ('C_A',)"])
        self.write_config([self.instance([{"id": "C_A"}, {"id": "C_B"}])])
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("[PASS] probe-inst/C_A:readable", out)
        self.assertRegex(out, r"\[FAIL\] probe-inst/C_B:readable")
        self.assertIn("watched", out)

    def test_an_adapter_without_a_declared_watch_set_rides_on_the_poll(self):
        write_adapter(self.base)
        self.write_config([self.instance([{"id": "C_A"}])])
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS] probe-inst/C_A:readable", out)

    def test_the_probe_reuses_the_engines_cursor_and_never_moves_it(self):
        """The probe polls from the engine's own persisted cursor (bounded work on a
        live install, written through the real Store so the schema cannot drift apart)
        — and persists NOTHING, or running the doctor would eat the next real poll."""
        write_adapter(self.base, poll=(
            "import pathlib; "
            "pathlib.Path(__file__).with_name('cursor-seen.txt')"
            ".write_text(repr(cursor)); "
            "return [], 'MOVED'"))
        self.write_config([self.instance([{"id": "C_A"}])])
        state = self.base / "state"
        state.mkdir()
        store = Store(state / "messages.db")
        store.cursor_set("probe-inst", "C_A", "CUR-42")
        store.close()

        code, _ = self.doctor()
        self.assertEqual(code, 0)
        seen = (self.base / "channels" / "probe" / "cursor-seen.txt").read_text()
        self.assertEqual(seen, "'CUR-42'")
        store = Store(state / "messages.db")
        try:
            self.assertEqual(store.cursor_get("probe-inst", "C_A"), "CUR-42",
                             "the doctor persisted the probe poll's cursor")
        finally:
            store.close()


class AdapterProbeTest(DoctorTestCase):
    def test_a_failing_adapter_constructor_fails_the_run(self):
        """The slack adapter refuses to construct half-configured (missing token or
        channel list); the doctor must surface that refusal as its own finding."""
        write_adapter(self.base, init=["raise ValueError('auth channels missing')"])
        self.write_config([self.instance([{"id": "C_A"}])])
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:adapter", out)
        self.assertIn("auth channels missing", out)

    def test_an_adapter_that_cannot_read_fails_the_run(self):
        write_adapter(self.base, read=False)
        self.write_config([self.instance([{"id": "C_A"}])])
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] probe-inst:adapter", out)


class VacuousPassTest(DoctorTestCase):
    def test_an_instance_watching_no_channels_cannot_pass(self):
        """No vacuous pass: an instance with nothing to confirm has no evidence of
        health, and 'nothing to check' masquerading as success is the defect class
        core/checks.py exists to kill."""
        write_adapter(self.base)
        self.write_config([self.instance([])])
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("watches no channels", out)
        self.assertNotIn("DOCTOR OK", out)


class MainWiringTest(DoctorTestCase):
    def main(self, env=None):
        import os
        old = dict(os.environ)
        if env is not None:
            os.environ.clear()
            os.environ.update({"PATH": old.get("PATH", "")} | env)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--config", str(self.base / "settings.json")])
            return code, buf.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_exit_0_when_healthy(self):
        write_adapter(self.base)
        self.write_config([self.instance([{"id": "C_A"}])])
        self.assertEqual(self.main()[0], 0)

    def test_exit_1_when_a_check_fails(self):
        write_adapter(self.base, poll="raise RuntimeError('boom')")
        self.write_config([self.instance([{"id": "C_A"}])])
        self.assertEqual(self.main()[0], 1)

    def test_exit_2_when_the_config_is_refused(self):
        write_adapter(self.base)
        self.write_config([self.instance(
            [{"id": "C_A"}], auth={"token": "env:DOCTOR_TEST_UNSET_VAR"})])
        self.assertEqual(self.main(env={})[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
