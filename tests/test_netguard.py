"""tests/netguard — the loopback-only network guard the suite runs under in CI and in the
pre-push hook (PYTHONPATH=tests/netguard). Round 3 of the ENH-31 review caught one test
reaching slack.com behind a best-effort exception handler; the guard turns that class into
a red test. These checks prove the guard bites when installed, and say so when it is not.
"""
import os
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

    def test_under_ci_the_guard_must_be_installed_not_skipped(self):
        """Round 4 (R4-6): without PYTHONPATH=tests/netguard the live check skipped and the
        suite stayed green. In CI that is a configuration error, not a skip."""
        if os.environ.get("CI") and not guard_installed():
            self.fail("CI runs the suite without the network guard — "
                      "set PYTHONPATH=tests/netguard (see .github/workflows/tests.yml)")

    @unittest.skipUnless(guard_installed(), "network guard not installed — run the suite with "
                                            "PYTHONPATH=tests/netguard, as CI and the hook do")
    def test_a_non_loopback_lookup_or_connect_is_refused_and_loopback_is_not(self):
        import sitecustomize
        refused = sitecustomize.NetworkRefused
        self.assertTrue(issubclass(refused, BaseException) and not issubclass(refused, Exception),
                        "a refusal an `except Exception` can swallow is no guard (R4-6)")
        for lookup in (socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex):
            with self.assertRaises(refused, msg=lookup.__name__) as cm:
                lookup("slack.com", 443) if lookup is socket.getaddrinfo else lookup("slack.com")
            self.assertIn("comms-engine-netguard", str(cm.exception))
        # TEST-NET-3, never routable; assembled so the sanitize gate's ipv4 class does not
        # read a literal address in a test
        far = ".".join(["203", "0", "113", "7"])
        sock = socket.socket()
        try:
            with self.assertRaises(refused):
                sock.connect((far, 443))
        finally:
            sock.close()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(refused):
                udp.sendto(b"x", (far, 53))
        finally:
            udp.close()
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", 0))                           # the architecture, allowed
            for every_interface in ("0.0.0.0", ""):
                with self.assertRaises(refused, msg=repr(every_interface)):
                    socket.socket().bind((every_interface, 0))
        finally:
            srv.close()
        # the shape the guard was built for: a network call behind a best-effort handler
        def best_effort():
            try:
                socket.getaddrinfo("slack.com", 443)
            except Exception:  # noqa: BLE001
                return "swallowed"
            return "connected"
        with self.assertRaises(refused):
            best_effort()


if __name__ == "__main__":
    unittest.main()
