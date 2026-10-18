# Design lineage

This project is a clean, public rebuild of patterns first explored in an earlier **private personal prototype**: a
multi-agent assistant where an orchestrator delegated work to specialist agents. The prototype was tied to
personal accounts and data, so **no data, logs, prompts, credentials or configuration were copied**. Most ideas
were reimplemented from scratch; two small generic modules were adapted (marked "ported" below). Everything here
uses synthetic data and mocks only.

## What carried over (as design, then re-implemented and tested here)
| Idea from the prototype | Here | How it was treated |
|---|---|---|
| A single policy enforcement point; default deny; unknown actions are consequential; approval-like fields in task data count as spoofing | `gate.py` | rebuilt |
| Human approvals bound to a request hash (action, params, run and task), expiring, single-use, signed | `approval_store.py` | rebuilt (generic, HMAC; asymmetric signing is planned) |
| A task-text classifier that treats text as data and normalises obfuscation (homoglyphs, leetspeak, spacing) | `policy.py` | ported, generalised, personal/channel-specific strings removed |
| Per-agent folder isolation and forbidden actions | `scope.py` | ported, deployment-specific paths and config loading removed |
| Running tests with the network blocked | `scripts/run_offline.sh` | rebuilt (netns + Python socket guard; the prototype's seccomp layer was not carried over) |
| Structured hand-offs between agents | `models.py` | rebuilt as strict validated records |

## What was deliberately left out
Account integrations (mail, calendar, events), personal triage rules, agent personas/prompts, operational logs and
any real data. These have no place in a public repository. Where a demo needs them, mocks and a synthetic corpus are
used instead.

## Lessons that shaped this rebuild
- **Build one end-to-end workflow first.** The prototype grew in breadth before it had a single, documented,
  testable flow. This rebuild started from one workflow (research → write → gated delivery) with crash recovery.
- **Decouple from the environment.** The prototype hard-coded host paths. Here, every path is injected (workdir,
  corpus, agents root), so tests run anywhere in a temp dir.
- **Make limits explicit.** Each control documents what it does *not* protect against (see `security.md`).
