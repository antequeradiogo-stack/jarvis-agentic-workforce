"""Orchestrator ("Jarvis"): validate task -> researcher -> writer -> optional delivery, with every tool call
gate-checked, every agent output validated, retries with exponential backoff, and a JSON-lines run log that
doubles as a checkpoint so a crashed or paused run resumes without repeating work or side effects.

Run statuses: completed | blocked (a consequential action was denied; resumable after approval) |
failed (retries exhausted / permanent error) | rejected (invalid or unsafe task, nothing ran)."""
from __future__ import annotations
import hashlib, json, time
from pathlib import Path
from typing import Callable

from .agents import MockResearcher, MockWriter
from .gate import Gate, request_sha256
from .models import AgentOutput, Result, TaskRequest, ValidationError
from .runlog import RunLog
from .tools import ToolBox, ToolError, TransientToolError


class ToolDenied(Exception):
    pass


class SimulatedCrash(Exception):
    """Fault injection: simulates the process dying at a named point."""


class StepFailed(Exception):
    def __init__(self, reason: str, blocked: bool = False):
        super().__init__(reason); self.reason, self.blocked = reason, blocked


class ToolContext:
    """What an agent is allowed to touch: gate-checked tool calls, each one logged."""

    def __init__(self, agent: str, gate: Gate, toolbox: ToolBox, log: RunLog, forbidden=(),
                 run_id: str | None = None, task_id: str | None = None):
        self.agent, self.gate, self.toolbox, self.log, self.forbidden = agent, gate, toolbox, log, set(forbidden)
        self.run_id, self.task_id = run_id, task_id

    def call(self, action: str, params: dict):
        if action in self.forbidden:
            self.log.write("tool_denied", agent=self.agent, action=action, reason="forbidden for this agent")
            raise ToolDenied(f"{self.agent}: {action} is forbidden for this agent")
        d = self.gate.check(action, params, run_id=self.run_id, task_id=self.task_id)
        if not d.allowed:
            self.log.write("tool_denied", agent=self.agent, action=action, reason=d.reason)
            raise ToolDenied(d.reason)
        out = self.toolbox.call(action, params)
        self.log.write("tool_call", agent=self.agent, action=action, allowed=True, reason=d.reason)
        return out


