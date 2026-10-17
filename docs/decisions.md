# Decisions (ADR log)

1. **Clean rebuild, not a fork of the prototype.** The earlier private prototype was tied to personal accounts and
   data. This repo reimplements its design (adapting only two small generic modules), so nothing private leaks and the code reads on its own
   (see `design-lineage.md`).
2. **Single-purpose repository.** It holds only this project, with synthetic data, mocks and its own CI.
3. **Stdlib only, unittest.** Zero install and fully offline.
4. **Default deny with an explicit allow-list**, not a deny-list: a deny-list misses novel actions.
5. **Rebuild > copy.** Only small, generic, well-tested logic was ported (intake policy, scope). Deployment-specific paths, names
   and channels were removed (a test asserts this for `policy.py`).
6. **Synthetic data only**, enforced by a secret/private-data scan test.
7. **Phase 1 rework (2026-10-17).** The first prototype of the workflow had one agent, no retries or
   idempotency, and a demo key in source. It was replaced: the key now comes from env/.env with no default, there
   are two agents, validated outputs, retries, and checkpoint/idempotency.
8. **The approval covers the final content.** Delivery params include the generated draft as `body`, so the human
   approves exactly what will be sent. The cost: the approval can only be issued after the draft exists, hence the
   blocked → approve → resume flow.
9. **The run log is the checkpoint.** A single append-only JSONL per run serves as both audit trail and recovery
   state. That is simpler than a separate DB, and each line is fsync'd.
10. **The approval is consumed before the side effect.** A crash after consuming can waste an approval but can
    never double-spend one. A crash after the effect but before the commit log is recovered from the outbox using
    the idempotency key, without needing a new approval.
11. **Permanent/unknown errors are not retried.** Only `TransientToolError` and malformed output are retried, with
    exponential backoff, so a bug is never hammered or masked.
12. **Mocks, not an LLM, for now.** Deterministic agents make every safety property testable. An LLM backend is
    planned behind the same validation and gate.
13. **Offline runner** prefers `unshare -rn` (true netns) and falls back to a Python socket guard. It reports which
    layers are active.
