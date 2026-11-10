"""Workload control layer: job registry, one control gate for every model and tool call, kill switch, audit log.

Inspired by OpenAI's "Control Room" write-up (9 Nov 2026): every agent job has a registered ``job_id``; one gate
checks every request against the registry before it runs; if the registry cannot be read the gate refuses
everything (fail closed); a stopped job is refused within the registry cache window.

Local backend: one JSON file guarded by an ``fcntl`` lock with atomic replace (stdlib only). The AWS path is a
DynamoDB table with the same item shape and conditional writes (``docs/aws-control-plane.md``).

Stop delay: status reads are cached for ``ttl_s`` (default 30 s, ``JARVIS_REGISTRY_TTL_S``, max 300). A stop issued
from another process is therefore refused on the first call after the cached entry expires: worst case ``ttl_s``.
Model calls also reserve budget with a conditional write that re-checks the status, so a stopped job cannot spend
more model budget even inside the window. A stop decided by this gate itself (an automatic stop rule) clears its
cache and applies at once."""
from __future__ import annotations
import contextlib, fcntl, json, os, re, time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

JOB_STATUSES = ("active", "stopped")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,40}$")
DEFAULT_TTL_S = 30.0
MAX_TTL_S = 300.0
SCHEMA = 1


class RegistryUnavailable(Exception):
    """The registry could not be read or written (missing, unreadable, corrupt, invalid). Callers must refuse."""


class RegistryConflict(Exception):
    """A conditional write failed: the job is not active, or the charge would exceed its budget."""


def usd_to_micro(usd: float | None) -> int | None:
    return None if usd is None else int(round(float(usd) * 1_000_000))


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ttl_from_env(default: float = DEFAULT_TTL_S) -> float:
    raw = os.environ.get("JARVIS_REGISTRY_TTL_S")
    return validate_ttl(float(raw)) if raw not in (None, "") else default


def validate_ttl(ttl: float) -> float:
    if not 0 <= ttl <= MAX_TTL_S:
        raise ValueError(f"registry cache TTL must be between 0 and {MAX_TTL_S:.0f} s (it bounds the stop delay)")
    return float(ttl)


