"""Per-agent folder isolation and forbidden actions (ported and generalised from an
earlier private prototype; deployment-specific paths, config loading and task plumbing removed).

Rules:
  1. an agent writes only inside its own workspace  <agents_root>/<name>/  (realpath-resolved: symlinks and '..'
     cannot escape)
  2. an agent cannot read another agent's workspace under <agents_root>/ (the shared 'common' folder is allowed)
  3. actions in ``forbidden`` are refused for this agent even with a valid approval (enforced by the orchestrator
     before the gate is asked)
  4. records carry kind in {suggestion, draft, simulated, completed_real}; completed_real needs evidence
Limits: an in-process check for cooperative code; it is not an OS sandbox (a malicious agent running arbitrary code
could use open() directly). OS-level isolation (separate users/containers) is a later phase."""
from __future__ import annotations
import datetime as _dt, json, os, re
from pathlib import Path

RECORD_KINDS = ("suggestion", "draft", "simulated", "completed_real")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SHARED = "common"


class ScopeError(PermissionError):
    pass


class AgentScope:
    def __init__(self, agents_root: Path | str, name: str, forbidden=()):
        if not isinstance(name, str) or not NAME_RE.match(name) or name == SHARED:
            raise ScopeError(f"invalid agent name {name!r}")
        self.root = Path(os.path.realpath(agents_root))
        self.name = name
        self.dir = self.root / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.forbidden = frozenset(forbidden)
        self.logs = self.dir / "logs"

    def _resolve(self, p) -> Path:
        p = Path(p)
        return Path(os.path.realpath(p if p.is_absolute() else self.dir / p))

    def _inside(self, rp: Path, base: Path) -> bool:
        return rp == base or base in rp.parents

    def check_write(self, p) -> Path:
        rp = self._resolve(p)
        if not self._inside(rp, self.dir):
            self.log("deny_write", path=str(rp))
            raise ScopeError(f"{self.name}: write outside own workspace refused")
        return rp

    def check_read(self, p) -> Path:
        rp = self._resolve(p)
        if self._inside(rp, self.root) and not self._inside(rp, self.dir):
            rel = rp.relative_to(self.root).parts
            if rel and rel[0] != SHARED:
                self.log("deny_read", path=str(rp))
                raise ScopeError(f"{self.name}: cross-agent workspace access refused")
        return rp

    def read_text(self, p) -> str:
        return self.check_read(p).read_text()

    def write_text(self, p, text: str) -> Path:
        rp = self.check_write(p)
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_name(rp.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, rp)
        return rp

    def check_action(self, action: str) -> None:
        if action in self.forbidden:
            self.log("deny_action", action=action)
            raise ScopeError(f"{self.name}: action {action!r} is forbidden for this agent")

    def log(self, event: str, **kw) -> None:
        self.logs.mkdir(exist_ok=True)
        with open(self.logs / "scope.jsonl", "a") as f:
            f.write(json.dumps({"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                                "agent": self.name, "event": event, **kw}, default=str) + "\n")

    def record(self, kind: str, title: str, payload, evidence=None) -> dict:
        if kind not in RECORD_KINDS:
            raise ValueError(f"kind must be one of {RECORD_KINDS}")
        if kind == "completed_real":
            ok = isinstance(evidence, list) and evidence and all(isinstance(e, dict) and e.get("type") and e.get("ref") for e in evidence)
            if not ok:
                raise ScopeError("completed_real requires non-empty evidence [{type, ref}]")
        return {"kind": kind, "title": title, "agent": self.name, "evidence": evidence or [], "payload": payload}
