"""Approval store: single-use, expiry, tamper, binding, key handling, concurrency."""
import json, os, tempfile, threading, unittest
from pathlib import Path
from helpers import TEST_KEY
from jarvis_workforce.approval_store import ApprovalError, ApprovalStore, load_key

P = {"to": "reviewer@example.com", "body": "hello"}


class Store(unittest.TestCase):
    ID = {"run_id": "run-1", "task_id": "task-1"}

    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.root = Path(self._t.name)
        self.s = ApprovalStore(self.root, TEST_KEY)

    def tearDown(self): self._t.cleanup()

    def pending(self): return list((self.root / "pending").glob("*.json"))

    def issue(self, *a, **k):
        return self.s.issue(*a, **{**self.ID, **k})

    def consume(self, *a, **k):
        return self.s.consume(*a, **{**self.ID, **k})

    def test_valid_approval_consumed_once(self):
        self.issue("send_email", P, ttl=60, now=1000)
        self.assertTrue(self.consume("send_email", P, now=1001)[0])
        ok, why = self.consume("send_email", P, now=1002)
        self.assertFalse(ok); self.assertIn("no approval", why)
        self.assertEqual(self.pending(), [])

    def test_replay_of_consumed_file_refused(self):
        self.issue("send_email", P, ttl=60, now=1000)
        self.consume("send_email", P, now=1001)
        c = next((self.root / "consumed").glob("*.json"))
        (self.root / "pending" / c.name).write_text(c.read_text())
        ok, why = self.consume("send_email", P, now=1002)
        self.assertFalse(ok); self.assertIn("replay", why)

    def test_expired(self):
        self.issue("send_email", P, ttl=60, now=1000)
        ok, why = self.consume("send_email", P, now=1060)
        self.assertFalse(ok); self.assertIn("expired", why)

    def test_issued_in_future_and_ttl_limits(self):
        self.issue("send_email", P, ttl=60, now=5000)
        self.assertIn("future", self.consume("send_email", P, now=1000)[1])
        with self.assertRaises(ApprovalError): self.issue("send_email", P, ttl=0)
        with self.assertRaises(ApprovalError): self.issue("send_email", P, ttl=99999)

    def test_tamper_any_signed_field_breaks_signature(self):
        for field, val in (("expires_at", 10**10), ("approver", "attacker"), ("nonce", "0" * 32), ("issued_at", 999),
                           ("action", "publish"), ("v", 3), ("run_id", "other-run"), ("task_id", "other-task")):
            with self.subTest(field=field):
                for p in self.pending(): p.unlink()
                self.issue("send_email", P, ttl=60, now=1000)
                p = self.pending()[0]; b = json.loads(p.read_text()); b[field] = val; p.write_text(json.dumps(b))
                ok, why = self.consume("send_email", P, now=1001)
                self.assertFalse(ok)
                self.assertRegex(why, "signature|version|no approval")

    def test_rebinding_hash_to_other_request_breaks_signature(self):
        from jarvis_workforce.gate import request_sha256
        self.issue("send_email", P, ttl=60, now=1000)
        other = {**P, "to": "attacker@example.com"}
        p = self.pending()[0]; b = json.loads(p.read_text())
        b["request_sha256"] = request_sha256("send_email", other, **self.ID)
        p.write_text(json.dumps(b))
        ok, why = self.consume("send_email", other, now=1001)
        self.assertFalse(ok); self.assertIn("signature", why)

    def test_extra_or_missing_fields_and_garbage(self):
        self.issue("send_email", P, ttl=60, now=1000)
        p = self.pending()[0]; b = json.loads(p.read_text()); b["approved"] = True; p.write_text(json.dumps(b))
        self.assertIn("malformed", self.consume("send_email", P, now=1001)[1])
        (self.root / "pending" / "junk.json").write_text("{not json")
        self.assertFalse(self.consume("send_email", P, now=1001)[0])

    def test_wrong_key(self):
        ApprovalStore(self.root, b"another-test-key-another-test-key-000").issue(
            "send_email", P, ttl=60, now=1000, **self.ID)
        self.assertIn("signature", self.consume("send_email", P, now=1001)[1])

    def test_parameter_change_invalidates(self):
        self.issue("send_email", P, ttl=60, now=1000)
        self.assertFalse(self.consume("send_email", {**P, "body": "hello!"}, now=1001)[0])
        self.assertFalse(self.consume("publish", P, now=1001)[0])

    def test_concurrent_consumers_only_one_wins(self):
        self.issue("send_email", P, ttl=600)
        results, stores = [], [ApprovalStore(self.root, TEST_KEY) for _ in range(8)]
        ts = [threading.Thread(target=lambda st=st: results.append(st.consume("send_email", P, **self.ID)[0])) for st in stores]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count(True), 1)

    def test_short_key_rejected(self):
        with self.assertRaises(ApprovalError): ApprovalStore(self.root, b"short")

    def test_approval_bound_to_run_and_task_and_single_use(self):
        from jarvis_workforce.gate import request_sha256
        hashes = {request_sha256("send_email", P, run_id=r, task_id=t)
                  for r, t in (("run-A", "task-1"), ("run-B", "task-1"), ("run-A", "task-2"))}
        self.assertEqual(len(hashes), 3)
        self.issue("send_email", P, ttl=60, now=1000, run_id="run-A", task_id="task-1")
        for run_id, task_id in (("run-B", "task-1"), ("run-A", "task-2")):
            ok, why = self.consume("send_email", P, now=1001, run_id=run_id, task_id=task_id)
            self.assertFalse(ok)
            self.assertIn("no approval", why)
            self.assertEqual(len(self.pending()), 1)
        self.assertTrue(self.consume("send_email", P, now=1001, run_id="run-A", task_id="task-1")[0])
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.consume("send_email", P, now=1002, run_id="run-A", task_id="task-1")[0])
        with self.assertRaises(ApprovalError):
            self.s.issue("send_email", P, ttl=60)
        self.issue("send_email", P, ttl=60, now=2000)
        ok, why = self.s.consume("send_email", P, now=2001)
        self.assertFalse(ok)
        self.assertIn("run_id", why)
        self.assertEqual(len(self.pending()), 1)


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

    def test_invalid_hex_and_short_key_are_approval_errors(self):
        with self.assertRaises(ApprovalError) as cm:
            load_key(None, {"JARVIS_APPROVAL_KEY": "hex:zzzz"})
        self.assertIn("hexadecimal", str(cm.exception))
        self.assertNotIn("zzzz", str(cm.exception))
        with self.assertRaises(ApprovalError) as cm:
            load_key(None, {"JARVIS_APPROVAL_KEY": "hex:" + "aa"})
        self.assertIn("32", str(cm.exception))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("JARVIS_APPROVAL_KEY=hex:zzzz\n")
            os.chmod(p, 0o600)
            with self.assertRaises(ApprovalError) as cm:
                load_key(d, {})
            self.assertIn("hexadecimal", str(cm.exception))
            p.write_text("JARVIS_APPROVAL_KEY=tiny-key\n")
            os.chmod(p, 0o600)
            with self.assertRaises(ApprovalError) as cm:
                load_key(d, {})
            self.assertIn("32", str(cm.exception))
            self.assertNotIn("tiny-key", str(cm.exception))

    def test_env_wins_over_dotenv(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"; p.write_text("JARVIS_APPROVAL_KEY=" + "z" * 40); os.chmod(p, 0o600)
            self.assertEqual(load_key(d, {"JARVIS_APPROVAL_KEY": "e" * 32}), b"e" * 32)


if __name__ == "__main__":
    unittest.main()