class Orchestrator:
    STEPS = (("research", "researcher"), ("write", "writer"))

    def __init__(self, workdir: Path | str, toolbox: ToolBox, gate: Gate, agents: dict | None = None, *,
                 max_retries: int = 3, backoff_base: float = 0.05, sleep: Callable[[float], None] = time.sleep,
                 crash_at: set | None = None, echo: Callable[[dict], None] | None = None, intake_policy=None,
                 scopes: dict | None = None):
        self.workdir = Path(workdir)
        self.toolbox, self.gate = toolbox, gate
        self.agents = agents or {"researcher": MockResearcher(), "writer": MockWriter()}
        self.max_retries, self.backoff_base, self.sleep = max_retries, backoff_base, sleep
        self.crash_at = set(crash_at or ())
        self.echo = echo
        self.intake_policy = intake_policy  # callable(goal) -> (ok, reason); see policy.intake_check
        self.scopes = scopes or {}          # agent name -> AgentScope (folder isolation + forbidden actions)

    # ---------- helpers ----------
    def _crash_point(self, name: str) -> None:
        if name in self.crash_at:
            self.crash_at.discard(name)
            raise SimulatedCrash(name)

    def run_dir(self, run_id: str) -> Path:
        return self.workdir / "runs" / run_id

    def _write_artifact(self, run_id: str, agent: str, name: str, text: str) -> str:
        scope = self.scopes.get(agent)
        if scope is not None:
            return str(scope.write_text(f"{run_id}/{name}", text))
        p = self.run_dir(run_id) / "artifacts" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(p)

    # ---------- main entry ----------
    def run(self, task_in, run_id: str | None = None) -> Result:
        try:
            task = task_in if isinstance(task_in, TaskRequest) else TaskRequest.from_dict(task_in)
        except ValidationError as e:
            tid = task_in.get("task_id") if isinstance(task_in, dict) and isinstance(task_in.get("task_id"), str) else "?"
            return Result(run_id or "-", tid[:64] or "?", "rejected", "suggestion", "task rejected", reason=f"invalid task: {e}")
        run_id = run_id or task.task_id
        log = RunLog(self.run_dir(run_id) / "events.jsonl", run_id, echo=self.echo)

        finished = log.find("run_finished")
        if finished and finished[-1]["result"]["status"] != "blocked":
            log.write("run_already_finished", status=finished[-1]["result"]["status"])
            return Result.from_dict(finished[-1]["result"])
        if log.events():
            started = log.find("run_started")
            if started and started[0]["task"] != task.to_dict():
                raise ValueError(f"run {run_id} exists for a different task; refusing to mix checkpoints")
            log.write("run_resumed", completed_steps=[e["step"] for e in log.find("step_completed")])
        else:
            log.write("run_started", task=task.to_dict())

        if self.intake_policy is not None:
            ok, why = self.intake_policy(task.goal)
            log.write("intake_policy", ok=ok, reason=why)
            if not ok:
                return self._finish(log, Result(run_id, task.task_id, "rejected", "suggestion", "task rejected by intake policy",
                                                reason=why, evidence=(f"runlog:{log.path}",)))

        outputs, artifacts = {}, {}
        try:
            for step, agent_name in self.STEPS:
                outputs[step], artifacts[step] = self._step(log, task, run_id, step, agent_name, outputs)
        except StepFailed as e:
            return self._finish(log, Result(run_id, task.task_id, "blocked" if e.blocked else "failed",
                                            "draft" if artifacts else "suggestion", f"step failed: {e.reason}",
                                            artifacts=artifacts, reason=e.reason, evidence=(f"runlog:{log.path}",)))

        effects, blocked = [], []
        if task.deliver is not None:
            params = {**task.deliver.params, "body": outputs["write"].content}
            try:
                eff = self._deliver(log, run_id, task.deliver.action, params, task.task_id)
                if eff is not None:
                    effects.append(eff)
            except ToolDenied as e:
                blocked.append({"action": task.deliver.action,
                                "request_sha256": request_sha256(task.deliver.action, params, run_id=run_id, task_id=task.task_id),
                                "reason": str(e)})
            except StepFailed as e:
                return self._finish(log, Result(run_id, task.task_id, "failed", "draft", f"delivery failed: {e.reason}",
                                                artifacts=artifacts, reason=e.reason, evidence=(f"runlog:{log.path}",)))
        if blocked:
            res = Result(run_id, task.task_id, "blocked", "draft",
                         f"draft ready; {blocked[0]['action']} needs a user approval for request {blocked[0]['request_sha256'][:12]}",
                         artifacts=artifacts, blocked_actions=tuple(blocked), reason=blocked[0]["reason"],
                         evidence=(f"runlog:{log.path}",))
        else:
            res = Result(run_id, task.task_id, "completed", "simulated" if effects else "draft",
                         ("draft delivered via mock " + effects[0]["action"]) if effects else "draft ready (no delivery requested)",
                         artifacts=artifacts, effects=tuple(effects),
                         evidence=(f"runlog:{log.path}",) + tuple(f"mock_effect:{e['effect_id']}" for e in effects))
        return self._finish(log, res)

    def _finish(self, log: RunLog, res: Result) -> Result:
        (log.path.parent / "result.json").write_text(json.dumps(res.to_dict(), indent=2, sort_keys=True))
        log.write("run_finished", result=res.to_dict())
        return res

    # ---------- agent steps with validation + retries ----------
    def _step(self, log, task, run_id, step, agent_name, outputs):
        agent = self.agents[agent_name]
        done = log.find("step_completed", step=step)
        if done:
            out = AgentOutput.validate(done[-1]["output"], agent_name, agent.output_kind)
            log.write("step_skipped", step=step, reason="already completed (checkpoint)")
            return out, done[-1]["artifact"]
        scope = self.scopes.get(agent_name)
        ctx = ToolContext(agent_name, self.gate, self.toolbox, log, forbidden=scope.forbidden if scope else (),
                          run_id=run_id, task_id=task.task_id)
        last = "unknown"
        for attempt in range(1, self.max_retries + 1):
            log.write("step_started", step=step, agent=agent_name, attempt=attempt)
            try:
                raw = agent.run(task, ctx, outputs)
                out = AgentOutput.validate(raw, agent_name, agent.output_kind)
            except SimulatedCrash:
                raise
            except ValidationError as e:
                last = f"malformed output: {e}"; log.write("malformed_output", step=step, attempt=attempt, error=str(e))
            except TransientToolError as e:
                last = f"transient: {e}"; log.write("transient_error", step=step, attempt=attempt, error=str(e))
            except ToolDenied as e:
                log.write("step_blocked", step=step, reason=str(e)); raise StepFailed(f"{step}: {e}", blocked=True)
            except (ToolError, Exception) as e:  # permanent / unexpected: fail fast, never retry blindly
                log.write("step_error", step=step, attempt=attempt, error=f"{type(e).__name__}: {e}")
                raise StepFailed(f"{step}: {type(e).__name__}: {e}")
            else:
                artifact = self._write_artifact(run_id, agent_name, f"{step}.md", out.content)
                log.write("step_completed", step=step, agent=agent_name, attempt=attempt, output=out.to_dict(), artifact=artifact)
                self._crash_point(f"after_step:{step}")
                return out, artifact
            if attempt < self.max_retries:
                delay = self.backoff_base * 2 ** (attempt - 1)
                log.write("retry_scheduled", step=step, next_attempt=attempt + 1, delay_s=delay)
                self.sleep(delay)
        log.write("step_failed", step=step, reason=last)
        raise StepFailed(f"{step}: retries exhausted ({last})")

    # ---------- consequential delivery: gate + idempotency ----------
    def _deliver(self, log, run_id, action, params, task_id: str):
        sha = request_sha256(action, params, run_id=run_id, task_id=task_id)
        key = hashlib.sha256(f"{run_id}|deliver|{sha}".encode()).hexdigest()[:32]
        committed = log.find("effect_committed", idempotency_key=key)
        if committed:
            log.write("effect_skipped", action=action, idempotency_key=key, reason="already committed (checkpoint)")
            return committed[-1]["effect"]
        prior = self.toolbox.lookup_effect(key)
        if prior:  # executed before a crash but never logged: adopt it, do NOT re-run or re-ask for approval
            eff = {"action": action, "effect_id": prior["effect_id"], "idempotency_key": key, "recovered": True}
            log.write("effect_committed", action=action, idempotency_key=key, effect=eff, recovered=True)
            return eff
        writer_scope = self.scopes.get("writer")
        if writer_scope is not None and action in writer_scope.forbidden:
            log.write("gate_decision", action=action, allowed=False, reason="forbidden for this agent")
            raise ToolDenied(f"{action} is forbidden for the writer agent")
        d = self.gate.check(action, params, run_id=run_id, task_id=task_id)
        log.write("gate_decision", action=action, allowed=d.allowed, reason=d.reason, request_sha256=sha)
        if not d.allowed:
            log.write("approval_required", action=action, params=params, request_sha256=sha, task_id=task_id)
            raise ToolDenied(d.reason)
        last = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                res = self.toolbox.call(action, params, idempotency_key=key)
                break
            except TransientToolError as e:
                last = str(e); log.write("transient_error", step="deliver", attempt=attempt, error=last)
                if attempt < self.max_retries:
                    delay = self.backoff_base * 2 ** (attempt - 1)
                    log.write("retry_scheduled", step="deliver", next_attempt=attempt + 1, delay_s=delay)
                    self.sleep(delay)
            except ToolError as e:
                log.write("step_error", step="deliver", error=str(e)); raise StepFailed(f"deliver: {e}")
        else:
            log.write("step_failed", step="deliver", reason=last)
            raise StepFailed(f"deliver: retries exhausted ({last})")
        self._crash_point(f"after_effect:{action}")
        eff = {"action": action, "effect_id": res["effect_id"], "idempotency_key": key, "deduped": res["deduped"]}
        log.write("effect_committed", action=action, idempotency_key=key, effect=eff)
        return eff
