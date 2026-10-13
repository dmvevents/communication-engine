"""core/redact: the BLOCK-tier scan a payload must pass before it is posted (ENH-31).

Controls are planted by concatenation so this test file never contains a secret-shaped
literal either; a clean reply — including one naming a loopback URL or a channel id —
must pass, or the scan would refuse the answers the dashboard exists to give.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.redact import scan  # noqa: E402


class ScanTest(unittest.TestCase):
    def test_every_credential_class_is_caught_by_name(self):
        planted = {
            "slack-token": "xo" + "xb-" + "1234567890-abcdefghij",
            "slack-app-token": "xa" + "pp-1-" + "A1234567890-abcdefghij",
            "telegram-bot-token": "123456789:" + "AA" + "a" * 31,
            "aws-access-key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "private-key": "-----BEGIN RSA PRIV" + "ATE KEY-----",
            "assigned-secret": "API_KEY=" + "Zz" * 10,
        }
        for cls, secret in planted.items():
            hits = scan(f"here you go: {secret} — do not share")
            self.assertEqual(hits, [cls], f"{cls} not caught as itself: {hits}")

    def test_the_hit_is_named_never_echoed(self):
        secret = "xo" + "xb-" + "1234567890-abcdefghij"
        for h in scan(secret):
            self.assertNotIn(secret, h)
            self.assertNotIn("xb-", h)

    def test_clean_replies_pass_including_loopback_urls_and_channel_ids(self):
        for text in ("here: http://127.0.0.1:8502", "see C_CUST and <@U_B>, thread 1700000000.000100",
                     "the token rotation finished; no secrets in this message", "", None):
            self.assertEqual(scan(text), [], text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
