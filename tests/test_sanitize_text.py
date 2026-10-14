"""The sanitize gate for text that never lands in the tree: commit messages and pull-request
bodies (scripts/sanitize-text.sh, and the commit-msg hook scripts/install-hooks.sh installs).

The independent privacy review of the public repository (2026-10-14) found the four items it
reported in exactly the places the tree gate cannot see — commit messages and pull-request
bodies — so those need a check of their own, with a planted control: a message carrying a
synthetic secret shape must be refused, a clean one must pass, and an empty scan is an error,
never a pass.

Planted shapes are built by concatenation so this file never contains one (the tree gate scans
tests/ too), and every run points the gate at an EMPTY denylist so a host's local denylist cannot
make a test pass or fail.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sanitize-text.sh"
GATE = ROOT / "scripts" / "sanitize-gate.sh"
HOOKS = ROOT / "scripts" / "install-hooks.sh"

TOKEN_SHAPE = "xox" + "b-" + "0123456789" + "abcdef"          # the gate's slack-token class
KEY_SHAPE = "AKIA" + "0123456789" + "ABCDEF"                   # the gate's aws-access-key class


def _run(args, text=None, cwd=None):
    with tempfile.NamedTemporaryFile("w", suffix=".denylist", delete=False) as f:
        empty_denylist = f.name
    try:
        env = dict(os.environ, SANITIZE_DENYLIST_FILE=empty_denylist)
        return subprocess.run(["bash", str(SCRIPT), *args], input=text, text=True,
                              capture_output=True, env=env, cwd=cwd, timeout=120)
    finally:
        os.unlink(empty_denylist)


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, env=env, timeout=60)


def _repo(tmp):
    """A throwaway git repository with one clean commit; returns its path."""
    repo = pathlib.Path(tmp) / "repo"
    repo.mkdir()
    for a in (("init", "-q"), ("config", "user.name", "Test"), ("config", "user.email", "t@example.com"),
              ("config", "commit.gpgsign", "false")):
        r = _git(repo, *a); assert r.returncode == 0, r.stderr
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    r = _git(repo, "commit", "-q", "-m", "a clean first message"); assert r.returncode == 0, r.stderr
    return repo


class TextGateTest(unittest.TestCase):
    def test_a_clean_message_passes(self):
        r = _run([], text="fixed the sweep window; see the runbook\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS", r.stdout)

    def test_a_planted_token_shape_is_refused(self):
        r = _run([], text="rotated the bot: " + TOKEN_SHAPE + "\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("slack-token", r.stdout, "the finding must name its class")

    def test_a_planted_key_shape_is_refused_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / "msg.txt"
            f.write_text("key " + KEY_SHAPE + " used in the demo\n")
            r = _run([str(f)])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("aws-access-key", r.stdout)

    def test_empty_text_is_an_error_not_a_pass(self):
        r = _run([], text="")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("PASS", r.stdout)

    def test_the_commit_range_mode_names_the_offending_commit_even_when_it_is_not_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp)
            (repo / "b.txt").write_text("two\n"); _git(repo, "add", "b.txt")
            r = _git(repo, "commit", "-q", "-m", "note the token " + TOKEN_SHAPE); self.assertEqual(r.returncode, 0, r.stderr)
            bad = _git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "c.txt").write_text("three\n"); _git(repo, "add", "c.txt")
            r = _git(repo, "commit", "-q", "-m", "a clean newest message"); self.assertEqual(r.returncode, 0, r.stderr)
            r = _run(["--commits", "HEAD~2..HEAD"], cwd=repo)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("commit-" + bad[:12], r.stdout, "the finding must name the commit it is in")
            clean = _run(["--commits", "HEAD~1..HEAD"], cwd=repo)
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            empty = _run(["--commits", "HEAD..HEAD"], cwd=repo)
            self.assertEqual(empty.returncode, 2, "an empty range is nothing to scan, not a pass")


class CommitMsgHookTest(unittest.TestCase):
    """scripts/install-hooks.sh installs a commit-msg hook; the hook refuses a message with a
    planted shape and git then creates no commit."""

    def _repo_with_hooks(self, tmp):
        repo = _repo(tmp)
        scripts = repo / "scripts"; scripts.mkdir()
        for f in (GATE, SCRIPT, HOOKS):
            shutil.copy(f, scripts / f.name)
        r = subprocess.run(["bash", str(scripts / "install-hooks.sh")], cwd=repo, text=True,
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return repo

    def test_the_hook_is_installed_and_refuses_a_planted_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_hooks(tmp)
            hook = repo / ".git" / "hooks" / "commit-msg"
            self.assertTrue(hook.is_file(), "install-hooks.sh did not install a commit-msg hook")
            self.assertTrue(os.access(hook, os.X_OK))
            env = dict(os.environ, SANITIZE_DENYLIST_FILE=str(pathlib.Path(tmp) / "empty.denylist"))
            pathlib.Path(env["SANITIZE_DENYLIST_FILE"]).write_text("")
            bad = pathlib.Path(tmp) / "bad.txt"; bad.write_text("ship it " + TOKEN_SHAPE + "\n")
            good = pathlib.Path(tmp) / "good.txt"; good.write_text("ship the sweep fix\n")
            refused = subprocess.run(["bash", str(hook), str(bad)], cwd=repo, text=True, capture_output=True, env=env)
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            passed = subprocess.run(["bash", str(hook), str(good)], cwd=repo, text=True, capture_output=True, env=env)
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

    def test_git_commit_itself_is_refused_and_leaves_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_hooks(tmp)
            env = dict(os.environ, SANITIZE_DENYLIST_FILE=str(pathlib.Path(tmp) / "empty.denylist"))
            pathlib.Path(env["SANITIZE_DENYLIST_FILE"]).write_text("")
            before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
            (repo / "d.txt").write_text("four\n"); _git(repo, "add", "d.txt")
            r = _git(repo, "commit", "-q", "-m", "paste: " + TOKEN_SHAPE, env=env)
            self.assertNotEqual(r.returncode, 0, "git accepted a commit message with a planted token shape")
            self.assertEqual(_git(repo, "rev-list", "--count", "HEAD").stdout.strip(), before)
            ok = _git(repo, "commit", "-q", "-m", "a clean message", env=env)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)


if __name__ == "__main__":
    unittest.main()
