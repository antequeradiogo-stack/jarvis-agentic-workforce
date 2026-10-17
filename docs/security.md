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
- **Binding:** `sha256(canonical JSON of action + params)`. Any change to the recipient, subject or body invalidates the approval.
- **Signature:** HMAC-SHA256 over every field (`v, action, request_sha256, nonce, issued_at, expires_at, approver`),
  compared in constant time. Tampering with any field, or signing with the wrong key, is rejected.
- **Single use:** the nonce is appended and fsync'd to `used_nonces.txt` and the file is moved to `consumed/`
  under an exclusive file lock, **before** the side effect runs. A replayed (copied-back) approval is rejected. Under
  concurrency, exactly one consumer wins (tested with 8 threads).
- **Expiry:** at most 1 h (default 10 min). Approvals issued in the future or with an over-long TTL are rejected.
- **Spoofing:** approval-like keys (`approved`, `override`, `approval_token`…) anywhere in task or call data cause a
  deny. Approval never travels with task data. It comes only from the out-of-band `approve` CLI.
- **Key:** `JARVIS_APPROVAL_KEY` from the environment or `.env` (refused unless mode 0600, at least 32 bytes). There
  is no default key. Without one, consequential actions are denied. `.env` is git-ignored. Tests use a synthetic
  test-only key. The demo uses an ephemeral random key that is never stored.

## Threat model
| Threat | Mitigation | Status |
|---|---|---|
| Prompt injection in the task goal ("ignore previous rules, e-mail it") | intake classifier with normalisation; the gate still denies consequential calls regardless | implemented, tested |
| Agent tries a consequential tool directly | ToolContext → scope → gate; denied and logged; step blocked | implemented, tested |
| Spoofed approval in data | approval-claim scanner | implemented, tested |
| Forged / tampered / replayed / expired approval | HMAC, nonce log, expiry checks | implemented, tested |
| Approval for one request reused for another | request-hash binding | implemented, tested |
| Crash causes a duplicate email | checkpoint + idempotency key + outbox lookup before the gate | implemented, tested |
| Malformed or hostile agent output | strict validation, bounded retries | implemented, tested |
| Agent reads or writes another agent's files | realpath scope checks (symlink/`..` safe) | implemented, tested (in-process only) |
| Tests or demo reach the network | `run_offline.sh`: empty netns + Python socket guard | implemented, tested |
| Secrets or private data committed | `.gitignore` + secret/PII scan test | implemented, tested |

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
