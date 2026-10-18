"""File-backed approval store: single-use, expiring, request-bound, HMAC-SHA256-signed approvals.

Layout under ``root``:
  pending/<nonce>.json   issued, not yet used
  consumed/<nonce>.json  used (moved here atomically)
  used_nonces.txt        append-only, fsync'd; checked on every consume (replay protection)
  .lock                  fcntl lock serialising consume()

An approval is bound to sha256(canonical(action, params, run_id, task_id)), so changing any parameter,
or presenting it for a different run or task, invalidates it. It is single-use.
consume() fails closed on: bad JSON, missing/extra fields, wrong version, bad signature (tamper or wrong key),
a different run or task, expired / issued in the future / TTL above max, nonce already used.

HONEST LIMIT: HMAC is symmetric. Any process that can read the key can mint approvals, so this stops accidental
and automated actions and leaves an audit trail; it is not a boundary against a malicious process running as the
same user. A real boundary needs the signer elsewhere (off-host Ed25519 key, or a KMS asymmetric key) - planned.
"""
from __future__ import annotations
import fcntl, hashlib, hmac, json, os, secrets, time
from contextlib import contextmanager
from pathlib import Path

from .gate import request_sha256

VERSION = 2
FIELDS = ("v", "action", "run_id", "task_id", "request_sha256", "nonce", "issued_at", "expires_at", "approver")
MAX_TTL = 3600
MIN_KEY_BYTES = 32


class ApprovalError(Exception):
    pass


