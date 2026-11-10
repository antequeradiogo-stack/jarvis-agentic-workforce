"""Run (or resume) one task with persistent state, for the real human-in-the-loop flow:

  PYTHONPATH=src python -m jarvis_workforce.run --workdir ./var --task examples/tasks/research_and_email.json
  PYTHONPATH=src python -m jarvis_workforce.approve --workdir ./var --run demo-email-001 --yes
  PYTHONPATH=src python -m jarvis_workforce.run --workdir ./var --task examples/tasks/research_and_email.json

Without JARVIS_APPROVAL_KEY the gate has no approval store, so every consequential action is denied (fail closed).

With ``--job JOB_ID`` the workload control gate is switched on: every model and tool call must belong to that
active, registered job (registry: ``<workdir>/control/jobs.json`` or ``--registry``; audit log next to it). A missing
or unreadable registry refuses every call. Manage jobs with ``python -m jarvis_workforce.jobs``."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from .approval_store import ApprovalError, ApprovalStore, load_key
from .control import AuditLog, ControlGate, JobRegistry, ttl_from_env
from .demo import CORPUS, ROOT, echo
from .gate import Gate
from .orchestrator import Orchestrator
from .policy import intake_check
from .scope import AgentScope
from .tools import ToolBox


def build_control(registry_path: Path) -> ControlGate:
    return ControlGate(JobRegistry(registry_path), AuditLog(registry_path.with_name("audit.jsonl")),
                       ttl_s=ttl_from_env())


def build(workdir: Path, echo_fn=echo, control: ControlGate | None = None) -> Orchestrator:
    try:
        store = ApprovalStore(workdir / "approvals", load_key(ROOT))
    except KeyError:
        print("note: no JARVIS_APPROVAL_KEY; consequential actions will be denied (fail closed)", file=sys.stderr)
        store = None
    scopes = {"researcher": AgentScope(workdir / "agents", "researcher", forbidden=("send_email", "publish", "purchase")),
              "writer": AgentScope(workdir / "agents", "writer", forbidden=("purchase",))}
    return Orchestrator(workdir, ToolBox(CORPUS, workdir / "mock_outbox"), Gate(store), echo=echo_fn,
                        intake_policy=intake_check, scopes=scopes, control=control)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run or resume a task.")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--task", required=True, help="path to a task JSON file")
    ap.add_argument("--run-id")
    ap.add_argument("--no-json", action="store_true", help="print only the trace, not the result JSON")
    ap.add_argument("--job", help="registered job_id; enables the workload control gate for every call")
    ap.add_argument("--registry", help="job registry JSON (default: <workdir>/control/jobs.json)")
    a = ap.parse_args(argv)
    try:
        control = None
        if a.job or a.registry:
            control = build_control(Path(a.registry) if a.registry else Path(a.workdir) / "control" / "jobs.json")
        orch = build(Path(a.workdir), control=control)
    except (ApprovalError, ValueError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 3
    res = orch.run(json.loads(Path(a.task).read_text()), run_id=a.run_id, job_id=a.job)
    if not a.no_json:
        print(json.dumps(res.to_dict(), indent=2))
    return {"completed": 0, "blocked": 10, "failed": 1, "rejected": 2}[res.status]


if __name__ == "__main__":
    sys.exit(main())
