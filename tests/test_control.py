"""Workload control layer: job registry, control gate (fail closed, cache TTL, allow-lists, budget), kill switch,
automatic stop rules, audit log and cost per job. No sleeps: the cache clock is injected."""
import ast, io, json, unittest
from contextlib import redirect_stderr, redirect_stdout
from helpers import ROOT, WorkflowCase, email_task, research_task
from jarvis_workforce import jobs as jobs_cli, run as run_cli
from jarvis_workforce.agents import MockResearcher, MockWriter, ScriptedAgent
from jarvis_workforce.control import (AuditLog, AutoStopRules, ControlGate, JobRegistry, RegistryConflict,
                                      RegistryUnavailable, cost_summary, ttl_from_env, validate_ttl)
from jarvis_workforce.llm import MockModel, cost_usd, count_tokens
from jarvis_workforce.tools import summarize

READ_TOOLS = ["list_docs", "read_doc"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ControlCase(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.reg = JobRegistry(self.work / "control" / "jobs.json")
        self.reg.init()
        self.audit = AuditLog(self.work / "control" / "audit.jsonl")
        self.clock = Clock()

    def gate(self, ttl=30, rules=AutoStopRules()):
        return ControlGate(self.reg, self.audit, ttl_s=ttl, clock=self.clock, rules=rules)

    def register(self, job_id="job-1", tools=READ_TOOLS, models=("mock-small",), **kw):
        return self.reg.register(job_id, "owner-a", tools, models, **kw)

    def decisions(self, **match):
        return [e for e in self.audit.entries() if e["event"] == "decision"
                and all(e.get(k) == v for k, v in match.items())]


class Registry(ControlCase):
    def test_register_get_stop_resume_roundtrip(self):
        self.register(budget_usd=0.5, budget_tokens=1000)
        j = self.reg.get("job-1")
        self.assertEqual((j.owner, j.status, j.allowed_tools, j.budget_micro_usd), ("owner-a", "active",
                                                                                    tuple(READ_TOOLS), 500_000))
        self.assertEqual(self.reg.set_status("job-1", "stopped", "manual").status, "stopped")
        self.assertEqual(self.reg.get("job-1").stop_reason, "manual")
        self.assertEqual(self.reg.set_status("job-1", "active").stop_reason, "")
        self.assertEqual(self.reg.get("job-1").version, 3)

    def test_duplicate_registration_and_invalid_records_refused(self):
        self.register()
        with self.assertRaises(RegistryConflict):
            self.register()
        for bad in ({"job_id": "x"}, {"job_id": "job-2", "tools": ["Bad Tool!"]}):
            with self.assertRaises(ValueError):
                self.reg.register(bad["job_id"], "o", bad.get("tools", ()))

    def test_missing_corrupt_and_tampered_registry_raise(self):
        self.register()
        good = self.reg.path.read_text()
        cases = {"missing": None, "not json": "{ nope", "wrong schema": json.dumps({"schema": 99, "jobs": {}}),
                 "bad status": good.replace('"active"', '"running"'),
                 "unknown field": good.replace('"owner": "owner-a"', '"owner": "owner-a", "admin": true')}
        for name, body in cases.items():
            with self.subTest(name):
                if body is None:
                    self.reg.path.unlink()
                else:
                    self.reg.path.write_text(body)
                with self.assertRaises(RegistryUnavailable):
                    self.reg.get("job-1")
                self.reg.path.write_text(good)

    def test_charge_is_conditional_on_status_and_budget(self):
        self.register(budget_tokens=100)
        self.reg.charge("job-1", 60, 0)
        with self.assertRaises(RegistryConflict):
            self.reg.charge("job-1", 41, 0)
        self.assertEqual(self.reg.get("job-1").spent_tokens, 60)
        self.reg.set_status("job-1", "stopped", "x")
        with self.assertRaises(RegistryConflict):
            self.reg.charge("job-1", 1, 0)


class GateDecisions(ControlCase):
    def test_active_job_with_allowed_tool_is_allowed_and_audited(self):
        self.register()
        d = self.gate().authorize_tool("job-1", "researcher", "read_doc", run_id="r1")
        self.assertTrue(d.allowed)
        e = self.decisions()[-1]
        self.assertEqual((e["job_id"], e["agent"], e["tool"], e["decision"], e["run_id"]),
                         ("job-1", "researcher", "read_doc", "allow", "r1"))
        self.assertTrue(e["ts"].endswith("Z"))

    def test_stopped_job_is_refused(self):
        self.register()
        self.reg.set_status("job-1", "stopped", "operator stop")
        g = self.gate()
        self.assertFalse(g.authorize_tool("job-1", "researcher", "read_doc").allowed)
        d = g.authorize_model("job-1", "researcher", "mock-small", 10, 0.0001)
        self.assertFalse(d.allowed)
        self.assertIn("stopped", d.reason)
        self.assertEqual(self.reg.get("job-1").spent_tokens, 0)

    def test_unknown_or_missing_job_id_refused(self):
        g = self.gate()
        self.assertIn("not registered", g.authorize_tool("ghost-job", "researcher", "read_doc").reason)
        self.assertIn("no job_id", g.authorize_tool(None, "researcher", "read_doc").reason)

    def test_unreachable_or_corrupt_registry_refuses_everything(self):
        self.register()
        for name, breaker in (("deleted", lambda p: p.unlink()), ("corrupt", lambda p: p.write_text("{]")),
                              ("truncated", lambda p: p.write_text(""))):
            with self.subTest(name):
                good = self.reg.path.read_text()
                breaker(self.reg.path)
                g = self.gate()
                for d in (g.authorize_tool("job-1", "researcher", "read_doc"),
                          g.authorize_model("job-1", "researcher", "mock-small", 5, 0.00001)):
                    self.assertFalse(d.allowed)
                    self.assertIn("registry unavailable (fail closed)", d.reason)
                self.reg.path.write_text(good)

    def test_registry_down_after_cache_expiry_refuses(self):
        self.register()
        g = self.gate(ttl=30)
        self.assertTrue(g.authorize_tool("job-1", "researcher", "read_doc").allowed)
        self.reg.path.unlink()
        self.clock.t += 30
        self.assertFalse(g.authorize_tool("job-1", "researcher", "read_doc").allowed)

    def test_tool_allow_list_enforced_and_auto_stops(self):
        self.register()
        g = self.gate()
        d = g.authorize_tool("job-1", "writer", "send_email")
        self.assertFalse(d.allowed)
        self.assertIn("not on the job's allow-list", d.reason)
        self.assertEqual(self.reg.get("job-1").status, "stopped")
        self.assertIn("auto:", self.reg.get("job-1").stop_reason)
        self.assertFalse(g.authorize_tool("job-1", "researcher", "read_doc").allowed)  # applies at once

    def test_allow_list_without_auto_stop_rule_only_refuses(self):
        self.register()
        g = self.gate(rules=AutoStopRules(on_disallowed_tool=False))
        self.assertFalse(g.authorize_tool("job-1", "writer", "publish").allowed)
        self.assertEqual(self.reg.get("job-1").status, "active")
        self.assertFalse(g.authorize_model("job-1", "writer", "mock-large", 1, 0.0).allowed)  # model allow-list

    def test_budget_enforced_tokens_and_usd(self):
        self.register("job-t", budget_tokens=100)
        self.register("job-u", budget_usd=0.001)
        g = self.gate(rules=AutoStopRules(on_budget_overrun=False))
        self.assertTrue(g.authorize_model("job-t", "researcher", "mock-small", 100, 0.0).allowed)
        d = g.authorize_model("job-t", "researcher", "mock-small", 1, 0.0)
        self.assertFalse(d.allowed); self.assertIn("token budget exceeded", d.reason)
        self.assertTrue(g.authorize_model("job-u", "researcher", "mock-small", 1, 0.0009).allowed)
        d = g.authorize_model("job-u", "researcher", "mock-small", 1, 0.0002)
        self.assertFalse(d.allowed); self.assertIn("USD budget exceeded", d.reason)
        self.assertEqual(self.reg.get("job-u").spent_micro_usd, 900)  # refused charge was not applied

    def test_budget_overrun_auto_stops_job(self):
        self.register(budget_tokens=10)
        g = self.gate()
        self.assertFalse(g.authorize_model("job-1", "researcher", "mock-small", 11, 0.0).allowed)
        j = self.reg.get("job-1")
        self.assertEqual(j.status, "stopped"); self.assertIn("budget exceeded", j.stop_reason)
        self.assertTrue([e for e in self.audit.entries() if e["event"] == "stop" and e["agent"] == "auto-stop-rule"])

    def test_usage_settles_reservation_to_actual(self):
        self.register(budget_tokens=1000)
        g = self.gate()
        d = g.authorize_model("job-1", "researcher", "mock-small", 300, cost_usd("mock-small", 44, 256))
        self.assertEqual(self.reg.get("job-1").spent_tokens, 300)
        g.record_usage(d, "researcher", tokens_in=44, tokens_out=20, cost_usd=cost_usd("mock-small", 44, 20))
        j = self.reg.get("job-1")
        self.assertEqual(j.spent_tokens, 64)
        self.assertEqual(j.spent_micro_usd, round(cost_usd("mock-small", 44, 20) * 1e6))

    def test_repeated_errors_auto_stop(self):
        self.register()
        g = self.gate(rules=AutoStopRules(max_consecutive_errors=3))
        for _ in range(2):
            g.record_error("job-1", "researcher", "tool", "read_doc", "boom")
        self.assertEqual(self.reg.get("job-1").status, "active")
        d = g.authorize_tool("job-1", "researcher", "read_doc")
        g.record_usage(d, "researcher")  # a success resets the streak
        for _ in range(2):
            g.record_error("job-1", "researcher", "tool", "read_doc", "boom")
        self.assertEqual(self.reg.get("job-1").status, "active")
        g.record_error("job-1", "researcher", "tool", "read_doc", "boom")
        self.assertEqual(self.reg.get("job-1").status, "stopped")
        self.assertIn("3 consecutive errors", self.reg.get("job-1").stop_reason)

    def test_audit_write_failure_turns_allow_into_refuse(self):
        self.register()
        self.audit.path.mkdir(parents=True)  # a directory where the log file should be -> OSError on append
        d = self.gate().authorize_tool("job-1", "researcher", "read_doc")
        self.assertFalse(d.allowed)
        self.assertIn("audit log unavailable", d.reason)


class CacheTTL(ControlCase):
    def test_stop_takes_effect_within_cache_ttl(self):
        self.register()
        g = self.gate(ttl=30)
        self.assertTrue(g.authorize_tool("job-1", "researcher", "read_doc").allowed)
        self.reg.set_status("job-1", "stopped", "kill switch from another process")
        self.clock.t += 29.9
        self.assertTrue(g.authorize_tool("job-1", "researcher", "read_doc").allowed)   # still cached
        self.clock.t += 0.1
        self.assertFalse(g.authorize_tool("job-1", "researcher", "read_doc").allowed)  # at TTL: re-read, refused

    def test_reads_are_cached_and_counted(self):
        self.register()
        g = self.gate(ttl=30)
        base = self.reg.reads
        for _ in range(5):
            g.authorize_tool("job-1", "researcher", "read_doc")
        self.assertEqual(self.reg.reads - base, 1)
        self.clock.t += 31
        g.authorize_tool("job-1", "researcher", "read_doc")
        self.assertEqual(self.reg.reads - base, 2)

    def test_ttl_zero_means_no_cache(self):
        self.register()
        g = self.gate(ttl=0)
        g.authorize_tool("job-1", "researcher", "read_doc")
        self.reg.set_status("job-1", "stopped", "x")
        self.assertFalse(g.authorize_tool("job-1", "researcher", "read_doc").allowed)

    def test_model_calls_recheck_status_inside_the_window(self):
        self.register()
        g = self.gate(ttl=30)
        self.assertTrue(g.authorize_tool("job-1", "researcher", "read_doc").allowed)
        self.reg.set_status("job-1", "stopped", "x")
        self.assertFalse(g.authorize_model("job-1", "researcher", "mock-small", 5, 0.0).allowed)

    def test_ttl_bounds(self):
        self.assertEqual(validate_ttl(30), 30.0)
        for bad in (-1, 301):
            with self.assertRaises(ValueError):
                validate_ttl(bad)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"JARVIS_REGISTRY_TTL_S": "5"}):
            self.assertEqual(ttl_from_env(), 5.0)
        with mock.patch.dict(os.environ, {"JARVIS_REGISTRY_TTL_S": "9999"}):
            with self.assertRaises(ValueError):
                ttl_from_env()


