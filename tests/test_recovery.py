"""Checkpoint/resume: crashed or paused runs resume without repeating steps or side effects."""
import contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from helpers import WorkflowCase, email_task, research_task
from jarvis_workforce.orchestrator import SimulatedCrash
from jarvis_workforce.runlog import RunLog


class Resume(WorkflowCase):
    def approve_pending(self, run_id):
        self.issue_for(self.last_approval_request(run_id))

    def test_crash_after_research_resumes_at_write(self):
        o = self.make(crash_at={"after_step:research"})
        with self.assertRaises(SimulatedCrash): o.run(research_task())
        calls_before = len(self.toolbox.calls)
        r = o.run(research_task())
        self.assertEqual(r.status, "completed")
        self.assertEqual(len(self.toolbox.calls), calls_before)  # researcher's tools were not called again
        ev = [e["event"] for e in self.events("t-research-1")]
        self.assertIn("run_resumed", ev)
        self.assertEqual([e["step"] for e in self.events("t-research-1") if e["event"] == "step_completed"], ["research", "write"])

    def test_crash_after_effect_before_commit_does_not_resend_or_need_new_approval(self):
        o = self.make(crash_at={"after_effect:send_email"})
        o.run(email_task()); self.approve_pending("t-email-1")
        with self.assertRaises(SimulatedCrash): o.run(email_task())
        self.assertEqual(len(self.toolbox.effects()), 1)
        r = o.run(email_task())  # no new approval issued
        self.assertEqual((r.status, r.kind), ("completed", "simulated"))
        self.assertTrue(r.effects[0]["recovered"])
        self.assertEqual(len(self.toolbox.effects()), 1)

    def test_finished_run_is_idempotent(self):
        o = self.make(); o.run(email_task()); self.approve_pending("t-email-1")
        r1 = o.run(email_task()); r2 = o.run(email_task())
        self.assertEqual(r1, r2)
        self.assertEqual(len(self.toolbox.effects()), 1)
        self.assertEqual(self.events("t-email-1")[-1]["event"], "run_already_finished")

    def test_effect_already_committed_in_log_is_skipped_on_reentry(self):
        o = self.make(); o.run(email_task()); self.approve_pending("t-email-1")
        o.run(email_task())
        # simulate a blocked->resumed path re-entering delivery: drop run_finished so the run looks unfinished
        p = self.work / "runs" / "t-email-1" / "events.jsonl"
        lines = [l for l in p.read_text().splitlines() if '"run_finished"' not in l]
        p.write_text("\n".join(lines) + "\n")
        r = o.run(email_task())
        self.assertEqual(r.status, "completed")
        self.assertIn("effect_skipped", [e["event"] for e in self.events("t-email-1")])
        self.assertEqual(len(self.toolbox.effects()), 1)

    def test_idempotency_key_deterministic_and_run_scoped(self):
        o = self.make()
        for tid in ("t-email-1", "t-email-2"):
            o.run(email_task(tid)); self.approve_pending(tid); o.run(email_task(tid))
        keys = [e["idempotency_key"] for e in self.toolbox.effects()]
        self.assertEqual(len(set(keys)), 2)

    def test_resume_with_different_task_refused(self):
        o = self.make(); o.run(email_task())
        other = email_task(); other["goal"] = "Research something else entirely for the report."
        with self.assertRaises(ValueError): o.run(other)

    def test_torn_last_log_line_ignored(self):
        o = self.make(crash_at={"after_step:research"})
        with self.assertRaises(SimulatedCrash): o.run(research_task())
        with open(self.work / "runs" / "t-research-1" / "events.jsonl", "a") as f:
            f.write('{"seq": 99, "event": "step_comp')  # crash mid-write
        self.assertEqual(o.run(research_task()).status, "completed")

    def test_torn_outbox_resume_does_not_duplicate(self):
        o = self.make()
        o.run(email_task()); self.approve_pending("t-email-1"); o.run(email_task())
        first = self.toolbox.effects()
        self.assertEqual(len(first), 1)
        with open(self.toolbox.effects_file, "ab") as f:
            f.write(b'{"effect_id":"torn"')
        self.assertEqual(self.toolbox.effects(), first)
        p = self.work / "runs" / "t-email-1" / "events.jsonl"
        kept = [l for l in p.read_text().splitlines() if '"effect_committed"' not in l and '"run_finished"' not in l]
        p.write_text("\n".join(kept) + "\n")
        r = o.run(email_task())
        self.assertEqual(r.status, "completed")
        self.assertTrue(r.effects[0]["recovered"])
        self.assertEqual(self.toolbox.effects(), first)

    def test_torn_outbox_tail_is_replaced_by_one_effect(self):
        self.make()
        self.toolbox.effects_file.write_bytes(b'{"effect_id": "partial')
        self.assertEqual(self.toolbox.effects(), [])
        self.toolbox._effect("send_email", {"to": "a@example.com"}, "idem-1")
        parsed = self.toolbox.effects()
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["idempotency_key"], "idem-1")
        self.assertNotIn(b"partial", self.toolbox.effects_file.read_bytes())
        again = self.toolbox._effect("send_email", {"to": "a@example.com"}, "idem-1")
        self.assertTrue(again["deduped"])
        self.assertEqual(len(self.toolbox.effects()), 1)

    def test_corrupt_middle_effect_line_is_not_skipped(self):
        self.make()
        self.toolbox._effect("send_email", {"to": "a@example.com"}, "idem-1")
        good = self.toolbox.effects_file.read_bytes()
        self.toolbox.effects_file.write_bytes(b'{"nope"\n' + good)
        with self.assertRaises(json.JSONDecodeError):
            self.toolbox.effects()
        with self.assertRaises(json.JSONDecodeError):
            self.toolbox._effect("send_email", {"to": "b@example.com"}, "idem-2")
        self.assertEqual(self.toolbox.effects_file.read_bytes(), b'{"nope"\n' + good)

    def test_complete_effect_without_trailing_newline_is_not_resent(self):
        self.make()
        self.toolbox._effect("send_email", {"to": "a@example.com"}, "idem-1")
        self.toolbox.effects_file.write_bytes(self.toolbox.effects_file.read_bytes().rstrip(b"\n"))
        self.assertEqual(len(self.toolbox.effects()), 1)
        again = self.toolbox._effect("send_email", {"to": "a@example.com"}, "idem-1")
        self.assertTrue(again["deduped"])
        self.assertEqual(len(self.toolbox.effects()), 1)
        self.toolbox._effect("publish", {"to": "a@example.com"}, "idem-2")
        parsed = self.toolbox.effects()
        self.assertEqual(len(parsed), 2)
        self.assertEqual({e["idempotency_key"] for e in parsed}, {"idem-1", "idem-2"})


