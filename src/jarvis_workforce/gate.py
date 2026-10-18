"""Default-deny policy enforcement point (PEP) for every tool call.

Rules (in order):
1. Approval-like keys anywhere in the call params (``approved``, ``override``...) -> DENY (spoofing).
   Approval never travels with task data.
2. Action on the explicit SAFE allow-list (read/summarise, reversible, in-sandbox) -> ALLOW.
3. Anything else (send_email, publish, purchase, and every unknown action) is consequential -> ALLOW only if the
   approval store holds a valid, unexpired, unused, HMAC-signed approval bound to this exact request
   (sha256 of canonical action + params + run_id + task_id), which is consumed atomically. Otherwise DENY.
"""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from typing import Any

SAFE_ACTIONS = frozenset({"list_docs", "read_doc", "summarize"})
KNOWN_CONSEQUENTIAL = frozenset({"send_email", "publish", "purchase"})
APPROVAL_LIKE_KEYS = frozenset({
    "approved", "approval", "approvals", "approved_by", "authorized", "authorised", "authorization",
    "user_approved", "approval_token", "approval_id", "pre_approved", "action_permitted", "capability",
    "is_approved", "allow", "allowed", "override", "sudo"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    request_sha256: str


def canonical(action: str, params: dict, *, run_id: str | None = None, task_id: str | None = None) -> str:
    body = {"action": action, "params": params}
    if run_id is not None:
        body["run_id"] = run_id
    if task_id is not None:
        body["task_id"] = task_id
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_sha256(action: str, params: dict, *, run_id: str | None = None, task_id: str | None = None) -> str:
    return hashlib.sha256(canonical(action, params, run_id=run_id, task_id=task_id).encode()).hexdigest()


def find_approval_claims(obj: Any, path: str = "$", depth: int = 0) -> list[str]:
    if depth > 8:
        return [path + "(too deep)"]
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower().replace("-", "_") in APPROVAL_LIKE_KEYS:
                hits.append(f"{path}.{k}")
            hits += find_approval_claims(v, f"{path}.{k}", depth + 1)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits += find_approval_claims(v, f"{path}[{i}]", depth + 1)
    return hits


class Gate:
    def __init__(self, approval_store=None):
        self.approval_store = approval_store

    def check(self, action: str, params: dict, now: float | None = None, *,
              run_id: str | None = None, task_id: str | None = None) -> Decision:
        sha = request_sha256(action, params, run_id=run_id, task_id=task_id) if isinstance(params, dict) else "-"
        if not isinstance(action, str) or not isinstance(params, dict):
            return Decision(False, "malformed tool call", sha)
        claims = find_approval_claims(params)
        if claims:
            return Decision(False, f"approval-like fields in call data are never approval (spoofing): {claims[:3]}", sha)
        if action in SAFE_ACTIONS:
            return Decision(True, "safe action (allow-list)", sha)
        label = action if action in KNOWN_CONSEQUENTIAL else f"unregistered:{action}"
        if self.approval_store is None:
            return Decision(False, f"default deny: {label} needs approval and no approval store is configured", sha)
        ok, why = self.approval_store.consume(action, params, now=now, run_id=run_id, task_id=task_id)
        if not ok:
            return Decision(False, f"default deny: {label}: {why}", sha)
        return Decision(True, f"approved: {why}", sha)