class OrchestratorIntegration(ControlCase):
    def test_registered_job_completes_and_every_call_is_audited(self):
        self.register()
        r = self.make(control=self.gate()).run(research_task(), job_id="job-1")
        self.assertEqual(r.status, "completed")
        ev = self.events("t-research-1")
        tool_calls = [e for e in ev if e["event"] == "tool_call"]
        model_calls = [e for e in ev if e["event"] == "model_call"]
        self.assertTrue(model_calls)
        allows = self.decisions(decision="allow")
        self.assertEqual(len(allows), len(tool_calls) + len(model_calls))
        usage = [e for e in self.audit.entries() if e["event"] == "usage"]
        self.assertEqual(len(usage), len(allows))
        s = cost_summary(self.audit.entries())["job-1"]
        self.assertEqual(s["model_calls"], len(model_calls))
        self.assertEqual(s["tokens"], sum(e["tokens_in"] + e["tokens_out"] for e in model_calls))
        self.assertAlmostEqual(s["cost_usd"], sum(e["cost_usd"] for e in model_calls), places=8)
        j = self.reg.get("job-1")
        self.assertEqual(j.spent_tokens, s["tokens"])
        for e in self.audit.entries():
            self.assertEqual(set(AuditLog.FIELDS) - set(e), set())

    def test_run_without_job_id_is_refused_before_any_tool_runs(self):
        r = self.make(control=self.gate()).run(research_task())
        self.assertEqual(r.status, "blocked")
        self.assertIn("no job_id", r.reason)
        self.assertEqual(self.toolbox.calls, [])

    def test_stopped_job_run_is_blocked_and_resumes_after_resume(self):
        self.register()
        self.reg.set_status("job-1", "stopped", "kill switch")
        o = self.make(control=self.gate(ttl=0))
        r = o.run(research_task(), job_id="job-1")
        self.assertEqual(r.status, "blocked")
        self.assertIn("stopped", r.reason)
        self.assertEqual(self.toolbox.calls, [])
        self.reg.set_status("job-1", "active")
        self.assertEqual(o.run(research_task(), job_id="job-1").status, "completed")

    def test_registry_down_blocks_run_with_no_tool_or_model_call(self):
        self.register()
        self.reg.path.write_text("garbage")
        o = self.make(control=self.gate())
        r = o.run(research_task(), job_id="job-1")
        self.assertEqual(r.status, "blocked")
        self.assertIn("fail closed", r.reason)
        self.assertEqual((self.toolbox.calls, o.model.calls), ([], 0))

    def test_budget_stops_a_run_mid_way(self):
        self.register(budget_tokens=400)  # enough for one model reservation (prompt + 256), not two
        o = self.make(control=self.gate())
        r = o.run(research_task(), job_id="job-1")
        self.assertEqual(r.status, "blocked")
        self.assertIn("budget exceeded", r.reason)
        self.assertEqual(self.reg.get("job-1").status, "stopped")
        self.assertLessEqual(self.reg.get("job-1").spent_tokens, 400)

    def test_delivery_tool_must_be_on_allow_list_and_no_approval_is_consumed(self):
        self.register()  # read tools only
        o = self.make(control=self.gate(rules=AutoStopRules(on_disallowed_tool=False)))
        t = email_task()
        o.run(t, job_id="job-1")
        self.assertIsNone(next((e for e in self.events("t-email-1") if e["event"] == "approval_required"), None))
        ev = [e for e in self.events("t-email-1") if e["event"] == "control_refused"]
        self.assertEqual(ev[-1]["name"], "send_email")
        self.assertEqual(self.toolbox.effects(), [])

    def test_approved_delivery_with_allowed_tool_completes(self):
        self.register(tools=READ_TOOLS + ["send_email"])
        o = self.make(control=self.gate())
        t = email_task()
        self.assertEqual(o.run(t, job_id="job-1").status, "blocked")
        self.issue_for(self.last_approval_request("t-email-1"))
        r = o.run(t, job_id="job-1")
        self.assertEqual((r.status, r.kind), ("completed", "simulated"))
        self.assertTrue(self.decisions(tool="send_email", decision="allow"))

    def test_repeated_tool_errors_stop_the_job(self):
        self.register()
        o = self.make(control=self.gate(rules=AutoStopRules(max_consecutive_errors=2)), faults={"list_docs": 5},
                      max_retries=3)
        r = o.run(research_task(), job_id="job-1")
        self.assertEqual(r.status, "blocked")
        self.assertIn("consecutive errors", self.reg.get("job-1").stop_reason)

    def test_without_control_gate_behaviour_is_unchanged(self):
        r = self.make().run(research_task())
        self.assertEqual(r.status, "completed")
        self.assertFalse(self.audit.entries())


