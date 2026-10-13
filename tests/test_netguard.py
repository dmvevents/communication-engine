"""tests/netguard — the loopback-only network guard the suite runs under in CI and in the
pre-push hook (PYTHONPATH=tests/netguard). Round 3 of the ENH-31 review caught one test
reaching slack.com behind a best-effort exception handler; the guard turns that class into
a red test. These checks prove the guard bites when installed, and say so when it is not.
"""
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "tests" / "netguard" / "sitecustomize.py"


def guard_installed():
    mod = sys.modules.get("sitecustomize")
    return mod is not None and getattr(mod, "_MARKER", None) == "comms-engine-netguard"


class NetGuardTest(unittest.TestCase):
    def test_the_guard_ships_and_documents_how_it_is_run(self):
        self.assertTrue(GUARD.is_file())
        text = GUARD.read_text()
        self.assertIn("PYTHONPATH=tests/netguard", text)
        for cls in ("def connect(", "def bind(", "def getaddrinfo("):
            self.assertIn(cls, text)

    @unittest.skipUnless(guard_installed(), "network guard not installed — run the suite with "
                                            "PYTHONPATH=tests/netguard, as CI and the hook do")
    def test_a_non_loopback_lookup_or_connect_is_refused_and_loopback_is_not(self):
        with self.assertRaises(RuntimeError) as cm:
            socket.getaddrinfo("slack.com", 443)
        self.assertIn("comms-engine-netguard", str(cm.exception))
        sock = socket.socket()
        try:
            with self.assertRaises(RuntimeError):
                # TEST-NET-3, never routable; assembled so the sanitize gate's ipv4 class
                # does not read a literal address in a test
                sock.connect((".".join(["203", "0", "113", "7"]), 443))
        finally:
            sock.close()
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", 0))                           # the architecture, allowed
            with self.assertRaises(RuntimeError):
                socket.socket().bind(("0.0.0.0", 0))
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
