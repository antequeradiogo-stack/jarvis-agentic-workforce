"""Approval store: single-use, expiry, tamper, binding, key handling, concurrency."""
import json, os, tempfile, threading, unittest
from pathlib import Path
from helpers import TEST_KEY
from jarvis_workforce.approval_store import ApprovalError, ApprovalStore, load_key

P = {"to": "reviewer@example.com", "body": "hello"}


class Store(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.root = Path(self._t.name)
        self.s = ApprovalStore(self.root, TEST_KEY)

    def tearDown(self): self._t.cleanup()

    def pending(self): return list((self.root / "pending").glob("*.json"))

    def test_valid_approval_consumed_once(self):
        self.s.issue("send_email", P, ttl=60, now=1000)
        self.assertTrue(self.s.consume("send_email", P, now=1001)[0])
        ok, why = self.s.consume("send_email", P, now=1002)
        self.assertFalse(ok); self.assertIn("no approval", why)
        self.assertEqual(self.pending(), [])

    def test_replay_of_consumed_file_refused(self):
        self.s.issue("send_email", P, ttl=60, now=1000)
        self.s.consume("send_email", P, now=1001)
        c = next((self.root / "consumed").glob("*.json"))
        (self.root / "pending" / c.name).write_text(c.read_text())
        ok, why = self.s.consume("send_email", P, now=1002)
        self.assertFalse(ok); self.assertIn("replay", why)

    def test_expired(self):
        self.s.issue("send_email", P, ttl=60, now=1000)
        ok, why = self.s.consume("send_email", P, now=1060)
        self.assertFalse(ok); self.assertIn("expired", why)

    def test_issued_in_future_and_ttl_limits(self):
        self.s.issue("send_email", P, ttl=60, now=5000)
        self.assertIn("future", self.s.consume("send_email", P, now=1000)[1])
        with self.assertRaises(ApprovalError): self.s.issue("send_email", P, ttl=0)
        with self.assertRaises(ApprovalError): self.s.issue("send_email", P, ttl=99999)

    def test_tamper_any_signed_field_breaks_signature(self):
        for field, val in (("expires_at", 10**10), ("approver", "attacker"), ("nonce", "0" * 32), ("issued_at", 999),
                           ("action", "publish"), ("v", 2)):
            with self.subTest(field=field):
                for p in self.pending(): p.unlink()
                self.s.issue("send_email", P, ttl=60, now=1000)
                p = self.pending()[0]; b = json.loads(p.read_text()); b[field] = val; p.write_text(json.dumps(b))
                ok, why = self.s.consume("send_email", P, now=1001)
                self.assertFalse(ok)
                self.assertRegex(why, "signature|version|no approval")

    def test_rebinding_hash_to_other_request_breaks_signature(self):
        from jarvis_workforce.gate import request_sha256
        self.s.issue("send_email", P, ttl=60, now=1000)
        other = {**P, "to": "attacker@example.com"}
        p = self.pending()[0]; b = json.loads(p.read_text()); b["request_sha256"] = request_sha256("send_email", other)
        p.write_text(json.dumps(b))
        ok, why = self.s.consume("send_email", other, now=1001)
        self.assertFalse(ok); self.assertIn("signature", why)

    def test_extra_or_missing_fields_and_garbage(self):
        self.s.issue("send_email", P, ttl=60, now=1000)
        p = self.pending()[0]; b = json.loads(p.read_text()); b["approved"] = True; p.write_text(json.dumps(b))
        self.assertIn("malformed", self.s.consume("send_email", P, now=1001)[1])
        (self.root / "pending" / "junk.json").write_text("{not json")
        self.assertFalse(self.s.consume("send_email", P, now=1001)[0])

    def test_wrong_key(self):
        ApprovalStore(self.root, b"another-test-key-another-test-key-000").issue("send_email", P, ttl=60, now=1000)
        self.assertIn("signature", self.s.consume("send_email", P, now=1001)[1])

    def test_parameter_change_invalidates(self):
        self.s.issue("send_email", P, ttl=60, now=1000)
        self.assertFalse(self.s.consume("send_email", {**P, "body": "hello!"}, now=1001)[0])
        self.assertFalse(self.s.consume("publish", P, now=1001)[0])

    def test_concurrent_consumers_only_one_wins(self):
        self.s.issue("send_email", P, ttl=600)
        results, stores = [], [ApprovalStore(self.root, TEST_KEY) for _ in range(8)]
        ts = [threading.Thread(target=lambda st=st: results.append(st.consume("send_email", P)[0])) for st in stores]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count(True), 1)

    def test_short_key_rejected(self):
        with self.assertRaises(ApprovalError): ApprovalStore(self.root, b"short")


class KeyLoading(unittest.TestCase):
    def test_env_raw_and_hex(self):
        self.assertEqual(load_key(None, {"JARVIS_APPROVAL_KEY": "k" * 32}), b"k" * 32)
        self.assertEqual(load_key(None, {"JARVIS_APPROVAL_KEY": "hex:" + "ab" * 32}), bytes.fromhex("ab" * 32))
        with self.assertRaises(ApprovalError): load_key(None, {"JARVIS_APPROVAL_KEY": "short"})

    def test_missing_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(KeyError): load_key(d, {})

    def test_dotenv_requires_0600(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"; p.write_text('JARVIS_APPROVAL_KEY="' + "z" * 40 + '"\n')
            os.chmod(p, 0o644)
            with self.assertRaises(ApprovalError): load_key(d, {})
            os.chmod(p, 0o600)
            self.assertEqual(load_key(d, {}), b"z" * 40)

    def test_env_wins_over_dotenv(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"; p.write_text("JARVIS_APPROVAL_KEY=" + "z" * 40); os.chmod(p, 0o600)
            self.assertEqual(load_key(d, {"JARVIS_APPROVAL_KEY": "e" * 32}), b"e" * 32)


if __name__ == "__main__":
    unittest.main()
