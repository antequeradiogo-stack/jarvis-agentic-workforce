"""Append-only JSON-lines run log. It is both the audit trail and the checkpoint used for recovery.

Each line: {"seq", "ts", "run_id", "event", ...fields}. Lines are flushed + fsync'd so a crash after
``write()`` returns never loses the event. A torn final line (crash mid-write) is ignored on read."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from typing import Any, Callable


class RunLog:
    def __init__(self, path: Path | str, run_id: str, echo: Callable[[dict], None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.echo = echo
        self._seq = len(self.events())

    def write(self, event: str, **fields: Any) -> dict:
        self._seq += 1
        rec = {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.run_id, "event": event, **fields}
        line = json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n"); f.flush(); os.fsync(f.fileno())
        if self.echo:
            self.echo(rec)
        return rec

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # torn last line after a crash
        return out

    def find(self, event: str, **match: Any) -> list[dict]:
        return [e for e in self.events() if e.get("event") == event and all(e.get(k) == v for k, v in match.items())]
