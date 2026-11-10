"""Job registry CLI and kill switch.

  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json init
  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json register demo-job-001 \\
      --owner ops --tools list_docs,read_doc --models mock-small --budget-usd 0.01 --budget-tokens 20000
  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json stop demo-job-001 --reason "manual stop"
  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json resume demo-job-001
  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json list
  PYTHONPATH=src python -m jarvis_workforce.jobs --registry ./var/control/jobs.json costs

The audit log defaults to ``audit.jsonl`` next to the registry. A stop reaches running gates within their cache
TTL (default 30 s). Exit codes: 0 ok, 2 bad input or unknown job, 3 registry unavailable."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

from .control import AuditLog, JobRegistry, RegistryConflict, RegistryUnavailable, cost_summary


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def _show(job) -> dict:
    d = job.to_dict()
    d["budget_usd"] = None if job.budget_micro_usd is None else job.budget_micro_usd / 1e6
    d["spent_usd"] = job.spent_micro_usd / 1e6
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Job registry and kill switch for the workload control gate.")
    ap.add_argument("--registry", default=os.environ.get("JARVIS_JOB_REGISTRY", "./var/control/jobs.json"))
    ap.add_argument("--audit", help="audit log path (default: audit.jsonl next to the registry)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create an empty registry if none exists")
    r = sub.add_parser("register", help="register a new active job")
    r.add_argument("job_id")
    r.add_argument("--owner", required=True)
    r.add_argument("--tools", default="", help="comma-separated tool allow-list")
    r.add_argument("--models", default="", help="comma-separated model allow-list")
    r.add_argument("--budget-usd", type=float)
    r.add_argument("--budget-tokens", type=int)
    s = sub.add_parser("stop", help="kill switch: stop a job (refused by every gate within the cache TTL)")
    s.add_argument("job_id")
    s.add_argument("--reason", default="stopped by operator")
    s.add_argument("--by", default="operator")
    u = sub.add_parser("resume", help="set a stopped job back to active")
    u.add_argument("job_id")
    u.add_argument("--by", default="operator")
    sub.add_parser("list", help="list jobs")
    sh = sub.add_parser("show", help="show one job")
    sh.add_argument("job_id")
    sub.add_parser("costs", help="cost per job from the audit log")
    a = ap.parse_args(argv)

    reg = JobRegistry(a.registry)
    audit = AuditLog(a.audit or Path(a.registry).with_name("audit.jsonl"))
    try:
        if a.cmd == "init":
            reg.init(); print(f"registry ready: {reg.path}")
        elif a.cmd == "register":
            job = reg.register(a.job_id, a.owner, _csv(a.tools), _csv(a.models), budget_usd=a.budget_usd,
                               budget_tokens=a.budget_tokens)
            audit.write("register", job_id=job.job_id, agent=a.owner, reason="registered")
            print(json.dumps(_show(job), indent=2))
        elif a.cmd == "stop":
            job = reg.set_status(a.job_id, "stopped", a.reason)
            audit.write("stop", job_id=job.job_id, agent=a.by, reason=a.reason)
            print(f"stopped {job.job_id}: refused by every gate within its cache TTL (default 30 s)")
        elif a.cmd == "resume":
            job = reg.set_status(a.job_id, "active")
            audit.write("resume", job_id=job.job_id, agent=a.by, reason="resumed")
            print(f"resumed {job.job_id}")
        elif a.cmd == "list":
            for j in reg.list():
                cap = "-" if j.budget_micro_usd is None else f"{j.budget_micro_usd / 1e6:.6f}"
                print(f"{j.job_id:<24} {j.status:<8} owner={j.owner} spent_usd={j.spent_micro_usd / 1e6:.6f}/{cap} "
                      f"tokens={j.spent_tokens}/{j.budget_tokens if j.budget_tokens is not None else '-'}"
                      + (f" reason={j.stop_reason!r}" if j.stop_reason else ""))
        elif a.cmd == "show":
            job = reg.get(a.job_id)
            if job is None:
                print(f"unknown job {a.job_id}", file=sys.stderr); return 2
            print(json.dumps(_show(job), indent=2))
        elif a.cmd == "costs":
            print(json.dumps(cost_summary(audit.entries()), indent=2, sort_keys=True))
    except RegistryUnavailable as e:
        print(f"registry unavailable: {e}", file=sys.stderr); return 3
    except KeyError as e:
        print(f"unknown job {e}", file=sys.stderr); return 2
    except (RegistryConflict, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
