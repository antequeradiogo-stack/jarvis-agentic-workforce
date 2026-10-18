import sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
CORPUS = ROOT / "examples" / "corpus"
TEST_KEY = b"test-only-key-0123456789abcdef-not-a-secret!!"  # synthetic test key, never used outside tests

from jarvis_workforce.approval_store import ApprovalStore  # noqa: E402
from jarvis_workforce.gate import Gate  # noqa: E402
from jarvis_workforce.orchestrator import Orchestrator  # noqa: E402
from jarvis_workforce.tools import ToolBox  # noqa: E402


def email_task(task_id="t-email-1", **extra_params):
    return {"task_id": task_id, "kind": "research", "goal": "Research default-deny tool gates and draft a short report.",
            "deliver": {"action": "send_email", "params": {"to": "reviewer@example.com", "subject": "Draft", **extra_params}}}


def research_task(task_id="t-research-1"):
    return {"task_id": task_id, "kind": "research", "goal": "Research approval binding and idempotency for agents."}


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jw-test-")
        self.work = Path(self._tmp.name)
        self.sleeps = []
        self.store = ApprovalStore(self.work / "approvals", TEST_KEY)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, faults=None, agents=None, crash_at=None, **kw):
        self.toolbox = ToolBox(CORPUS, self.work / "outbox", faults=faults)
        self.orch = Orchestrator(self.work, self.toolbox, Gate(self.store), agents, sleep=self.sleeps.append,
                                 crash_at=crash_at, **kw)
        return self.orch

    def events(self, run_id):
        import json
        return [json.loads(l) for l in (self.work / "runs" / run_id / "events.jsonl").read_text().splitlines()]

    def last_approval_request(self, run_id):
        return [e for e in self.events(run_id) if e["event"] == "approval_required"][-1]

    def issue_for(self, req, **kw):
        kw.setdefault("ttl", 60)
        return self.store.issue(req["action"], req["params"], run_id=req["run_id"], task_id=req["task_id"], **kw)
