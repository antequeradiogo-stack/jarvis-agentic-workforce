"""Per-agent folder isolation and forbidden actions."""
import os, tempfile, unittest
from pathlib import Path
from helpers import WorkflowCase, email_task, research_task
from jarvis_workforce.agents import MockResearcher, MockWriter
from jarvis_workforce.scope import AgentScope, ScopeError


class Isolation(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.root = Path(self._t.name) / "agents"
        self.a = AgentScope(self.root, "researcher"); self.b = AgentScope(self.root, "writer")

    def tearDown(self): self._t.cleanup()

    def test_write_inside_own_workspace(self):
        p = self.a.write_text("run1/notes.md", "hi")
        self.assertEqual(p.read_text(), "hi"); self.assertTrue(str(p).startswith(str(self.a.dir)))

    def test_write_escape_refused(self):
        for bad in ("../writer/x.md", "/tmp/x.md", "run/../../x.md", str(self.b.dir / "x.md")):
            with self.assertRaises(ScopeError, msg=bad): self.a.write_text(bad, "x")

    def test_symlink_escape_refused(self):
        os.symlink(self.b.dir, self.a.dir / "link")
        with self.assertRaises(ScopeError): self.a.write_text("link/evil.md", "x")
        with self.assertRaises(ScopeError): self.a.read_text("link/evil.md")

    def test_cross_agent_read_refused_common_allowed(self):
        self.b.write_text("secret-plan.md", "b's data")
        with self.assertRaises(ScopeError): self.a.read_text(self.b.dir / "secret-plan.md")
        (self.root / "common").mkdir(); (self.root / "common" / "shared.md").write_text("ok")
        self.assertEqual(self.a.read_text(self.root / "common" / "shared.md"), "ok")

    def test_denials_are_logged_in_own_folder(self):
        with self.assertRaises(ScopeError): self.a.write_text("../writer/x", "x")
        self.assertIn("deny_write", (self.a.dir / "logs" / "scope.jsonl").read_text())

    def test_bad_names(self):
        for n in ("../x", "Common", "common", "", "a" * 40):
            with self.assertRaises(ScopeError): AgentScope(self.root, n)

    def test_forbidden_action_and_records(self):
        s = AgentScope(self.root, "buyer", forbidden=("purchase",))
        with self.assertRaises(ScopeError): s.check_action("purchase")
        s.check_action("summarize")
        with self.assertRaises(ScopeError): s.record("completed_real", "t", {}, evidence=[])
        self.assertEqual(s.record("draft", "t", {})["kind"], "draft")


class ScopedWorkflow(WorkflowCase):
    def scopes(self, writer_forbidden=()):
        return {"researcher": AgentScope(self.work / "agents", "researcher", forbidden=("send_email", "publish", "purchase")),
                "writer": AgentScope(self.work / "agents", "writer", forbidden=writer_forbidden)}

    def test_artifacts_land_in_each_agents_own_folder(self):
        r = self.make(scopes=self.scopes()).run(research_task())
        self.assertIn("/agents/researcher/", r.artifacts["research"])
        self.assertIn("/agents/writer/", r.artifacts["write"])

    def test_forbidden_action_refused_even_with_valid_approval(self):
        o = self.make(scopes=self.scopes(writer_forbidden=("send_email",)))
        o.run(email_task())
        ev = [e for e in self.events("t-email-1") if e["event"] == "gate_decision"][-1]
        self.assertIn("forbidden", ev["reason"])
        from jarvis_workforce.gate import request_sha256  # noqa: F401
        # issue an approval for what the gate would see; still refused because the agent may never do it
        draft = [e for e in self.events("t-email-1") if e["event"] == "step_completed" and e["step"] == "write"][0]
        self.store.issue("send_email", {"to": "reviewer@example.com", "subject": "Draft", "body": draft["output"]["content"]})
        self.assertEqual(o.run(email_task()).status, "blocked")
        self.assertEqual(self.toolbox.effects(), [])

    def test_researcher_cannot_call_send_email_inline(self):
        class Rogue(MockResearcher):
            def run(self, task, ctx, inputs):
                ctx.call("send_email", {"to": "x@example.com"}); return {}
        o = self.make(agents={"researcher": Rogue(), "writer": MockWriter()}, scopes=self.scopes())
        r = o.run(research_task())
        self.assertEqual(r.status, "blocked"); self.assertIn("forbidden", r.reason)


if __name__ == "__main__":
    unittest.main()
