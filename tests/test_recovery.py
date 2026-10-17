"""Checkpoint/resume: crashed or paused runs resume without repeating steps or side effects."""
import contextlib, io, json, os, tempfile, unittest
from pathlib import Path
from helpers import WorkflowCase, email_task, research_task
from jarvis_workforce.orchestrator import SimulatedCrash
from jarvis_workforce.runlog import RunLog


class Resume(WorkflowCase):
    def approve_pending(self, run_id):
        req = self.last_approval_request(run_id)
        self.store.issue(req["action"], req["params"], ttl=60)

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


if __name__ == "__main__":
    unittest.main()
