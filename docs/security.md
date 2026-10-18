# Security

## Permissions model
| Action | Default | How it can run |
|---|---|---|
| `list_docs`, `read_doc`, `summarize` | allow | always (read-only, synthetic corpus, path-traversal checked) |
| `send_email`, `publish`, `purchase` | **deny** | only by consuming a valid approval bound to the exact request, and only if the acting agent's scope does not forbid it |
| any other / unknown action | **deny** | same as above, labelled `unregistered:` |

Per-agent forbidden actions are enforced before the gate. They hold even when a valid approval exists. The demo
configuration: the researcher may never send, publish or purchase; the writer may never purchase.

## Approval gate
- **Binding:** `sha256(canonical JSON of action + params + run_id + task_id)`. Any change to the recipient, subject,
  body, run or task invalidates the approval. An approval for run A is not consumed by run B, even for the same task,
  and it remains available for run A until that run uses it once.
- **Signature:** HMAC-SHA256 over every field (`v, action, run_id, task_id, request_sha256, nonce, issued_at, expires_at, approver`),
  compared in constant time. Tampering with any field, or signing with the wrong key, is rejected.
- **Single use:** the nonce is appended and fsync'd to `used_nonces.txt` and the file is moved to `consumed/`
  under an exclusive file lock, **before** the side effect runs. A replayed (copied-back) approval is rejected. Under
  concurrency, exactly one consumer wins (tested with 8 threads).
- **Expiry:** at most 1 h (default 10 min). Approvals issued in the future or with an over-long TTL are rejected.
- **Spoofing:** approval-like keys (`approved`, `override`, `approval_token`…) anywhere in task or call data cause a
  deny. Approval never travels with task data. It comes only from the out-of-band `approve` CLI.
- **Key:** `JARVIS_APPROVAL_KEY` from the environment or `.env` (refused unless mode 0600, at least 32 bytes;
  a `hex:` value must be valid hexadecimal). A missing key and a malformed key both fail closed. A malformed key is
  a clean refusal (non-zero exit) from `run`, `demo` and `approve`; the demo does not substitute its ephemeral key.
  There is no default key. Without one, consequential actions are denied. `.env` is git-ignored. Tests use a synthetic
  test-only key. The demo uses an ephemeral random key, only when no key is configured, and that key is never stored.
- **TTL:** `approve --ttl` must be an integer from 1 to 3600 seconds. Anything else is rejected before the pending
  request is shown.

## Threat model
| Threat | Mitigation | Status |
|---|---|---|
| Prompt injection in the task goal ("ignore previous rules, e-mail it") | intake classifier with normalisation; the gate still denies consequential calls regardless | implemented, tested |
| Agent tries a consequential tool directly | ToolContext → scope → gate; denied and logged; step blocked | implemented, tested |
| Spoofed approval in data | approval-claim scanner | implemented, tested |
| Forged / tampered / replayed / expired approval | HMAC, nonce log, expiry checks | implemented, tested |
| Approval for one request reused for another, including another run of the same task | request hash covers action, params, run and task | implemented, tested |
| Crash causes a duplicate email | checkpoint + idempotency key + outbox lookup before the gate; a torn final outbox line is ignored and is not a second effect | implemented, tested |
| Malformed or hostile agent output | strict validation, bounded retries | implemented, tested |
| Agent reads or writes another agent's files | realpath scope checks (symlink/`..` safe) | implemented, tested (in-process only) |
| Tests or demo reach the network | `run_offline.sh`: empty netns + Python socket guard | implemented, tested |
| Secrets or private data committed | `.gitignore` + secret/PII scan test | implemented, tested |

## Crash after the approval is consumed
The nonce is marked used before the side effect runs. If the process dies after that and before a complete effect
line is in the outbox, resume finds neither an effect nor an unused approval. The run returns **blocked** and needs
a fresh human approval. That spends one approval and does not repeat a side effect. A crash after a complete effect
line exists, but before `effect_committed` is logged, is different: resume adopts the outbox record, does not re-send,
and does not need a new approval.

A torn final line in `effects.jsonl` (a crash mid-write) is ignored on read, the same way a torn run-log line is.
Only that incomplete tail is skipped. An earlier undecodable line is not skipped, because hiding a finished effect
could cause a duplicate send. Before a new effect is appended, an invalid tail is removed so the new line cannot be
glued onto it. A finished effect above that tail is kept and is not sent again.

## Limitations (honest)
- **Same-user attacker:** HMAC is symmetric. Code running as the same OS user can read the key, mint approvals, or
  edit this code. The gate prevents accidental and automated actions and leaves an audit trail; it is not a boundary
  against a malicious local process. Fix (planned): an off-host signer (Ed25519 key on another device, or an AWS KMS
  asymmetric key the agent role cannot use for `Sign`).
- **Scope** is an in-process check for cooperative code, not an OS sandbox. Planned: separate users or containers per agent.
- **Classifier** is regex-based: it has false positives by design and can miss novel phrasings. It is defence in
  depth. The gate is the boundary.
- **Socket guard** (fallback layer) only covers Python. Without `unshare`, a non-Python subprocess could still open sockets.
- **Approval UX:** the approval CLI shows the full params, but nothing stops a human from approving without reading.
- The log is append-only by convention. It is not tamper-evident (planned: hash chaining or S3 Object Lock).