class ApproveCli(unittest.TestCase):
    def test_run_approve_resume_via_cli(self):
        from jarvis_workforce import approve, run
        root = Path(__file__).resolve().parents[1]
        task = root / "examples" / "tasks" / "research_and_email.json"
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            old = os.environ.get("JARVIS_APPROVAL_KEY")
            os.environ["JARVIS_APPROVAL_KEY"] = "cli-test-key-cli-test-key-cli-test-key"
            try:
                self.assertEqual(run.main(["--workdir", d, "--task", str(task)]), 10)      # blocked
                self.assertEqual(approve.main(["--workdir", d, "--run", "demo-email-001"]), 0)  # dry run
                self.assertEqual(len(list((Path(d) / "approvals" / "pending").glob("*.json"))), 0)
                self.assertEqual(approve.main(["--workdir", d, "--run", "demo-email-001", "--yes"]), 0)
                self.assertEqual(run.main(["--workdir", d, "--task", str(task)]), 0)       # completed
                effects = (Path(d) / "mock_outbox" / "effects.jsonl").read_text().splitlines()
                self.assertEqual(len(effects), 1)
            finally:
                if old is None: os.environ.pop("JARVIS_APPROVAL_KEY", None)
                else: os.environ["JARVIS_APPROVAL_KEY"] = old

    def test_no_key_fails_closed(self):
        from jarvis_workforce import run
        root = Path(__file__).resolve().parents[1]
        task = root / "examples" / "tasks" / "research_and_email.json"
        env = os.environ.pop("JARVIS_APPROVAL_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run.main(["--workdir", d, "--task", str(task)]), 10)
                self.assertIn("no approval store", out.getvalue())
                out.truncate(0); out.seek(0)
                self.assertEqual(run.main(["--workdir", d, "--task", str(task), "--no-json"]), 10)
                self.assertNotIn('"status"', out.getvalue())
                self.assertIn("status=blocked", out.getvalue())
        finally:
            if env: os.environ["JARVIS_APPROVAL_KEY"] = env

    def test_malformed_key_clis_fail_closed(self):
        from jarvis_workforce import approve, demo, run
        root = Path(__file__).resolve().parents[1]
        task = str(root / "examples" / "tasks" / "research_and_email.json")
        old = os.environ.pop("JARVIS_APPROVAL_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                os.environ["JARVIS_APPROVAL_KEY"] = "k" * 32
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(run.main(["--workdir", d, "--task", task, "--no-json"]), 10)
                for bad in ("hex:zzzz", "tiny-key"):
                    os.environ["JARVIS_APPROVAL_KEY"] = bad
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = approve.main(["--workdir", d, "--run", "demo-email-001", "--yes"])
                    self.assertEqual(code, 3, bad)
                    self.assertIn("refusing:", err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertNotIn(bad, err.getvalue())
                    self.assertNotIn("action:", out.getvalue())
                    self.assertEqual(list((Path(d) / "approvals" / "pending").glob("*.json")), [])
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = run.main(["--workdir", d, "--task", task, "--no-json"])
                    self.assertEqual(code, 3, bad)
                    self.assertIn("refusing:", err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertNotIn("status=completed", out.getvalue())
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = demo.main(["--scenario", "blocked"])
                    self.assertEqual(code, 3, bad)
                    self.assertIn("refusing:", err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertNotIn("ephemeral", out.getvalue())
                    self.assertNotIn(bad, err.getvalue())
                self.assertFalse((Path(d) / "mock_outbox" / "effects.jsonl").exists())
        finally:
            if old is None: os.environ.pop("JARVIS_APPROVAL_KEY", None)
            else: os.environ["JARVIS_APPROVAL_KEY"] = old

    def test_ttl_is_validated_before_the_request_is_shown(self):
        from jarvis_workforce import approve, run
        root = Path(__file__).resolve().parents[1]
        task = str(root / "examples" / "tasks" / "research_and_email.json")
        old = os.environ.pop("JARVIS_APPROVAL_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                os.environ["JARVIS_APPROVAL_KEY"] = "k" * 32
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(run.main(["--workdir", d, "--task", task, "--no-json"]), 10)
                pending = Path(d) / "approvals" / "pending"
                for bad in ("0", "-5", "4000", "abc"):
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            approve.main(["--workdir", d, "--run", "demo-email-001", "--yes", "--ttl", bad])
                    self.assertEqual(cm.exception.code, 2, bad)
                    self.assertNotIn("Traceback", err.getvalue())
                    self.assertIn("ttl", err.getvalue())
                    self.assertNotIn("action:", out.getvalue())
                    self.assertNotIn("request_sha256", out.getvalue())
                self.assertEqual(list(pending.glob("*.json")), [])
                for ttl in ("1", "3600"):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(approve.main(["--workdir", d, "--run", "demo-email-001", "--yes", "--ttl", ttl]), 0)
                deltas = sorted(json.loads(p.read_text())["expires_at"] - json.loads(p.read_text())["issued_at"]
                                for p in pending.glob("*.json"))
                self.assertEqual(deltas, [1, 3600])
        finally:
            if old is None: os.environ.pop("JARVIS_APPROVAL_KEY", None)
            else: os.environ["JARVIS_APPROVAL_KEY"] = old


if __name__ == "__main__":
    unittest.main()