class ModelPath(unittest.TestCase):
    def test_mock_model_matches_summarize_and_counts_tokens(self):
        text = (CORPUS_DOC := (ROOT / "examples" / "corpus" / "default-deny.md").read_text())
        out = MockModel().complete(text, focus="default deny gate", max_sentences=2)
        self.assertEqual(out.text, summarize(CORPUS_DOC, 2, "default deny gate"))
        self.assertEqual(out.tokens_in, count_tokens(text))
        self.assertEqual(out.cost_usd, cost_usd("mock-small", out.tokens_in, out.tokens_out))

    def test_max_tokens_caps_output(self):
        out = MockModel().complete("One two three four five six seven. Eight nine ten.", max_sentences=2, max_tokens=4)
        self.assertLessEqual(out.tokens_out, 4)

    def test_agents_never_import_or_hold_a_model(self):
        src = (ROOT / "src" / "jarvis_workforce" / "agents.py").read_text()
        tree = ast.parse(src)
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                   {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertFalse({m for m in imported if m and ("llm" in m or "control" in m)})
        self.assertNotIn("MockModel", src)
        for agent in (MockResearcher(), MockWriter(), ScriptedAgent(MockWriter(), [])):
            self.assertFalse([k for k, v in vars(agent).items() if isinstance(v, MockModel)])


class CLI(ControlCase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jobs_cli.main(["--registry", str(self.reg.path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_register_stop_resume_list_costs(self):
        self.assertEqual(self.cli("register", "job-9", "--owner", "ops", "--tools", "list_docs,read_doc",
                                  "--models", "mock-small", "--budget-usd", "0.02")[0], 0)
        code, out, _ = self.cli("stop", "job-9", "--reason", "incident drill")
        self.assertEqual(code, 0); self.assertIn("within its cache TTL", out)
        self.assertEqual(self.reg.get("job-9").status, "stopped")
        self.assertIn("incident drill", self.cli("list")[1])
        self.assertEqual(self.cli("resume", "job-9")[0], 0)
        self.assertEqual(self.reg.get("job-9").status, "active")
        self.assertEqual([e["event"] for e in self.audit.entries()], ["register", "stop", "resume"])
        self.assertIn("job-9", json.loads(self.cli("costs")[1]))

    def test_cli_errors(self):
        self.assertEqual(self.cli("stop", "nope-job")[0], 2)
        self.reg.path.write_text("{bad")
        self.assertEqual(self.cli("list")[0], 3)

    def test_run_cli_with_job(self):
        self.register("cli-job", tools=READ_TOOLS)
        task = self.work / "task.json"
        task.write_text(json.dumps(research_task("t-cli-1")))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok = run_cli.main(["--workdir", str(self.work), "--task", str(task), "--job", "cli-job", "--no-json"])
            self.reg.set_status("cli-job", "stopped", "x")
            task.write_text(json.dumps(research_task("t-cli-2")))
            stopped = run_cli.main(["--workdir", str(self.work), "--task", str(task), "--job", "cli-job", "--no-json"])
        self.assertEqual((ok, stopped), (0, 10))


if __name__ == "__main__":
    unittest.main()
