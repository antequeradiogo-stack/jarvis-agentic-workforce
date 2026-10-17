"""Validated records: tasks in, agent outputs between steps, results out.

Everything that crosses a boundary (task intake, agent -> orchestrator, orchestrator -> caller) is validated
strictly: unknown fields, wrong types and out-of-range values raise ValidationError. Agent output is treated
as untrusted (an LLM-backed agent could return anything)."""
from __future__ import annotations
import re
from dataclasses import asdict, dataclass, field
from typing import Any

TASK_KINDS = ("research", "analysis", "draft", "review")
DELIVERY_ACTIONS = ("send_email", "publish", "purchase")
OUTPUT_KINDS = ("research", "draft")
RESULT_STATUSES = ("completed", "blocked", "failed", "rejected")
RESULT_KINDS = ("suggestion", "draft", "simulated", "completed_real")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
MAX_TEXT = 20_000


class ValidationError(ValueError):
    pass


def _exact_keys(d: Any, required: set, optional: set = frozenset(), where: str = "record") -> None:
    if not isinstance(d, dict):
        raise ValidationError(f"{where}: expected object, got {type(d).__name__}")
    missing, extra = required - set(d), set(d) - required - set(optional)
    if missing:
        raise ValidationError(f"{where}: missing fields {sorted(missing)}")
    if extra:
        raise ValidationError(f"{where}: unknown fields {sorted(extra)}")


def _text(v: Any, where: str, max_len: int = MAX_TEXT) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{where}: must be a non-empty string")
    if len(v) > max_len:
        raise ValidationError(f"{where}: too long ({len(v)} > {max_len})")
    return v


@dataclass(frozen=True)
class ToolCall:
    action: str
    params: dict

    @classmethod
    def from_dict(cls, d: Any, where: str = "tool_call") -> "ToolCall":
        _exact_keys(d, {"action", "params"}, where=where)
        if not isinstance(d["action"], str) or not re.match(r"^[a-z][a-z0-9_]{1,40}$", d["action"]):
            raise ValidationError(f"{where}.action: invalid")
        if not isinstance(d["params"], dict):
            raise ValidationError(f"{where}.params: must be an object")
        return cls(d["action"], dict(d["params"]))


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    kind: str
    goal: str
    deliver: ToolCall | None = None

    @classmethod
    def from_dict(cls, d: Any) -> "TaskRequest":
        _exact_keys(d, {"task_id", "kind", "goal"}, {"deliver"}, where="task")
        if not isinstance(d["task_id"], str) or not ID_RE.match(d["task_id"]):
            raise ValidationError("task.task_id: invalid (3-64 chars, [A-Za-z0-9_.-])")
        if d["kind"] not in TASK_KINDS:
            raise ValidationError(f"task.kind: must be one of {TASK_KINDS}")
        goal = _text(d["goal"], "task.goal", 2000)
        deliver = None
        if d.get("deliver") is not None:
            deliver = ToolCall.from_dict(d["deliver"], "task.deliver")
            if deliver.action not in DELIVERY_ACTIONS:
                raise ValidationError(f"task.deliver.action: must be one of {DELIVERY_ACTIONS}")
        return cls(d["task_id"], d["kind"], goal, deliver)

    def to_dict(self) -> dict:
        d = {"task_id": self.task_id, "kind": self.kind, "goal": self.goal}
        if self.deliver:
            d["deliver"] = asdict(self.deliver)
        return d


@dataclass(frozen=True)
class AgentOutput:
    agent: str
    kind: str
    content: str
    sources: tuple[str, ...] = ()

    @classmethod
    def validate(cls, raw: Any, expected_agent: str, expected_kind: str) -> "AgentOutput":
        _exact_keys(raw, {"agent", "kind", "content"}, {"sources"}, where=f"{expected_agent} output")
        if raw["agent"] != expected_agent:
            raise ValidationError(f"output.agent: expected {expected_agent!r}, got {raw['agent']!r}")
        if raw["kind"] != expected_kind:
            raise ValidationError(f"output.kind: expected {expected_kind!r}, got {raw['kind']!r}")
        content = _text(raw["content"], "output.content")
        sources = raw.get("sources", [])
        if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
            raise ValidationError("output.sources: must be a list of non-empty strings")
        return cls(expected_agent, expected_kind, content, tuple(sources))

    def to_dict(self) -> dict:
        return {"agent": self.agent, "kind": self.kind, "content": self.content, "sources": list(self.sources)}


@dataclass(frozen=True)
class Result:
    run_id: str
    task_id: str
    status: str
    kind: str
    summary: str
    artifacts: dict = field(default_factory=dict)
    blocked_actions: tuple = ()
    effects: tuple = ()
    evidence: tuple = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValidationError(f"result.status: unknown {self.status!r}")
        if self.kind not in RESULT_KINDS:
            raise ValidationError(f"result.kind: unknown {self.kind!r}")
        if self.kind == "completed_real" and not self.evidence:
            raise ValidationError("result: completed_real requires evidence")

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("blocked_actions", "effects", "evidence"):
            d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        d = dict(d)
        for k in ("blocked_actions", "effects", "evidence"):
            d[k] = tuple(d.get(k, ()))
        return cls(**d)