@dataclass(frozen=True)
class Job:
    job_id: str
    owner: str
    allowed_tools: tuple = ()
    allowed_models: tuple = ()
    budget_micro_usd: int | None = None
    budget_tokens: int | None = None
    status: str = "active"
    spent_micro_usd: int = 0
    spent_tokens: int = 0
    stop_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not JOB_ID_RE.match(self.job_id):
            raise ValueError("job_id: invalid (3-64 chars, [A-Za-z0-9_.-])")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("owner: required")
        if self.status not in JOB_STATUSES:
            raise ValueError(f"status: must be one of {JOB_STATUSES}")
        for name in (*self.allowed_tools, *self.allowed_models):
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise ValueError(f"allow-list entry invalid: {name!r}")
        for k in ("budget_micro_usd", "budget_tokens"):
            v = getattr(self, k)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ValueError(f"{k}: must be a non-negative integer or null")
        for k in ("spent_micro_usd", "spent_tokens", "version"):
            v = getattr(self, k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{k}: must be a non-negative integer")

    @property
    def active(self) -> bool:
        return self.status == "active"

    def over_budget(self, extra_tokens: int = 0, extra_micro_usd: int = 0) -> str:
        """Reason string if spending this much more would exceed a cap, else ''."""
        if self.budget_tokens is not None and self.spent_tokens + extra_tokens > self.budget_tokens:
            return f"token budget exceeded ({self.spent_tokens + extra_tokens} > {self.budget_tokens})"
        if self.budget_micro_usd is not None and self.spent_micro_usd + extra_micro_usd > self.budget_micro_usd:
            return (f"USD budget exceeded ({(self.spent_micro_usd + extra_micro_usd) / 1e6:.6f} > "
                    f"{self.budget_micro_usd / 1e6:.6f})")
        return ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["allowed_tools"], d["allowed_models"] = list(self.allowed_tools), list(self.allowed_models)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        if not isinstance(d, dict):
            raise ValueError("job record must be an object")
        extra = set(d) - {f for f in cls.__dataclass_fields__}
        if extra:
            raise ValueError(f"job record: unknown fields {sorted(extra)}")
        d = dict(d)
        for k in ("allowed_tools", "allowed_models"):
            if not isinstance(d.get(k, []), list):
                raise ValueError(f"{k}: must be a list")
            d[k] = tuple(d.get(k, ()))
        return cls(**d)


class JobRegistry:
    """JSON-file registry: ``{"schema": 1, "jobs": {job_id: Job}}``. Reads never create the file: a missing,
    unreadable or corrupt registry raises RegistryUnavailable so the gate can fail closed."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.reads = 0  # observable for cache tests

    # ---------- storage ----------
    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not self.path.exists():
                self._write({})

    @contextlib.contextmanager
    def _locked(self):
        try:
            lf = open(self.path.with_name(self.path.name + ".lock"), "a")
        except OSError as e:
            raise RegistryUnavailable(f"registry lock unavailable: {e}") from e
        with lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _read(self) -> dict:
        self.reads += 1
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise RegistryUnavailable(f"registry not found: {self.path.name}") from e
        except (OSError, ValueError) as e:
            raise RegistryUnavailable(f"registry unreadable or corrupt: {type(e).__name__}") from e
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("jobs"), dict):
            raise RegistryUnavailable("registry has an unexpected format")
        try:
            jobs = {k: Job.from_dict(v) for k, v in raw["jobs"].items()}
        except (TypeError, ValueError) as e:
            raise RegistryUnavailable(f"registry has an invalid job record: {e}") from e
        if any(k != j.job_id for k, j in jobs.items()):
            raise RegistryUnavailable("registry key does not match job_id")
        return jobs

    def _write(self, jobs: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = json.dumps({"schema": SCHEMA, "jobs": {k: j.to_dict() for k, j in sorted(jobs.items())}},
                          indent=2, sort_keys=True)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body + "\n"); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            raise RegistryUnavailable(f"registry write failed: {e}") from e

    def _update(self, job_id: str, fn: Callable[[Job], Job]) -> Job:
        with self._locked():
            jobs = self._read()
            if job_id not in jobs:
                raise KeyError(job_id)
            old = jobs[job_id]
            new = replace(fn(old), version=old.version + 1, updated_at=_now_iso(self.clock()))
            jobs[job_id] = new
            self._write(jobs)
            return new

    # ---------- API ----------
    def get(self, job_id: str) -> Job | None:
        return self._read().get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._read().values(), key=lambda j: j.job_id)

    def register(self, job_id: str, owner: str, allowed_tools=(), allowed_models=(), *,
                 budget_usd: float | None = None, budget_tokens: int | None = None) -> Job:
        now = _now_iso(self.clock())
        job = Job(job_id, owner, tuple(allowed_tools), tuple(allowed_models), usd_to_micro(budget_usd),
                  budget_tokens, "active", created_at=now, updated_at=now)
        with self._locked():
            jobs = self._read()
            if job_id in jobs:  # DynamoDB: attribute_not_exists(job_id)
                raise RegistryConflict(f"job {job_id} already registered")
            jobs[job_id] = job
            self._write(jobs)
        return job

    def set_status(self, job_id: str, status: str, reason: str = "") -> Job:
        if status not in JOB_STATUSES:
            raise ValueError(f"status: must be one of {JOB_STATUSES}")
        return self._update(job_id, lambda j: replace(j, status=status, stop_reason=reason if status == "stopped" else ""))

    def charge(self, job_id: str, tokens: int, micro_usd: int) -> Job:
        """Conditional reservation: succeeds only if the job is active and stays within budget (fresh read)."""
        def fn(j: Job) -> Job:
            if not j.active:
                raise RegistryConflict(f"job {job_id} is stopped: {j.stop_reason or 'no reason given'}")
            over = j.over_budget(tokens, micro_usd)
            if over:
                raise RegistryConflict(over)
            return replace(j, spent_tokens=j.spent_tokens + tokens, spent_micro_usd=j.spent_micro_usd + micro_usd)
        return self._update(job_id, fn)

    def adjust(self, job_id: str, tokens: int, micro_usd: int) -> Job:
        """Unconditional settle after a call: replace the reservation with actual usage (deltas may be negative)."""
        return self._update(job_id, lambda j: replace(j, spent_tokens=max(0, j.spent_tokens + tokens),
                                                       spent_micro_usd=max(0, j.spent_micro_usd + micro_usd)))


class AuditLog:
    """Append-only JSONL audit trail of every control decision and usage record (fsync'd, torn tail ignored)."""
    FIELDS = ("ts", "event", "job_id", "run_id", "agent", "kind", "model", "tool", "decision", "reason",
              "tokens_in", "tokens_out", "tokens", "cost_usd")

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    def write(self, event: str, **fields) -> dict:
        rec = {k: None for k in self.FIELDS}
        rec.update(fields, ts=_now_iso(self.clock()), event=event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
        return rec

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def cost_summary(entries: list[dict]) -> dict:
    """Cost per job from audit entries: allowed/refused decisions, calls, tokens and USD."""
    out: dict = {}
    for e in entries:
        jid = e.get("job_id") or "(none)"
        s = out.setdefault(jid, {"allowed": 0, "refused": 0, "model_calls": 0, "tool_calls": 0, "errors": 0,
                                 "tokens": 0, "cost_usd": 0.0})
        if e.get("event") == "decision":
            s["allowed" if e.get("decision") == "allow" else "refused"] += 1
        elif e.get("event") == "usage":
            s["model_calls" if e.get("kind") == "model" else "tool_calls"] += 1
            s["tokens"] += int(e.get("tokens") or 0)
            s["cost_usd"] = round(s["cost_usd"] + float(e.get("cost_usd") or 0.0), 8)
        elif e.get("event") == "error":
            s["errors"] += 1
    return out


@dataclass(frozen=True)
class AutoStopRules:
    on_budget_overrun: bool = True
    on_disallowed_tool: bool = True
    max_consecutive_errors: int | None = 3  # None disables the rule


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    reason: str
    job_id: str | None
    kind: str
    name: str
    reserved_tokens: int = 0
    reserved_micro_usd: int = 0


@dataclass
class _CacheEntry:
    job: Job
    fetched_at: float


class ControlGate:
    """The one checkpoint for every model and tool call of a registered job. Refuses on any doubt."""

    def __init__(self, registry: JobRegistry, audit: AuditLog, *, ttl_s: float = DEFAULT_TTL_S,
                 clock: Callable[[], float] = time.monotonic, rules: AutoStopRules = AutoStopRules()):
        self.registry, self.audit, self.clock, self.rules = registry, audit, clock, rules
        self.ttl_s = validate_ttl(ttl_s)
        self._cache: dict[str, _CacheEntry] = {}
        self._errors: dict[str, int] = {}

    # ---------- registry reads (cached) ----------
    def _job(self, job_id: str) -> Job | None:
        hit = self._cache.get(job_id)
        now = self.clock()
        if hit is not None and now - hit.fetched_at < self.ttl_s:
            return hit.job
        self._cache.pop(job_id, None)  # never serve an expired entry, even if the registry is down
        job = self.registry.get(job_id)  # RegistryUnavailable propagates -> refuse
        if job is not None:
            self._cache[job_id] = _CacheEntry(job, now)
        return job

    def invalidate(self, job_id: str | None = None) -> None:
        self._cache.clear() if job_id is None else self._cache.pop(job_id, None)

    # ---------- decisions ----------
    def _decide(self, allowed: bool, reason: str, job_id, agent, kind, name, run_id=None, **extra) -> ControlDecision:
        d = ControlDecision(allowed, reason, job_id, kind, name, extra.pop("reserved_tokens", 0),
                            extra.pop("reserved_micro_usd", 0))
        try:
            self.audit.write("decision", job_id=job_id, run_id=run_id, agent=agent, kind=kind,
                             model=name if kind == "model" else None, tool=name if kind == "tool" else None,
                             decision="allow" if allowed else "refuse", reason=reason, **extra)
        except OSError as e:
            if allowed:  # an allow we cannot record is refused (fail closed); the reservation stays charged
                return replace(d, allowed=False, reason=f"audit log unavailable (fail closed): {type(e).__name__}")
        return d

    def _precheck(self, job_id, agent, kind, name, run_id, allow_list_attr: str):
        """Returns (job, None) when the cached status allows the call, else (None, refusal)."""
        if not job_id:
            return None, self._decide(False, "no job_id: every call must belong to a registered job", job_id, agent,
                                      kind, name, run_id)
        try:
            job = self._job(job_id)
        except RegistryUnavailable as e:
            return None, self._decide(False, f"registry unavailable (fail closed): {e}", job_id, agent, kind, name, run_id)
        if job is None:
            return None, self._decide(False, f"job {job_id} is not registered", job_id, agent, kind, name, run_id)
        if not job.active:
            return None, self._decide(False, f"job {job_id} is stopped: {job.stop_reason or 'no reason given'}",
                                      job_id, agent, kind, name, run_id)
        if name not in getattr(job, allow_list_attr):
            reason = f"{kind} {name!r} is not on the job's allow-list"
            if self.rules.on_disallowed_tool:
                reason += self._auto_stop(job_id, f"auto: {kind} {name!r} not on allow-list (agent {agent})")
            return None, self._decide(False, reason, job_id, agent, kind, name, run_id)
        return job, None

    def authorize_tool(self, job_id: str | None, agent: str, tool: str, run_id: str | None = None) -> ControlDecision:
        job, refused = self._precheck(job_id, agent, "tool", tool, run_id, "allowed_tools")
        if refused:
            return refused
        over = job.over_budget()  # tools are free here; still refuse once a cap is already exceeded
        if over:
            return self._decide(False, over, job_id, agent, "tool", tool, run_id)
        return self._decide(True, "job active, tool on allow-list", job_id, agent, "tool", tool, run_id)

    def authorize_model(self, job_id: str | None, agent: str, model: str, est_tokens: int, est_cost_usd: float,
                        run_id: str | None = None) -> ControlDecision:
        job, refused = self._precheck(job_id, agent, "model", model, run_id, "allowed_models")
        if refused:
            return refused
        micro = usd_to_micro(est_cost_usd)
        try:
            self.registry.charge(job_id, est_tokens, micro)  # conditional write: active AND within budget
        except RegistryUnavailable as e:
            return self._decide(False, f"registry unavailable (fail closed): {e}", job_id, agent, "model", model, run_id)
        except (RegistryConflict, KeyError) as e:
            reason = str(e)
            if self.rules.on_budget_overrun and "budget exceeded" in reason:
                reason += self._auto_stop(job_id, f"auto: {reason}")
            else:
                self.invalidate(job_id)
            return self._decide(False, reason, job_id, agent, "model", model, run_id,
                                tokens=est_tokens, cost_usd=est_cost_usd)
        return self._decide(True, "job active, model on allow-list, budget reserved", job_id, agent, "model", model,
                            run_id, tokens=est_tokens, cost_usd=est_cost_usd, reserved_tokens=est_tokens,
                            reserved_micro_usd=micro)

    # ---------- after the call ----------
    def record_usage(self, d: ControlDecision, agent: str, *, tokens_in: int = 0, tokens_out: int = 0,
                     cost_usd: float = 0.0, run_id: str | None = None) -> None:
        tokens = tokens_in + tokens_out
        if d.kind == "model":
            try:
                self.registry.adjust(d.job_id, tokens - d.reserved_tokens, usd_to_micro(cost_usd) - d.reserved_micro_usd)
            except (RegistryUnavailable, KeyError):
                pass  # the reservation (worst case) stays charged: over-counting is the safe direction
        self._errors[d.job_id] = 0
        self.audit.write("usage", job_id=d.job_id, run_id=run_id, agent=agent, kind=d.kind,
                         model=d.name if d.kind == "model" else None, tool=d.name if d.kind == "tool" else None,
                         decision="allow", reason="call completed", tokens_in=tokens_in, tokens_out=tokens_out,
                         tokens=tokens, cost_usd=round(cost_usd, 8))

    def record_error(self, job_id: str | None, agent: str, kind: str, name: str, error: str,
                     run_id: str | None = None) -> None:
        if not job_id:
            return
        n = self._errors.get(job_id, 0) + 1
        self._errors[job_id] = n
        reason = f"error {n}: {error}"
        limit = self.rules.max_consecutive_errors
        if limit is not None and n >= limit:
            reason += self._auto_stop(job_id, f"auto: {n} consecutive errors (last: {error})")
        self.audit.write("error", job_id=job_id, run_id=run_id, agent=agent, kind=kind,
                         model=name if kind == "model" else None, tool=name if kind == "tool" else None,
                         reason=reason)

    # ---------- kill switch ----------
    def stop(self, job_id: str, reason: str, by: str = "operator") -> Job:
        job = self.registry.set_status(job_id, "stopped", reason)
        self.invalidate(job_id)
        self.audit.write("stop", job_id=job_id, agent=by, reason=reason)
        return job

    def _auto_stop(self, job_id: str, reason: str) -> str:
        try:
            self.stop(job_id, reason, by="auto-stop-rule")
            return "; job stopped automatically"
        except (RegistryUnavailable, KeyError) as e:
            self.invalidate(job_id)
            return f"; automatic stop failed ({type(e).__name__}), call refused anyway"
