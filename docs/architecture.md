# Architecture

All of this is implemented and covered by tests unless marked **[planned]**.

## Flow
1. **Intake.** `TaskRequest.from_dict` is strict: exact fields, `task_id` pattern, `kind` allow-list, optional
   structured `deliver` (`send_email` | `publish` | `purchase`). Then `policy.intake_check(goal)` rejects goals
   that are off the safe allow-list or that contain consequential or injection patterns. A rejected task never
   reaches an agent.
2. **Steps.** The orchestrator runs `research` (MockResearcher), then `write` (MockWriter). An agent can only act
   through a `ToolContext`, which applies its scope's forbidden actions, then the gate, then logs the call.
3. **Validation.** Raw agent output is untrusted. `AgentOutput.validate` checks exact keys, the expected agent and
   kind, non-empty bounded content and a list of source strings.
4. **Retries.** Malformed output and `TransientToolError` are retried up to `max_retries` (default 3) with
   backoff `base * 2^(n-1)`. Permanent or unexpected errors fail fast: unknown failures are never retried blindly.
   A denied tool call blocks the step.
5. **Delivery.** The consequential call's params are the task's `deliver.params` plus `body = draft`. The human
   therefore approves the exact final content after seeing the draft. The flow is: idempotency checks, then the
   agent's forbidden list, then the gate. On deny, the log records `approval_required` (with the full params and
   hash) and the run ends as **blocked**. A blocked run is resumable.
6. **Result.** `Result{status, kind, summary, artifacts, blocked_actions, effects, evidence, reason}`. `kind` is
   honest: `draft` when nothing was delivered, `simulated` for mock effects. `completed_real` requires evidence and
   is never produced by mocks.

## Gate (PEP)
Order of checks: approval-like keys anywhere in params → deny (spoofing). Then the safe allow-list (`list_docs`,
`read_doc`, `summarize`) → allow. Anything else → consume a matching approval or deny. Unknown actions are
labelled `unregistered:` and denied unless an approval exists for that exact request.

## Run log = audit trail + checkpoint
`runs/<run_id>/events.jsonl`, one fsync'd JSON object per line (`seq`, `ts`, `run_id`, `event`, fields). A torn last
line is ignored. On restart:
- `step_completed` → the step is skipped and its stored output is re-validated and reused
- `effect_committed` → the side effect is skipped
- effect present in the mock outbox but not committed (crash between execution and logging) → adopted as
  `recovered`; it is **not** re-sent and needs no new approval
- `run_finished` with a non-blocked status → the stored result is returned (idempotent)
- the same `run_id` with a different task → refused, so checkpoints are never mixed

Idempotency key = `sha256(run_id | "deliver" | request_sha256)[:32]`. The mock outbox dedupes on it, modelling an
external API that supports idempotency keys.

## Components
| Module | Responsibility |
|---|---|
| `models.py` | TaskRequest, ToolCall, AgentOutput, Result + ValidationError |
| `gate.py` | PEP, canonical request hash, approval-claim scanner |
| `approval_store.py` | issue/consume approvals, `load_key` |
| `orchestrator.py` | steps, ToolContext, retries, delivery, checkpoint logic, fault injection (`crash_at`) |
| `agents.py` | MockResearcher, MockWriter, ScriptedAgent (scripted faults for tests/demo) |
| `tools.py` | mock tools, extractive `summarize`, mock outbox with idempotency |
| `runlog.py` | append-only JSONL log |
| `policy.py`, `scope.py` | ported intake classifier, per-agent isolation |
| `demo.py`, `run.py`, `approve.py` | CLIs |

**[planned]** Pluggable LLM agents (the mock stays the default), a task queue/API, and an AWS deployment (see roadmap).
