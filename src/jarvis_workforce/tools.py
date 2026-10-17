"""Mock tools. Safe tools read a synthetic corpus; consequential tools write to a local mock 'outbox'
(effects.jsonl) instead of touching any real system. No network code anywhere in this package.

Consequential tools take an idempotency key and dedupe on it, modelling an external API that supports
idempotency keys (as many payment and messaging APIs do). Tools never decide permissions: callers must pass
the gate first (see orchestrator.ToolContext)."""
from __future__ import annotations
import hashlib, json, os, re
from pathlib import Path

DOC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}\.md$")
SIDE_EFFECT_ACTIONS = ("send_email", "publish", "purchase")


class ToolError(Exception):
    """Permanent tool failure (bad input). Not retried."""


class TransientToolError(Exception):
    """Temporary failure (timeout, 503...). Retried with backoff."""


class ToolBox:
    def __init__(self, corpus_dir: Path | str, effects_dir: Path | str, faults: dict | None = None):
        self.corpus = Path(corpus_dir).resolve()
        self.effects_dir = Path(effects_dir)
        self.effects_dir.mkdir(parents=True, exist_ok=True)
        self.effects_file = self.effects_dir / "effects.jsonl"
        self.faults = dict(faults or {})  # action -> number of transient failures to inject (testing/demo)
        self.calls: list[str] = []

    def _maybe_fault(self, action: str) -> None:
        if self.faults.get(action, 0) > 0:
            self.faults[action] -= 1
            raise TransientToolError(f"{action}: simulated transient failure")

    def call(self, action: str, params: dict, idempotency_key: str | None = None):
        self.calls.append(action)
        self._maybe_fault(action)
        if action == "list_docs":
            return sorted(p.name for p in self.corpus.glob("*.md"))
        if action == "read_doc":
            name = params.get("name", "")
            if not isinstance(name, str) or not DOC_RE.match(name):
                raise ToolError("read_doc: invalid document name")
            p = (self.corpus / name).resolve()
            if p.parent != self.corpus or not p.is_file():
                raise ToolError("read_doc: no such document")
            return p.read_text(encoding="utf-8")
        if action == "summarize":
            return summarize(params.get("text", ""), int(params.get("max_sentences", 2)), params.get("focus", ""))
        if action in SIDE_EFFECT_ACTIONS:
            if not idempotency_key:
                raise ToolError(f"{action}: idempotency key required for side effects")
            return self._effect(action, params, idempotency_key)
        raise ToolError(f"unknown tool {action!r}")

    # ---------- mock side effects ----------
    def effects(self) -> list[dict]:
        if not self.effects_file.exists():
            return []
        return [json.loads(l) for l in self.effects_file.read_text().splitlines() if l.strip()]

    def lookup_effect(self, idempotency_key: str) -> dict | None:
        return next((e for e in self.effects() if e["idempotency_key"] == idempotency_key), None)

    def _effect(self, action: str, params: dict, key: str) -> dict:
        prior = self.lookup_effect(key)
        if prior:
            return {**prior, "deduped": True}
        eff = {"effect_id": "mock-" + hashlib.sha256(key.encode()).hexdigest()[:12], "action": action,
               "idempotency_key": key, "params": params, "simulated": True}
        with open(self.effects_file, "a") as f:
            f.write(json.dumps(eff, sort_keys=True) + "\n"); f.flush(); os.fsync(f.fileno())
        return {**eff, "deduped": False}


def summarize(text: str, max_sentences: int = 2, focus: str = "") -> str:
    """Deterministic extractive summary: top sentences by overlap with focus words, kept in original order."""
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    if not sents:
        return ""
    fw = set(re.findall(r"[a-z]{4,}", focus.lower()))
    scored = sorted(range(len(sents)), key=lambda i: (-len(fw & set(re.findall(r"[a-z]{4,}", sents[i].lower()))), i))
    keep = sorted(scored[:max(1, max_sentences)])
    return " ".join(sents[i] for i in keep)
