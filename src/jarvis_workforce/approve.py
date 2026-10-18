"""Human approval CLI: show the exact pending request of a blocked run and issue a single-use approval for it.

  PYTHONPATH=src python -m jarvis_workforce.approve --workdir RUNS_ROOT --run RUN_ID            # show only
  PYTHONPATH=src python -m jarvis_workforce.approve --workdir RUNS_ROOT --run RUN_ID --yes --ttl 600

Requires JARVIS_APPROVAL_KEY (environment or .env, mode 0600). There is deliberately no way for an agent or a
task to call this: it is the out-of-band human step."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from .approval_store import MAX_TTL, ApprovalError, ApprovalStore, load_key

ROOT = Path(__file__).resolve().parents[2]


def _parse_ttl(value: str) -> int:
    try:
        ttl = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--ttl must be an integer from 1 to {MAX_TTL}") from None
    if not 1 <= ttl <= MAX_TTL:
        raise argparse.ArgumentTypeError(f"--ttl must be from 1 to {MAX_TTL} seconds")
    return ttl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Approve the pending consequential action of a blocked run.")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--ttl", type=_parse_ttl, default=600, help=f"seconds the approval stays valid (1..{MAX_TTL})")
    ap.add_argument("--approver", default="user")
    ap.add_argument("--yes", action="store_true", help="actually issue the approval (otherwise only show it)")
    a = ap.parse_args(argv)
    key = None
    if a.yes:
        try:
            key = load_key(ROOT)
        except (KeyError, ApprovalError) as e:
            print(f"refusing: {e}", file=sys.stderr); return 3
    log = Path(a.workdir) / "runs" / a.run / "events.jsonl"
    if not log.is_file():
        print(f"no such run: {log}", file=sys.stderr); return 2
    reqs = [json.loads(l) for l in log.read_text().splitlines() if '"approval_required"' in l]
    if not reqs:
        print("run has no pending approval request", file=sys.stderr); return 2
    req = reqs[-1]
    print(f"action:          {req['action']}")
    print(f"request_sha256:  {req['request_sha256']}")
    print("params:\n" + json.dumps(req["params"], indent=2, ensure_ascii=False))
    if not a.yes:
        print("\n(dry run: re-run with --yes to approve exactly this request)"); return 0
    task_id = req.get("task_id")
    if req.get("run_id") != a.run or not isinstance(task_id, str) or not task_id.strip():
        print("refusing: approval request is not bound to this run and its task", file=sys.stderr); return 2
    try:
        body = ApprovalStore(Path(a.workdir) / "approvals", key).issue(
            req["action"], req["params"], ttl=a.ttl, approver=a.approver, run_id=a.run, task_id=task_id)
    except ApprovalError as e:
        print(f"refusing: {e}", file=sys.stderr); return 3
    print(f"\napproved: nonce {body['nonce'][:8]}..., single-use, expires in {a.ttl}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
