"""Smoke test: the demo CLI runs every scenario offline and shows the key outcomes."""
import contextlib, io, unittest
import helpers  # noqa: F401  (sys.path)
from jarvis_workforce import demo


class DemoSmoke(unittest.TestCase):
    def test_all_scenarios(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(demo.main(["--scenario", "all"]), 0)
        out = buf.getvalue()
        for needle in ("status=completed kind=draft", "GATE send_email: DENY", "status=blocked",
                       "GATE send_email: ALLOW - approved", "approval already used (replay)", "spoofing",
                       "bad signature", "malformed output rejected", "retry #2", "simulated crash",
                       "recovered after crash, not re-sent", "mock outbox holds 1 email(s) (expected 1)",
                       "intake policy: REJECT", "status=rejected"):
            self.assertIn(needle, out)


if __name__ == "__main__":
    unittest.main()