def sign(key: bytes, body: dict) -> str:
    msg = json.dumps({k: body[k] for k in FIELDS}, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class ApprovalStore:
    def __init__(self, root: Path | str, key: bytes, max_ttl: int = MAX_TTL):
        if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
            raise ApprovalError(f"approval key must be >= {MIN_KEY_BYTES} bytes")
        self.root = Path(root)
        self._key = bytes(key)
        self.max_ttl = max_ttl
        self.pending = self.root / "pending"
        self.consumed = self.root / "consumed"
        for d in (self.pending, self.consumed):
            d.mkdir(parents=True, exist_ok=True)
        self._used = self.root / "used_nonces.txt"
        self._used.touch(exist_ok=True)

    # ---------- issuing (the human-approval step; see approve.py CLI) ----------
    def issue(self, action: str, params: dict, ttl: int = 600, approver: str = "user", now: float | None = None, *,
              run_id: str | None = None, task_id: str | None = None) -> dict:
        problem = _identity_problem(run_id, task_id)
        if problem:
            raise ApprovalError(problem)
        if not 0 < ttl <= self.max_ttl:
            raise ApprovalError(f"ttl must be in (0, {self.max_ttl}]")
        t = time.time() if now is None else now
        body = {"v": VERSION, "action": action, "run_id": run_id, "task_id": task_id,
                "request_sha256": request_sha256(action, params, run_id=run_id, task_id=task_id),
                "nonce": secrets.token_hex(16), "issued_at": t, "expires_at": t + ttl, "approver": approver}
        body["sig"] = sign(self._key, body)
        tmp = self.pending / f".{body['nonce']}.tmp"
        tmp.write_text(json.dumps(body, sort_keys=True))
        os.replace(tmp, self.pending / f"{body['nonce']}.json")
        return body

    # ---------- verification ----------
    def _verify(self, body: dict, req_sha: str, now: float, run_id: str, task_id: str) -> str | None:
        """Return None if valid for this request, else the reason."""
        if not isinstance(body, dict) or set(body) != set(FIELDS) | {"sig"}:
            return "malformed approval (fields)"
        if body["v"] != VERSION:
            return "unsupported approval version"
        if not isinstance(body["sig"], str) or not hmac.compare_digest(sign(self._key, body), body["sig"]):
            return "bad signature (tampered or wrong key)"
        if not isinstance(body["run_id"], str) or not isinstance(body["task_id"], str):
            return "malformed approval (identity)"
        if body["run_id"] != run_id or body["task_id"] != task_id:
            return "approval bound to a different run or task"
        if body["request_sha256"] != req_sha:
            return "approval bound to a different request"
        if not all(isinstance(body[k], (int, float)) for k in ("issued_at", "expires_at")):
            return "malformed timestamps"
        if body["issued_at"] > now + 5:
            return "approval issued in the future"
        if body["expires_at"] - body["issued_at"] > self.max_ttl:
            return "approval ttl exceeds maximum"
        if now >= body["expires_at"]:
            return "approval expired"
        if body["nonce"] in self._used_nonces():
            return "approval already used (replay)"
        return None

    def _used_nonces(self) -> set:
        return set(self._used.read_text().split())

    @contextmanager
    def _locked(self):
        with open(self.root / ".lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def consume(self, action: str, params: dict, now: float | None = None, *,
                run_id: str | None = None, task_id: str | None = None) -> tuple[bool, str]:
        """Find and atomically consume a valid approval for exactly this run, task and request. Returns (ok, reason)."""
        problem = _identity_problem(run_id, task_id)
        if problem:
            return False, problem
        t = time.time() if now is None else now
        req = request_sha256(action, params, run_id=run_id, task_id=task_id)
        reasons = []
        with self._locked():
            for p in sorted(self.pending.glob("*.json")):
                try:
                    body = json.loads(p.read_text())
                except (OSError, ValueError):
                    reasons.append("unreadable approval file")
                    continue
                if isinstance(body, dict) and body.get("request_sha256") != req:
                    continue  # approval for some other request: not relevant, and not consumed
                why = self._verify(body, req, t, run_id, task_id)
                if why:
                    reasons.append(why)
                    continue
                # mark used BEFORE the side effect can happen: a crash can never allow a second use
                with open(self._used, "a") as f:
                    f.write(body["nonce"] + "\n"); f.flush(); os.fsync(f.fileno())
                os.replace(p, self.consumed / p.name)
                return True, f"approval {body['nonce'][:8]} by {body['approver']} consumed"
        return False, ("; ".join(sorted(set(reasons))) if reasons else "no approval for this exact request")


# ---------- key management ----------
KEY_ENV = "JARVIS_APPROVAL_KEY"


def _identity_problem(run_id, task_id) -> str | None:
    if isinstance(run_id, str) and run_id.strip() and isinstance(task_id, str) and task_id.strip():
        return None
    return "approval must be bound to a non-empty run_id and task_id"


def _decode_key(v: str) -> bytes:
    v = v.strip()
    if v.startswith("hex:"):
        try:
            key = bytes.fromhex(v[4:])
        except ValueError:
            raise ApprovalError(f"{KEY_ENV} hex value is not valid hexadecimal") from None
    else:
        key = v.encode()
    if len(key) < MIN_KEY_BYTES:
        raise ApprovalError(f"{KEY_ENV} must be >= {MIN_KEY_BYTES} bytes")
    return key


def load_key(project_root: Path | str | None = None, environ: dict | None = None) -> bytes:
    """Approval HMAC key from the environment, else from <project_root>/.env (must be mode 0600).

    Value is raw text, or 'hex:<hex>'. Raises KeyError if no key is configured (callers fail closed or, for the
    demo only, use an ephemeral random key). The key is never logged and .env is git-ignored."""
    env = os.environ if environ is None else environ
    if env.get(KEY_ENV):
        return _decode_key(env[KEY_ENV])
    if project_root is not None:
        p = Path(project_root) / ".env"
        if p.is_file():
            if p.stat().st_mode & 0o077:
                raise ApprovalError(".env is readable by group/others; run: chmod 600 .env")
            for line in p.read_text().splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() == KEY_ENV:
                    return _decode_key(v.strip().strip("'\""))
    raise KeyError(f"{KEY_ENV} not set (environment or .env)")
