# Setup and run guide

Everything here runs offline on mock agents, mock tools and a synthetic corpus. Nothing needs an account, an API
key or network access (the test and demo wrappers block the network on purpose).

## 1. Prerequisites
- Python 3.10 or newer (`python3 --version`). Standard library only: there is nothing to `pip install`.
- Git and a POSIX shell (Linux or macOS). On Windows, use WSL.
- Optional: `unshare` (util-linux, Linux only). With it, tests run in an empty network namespace. Without it, the
  scripts fall back to a Python socket guard and print `layers: python socket guard only` on stderr.
- Optional: `make`, for the `make test` and `make demo` shortcuts.

## 2. Get the code
```bash
git clone https://github.com/antequeradiogo-stack/jarvis-agentic-workforce.git
cd jarvis-agentic-workforce
```

## 3. Run the tests
```bash
./scripts/test.sh -v      # full suite, network-blocked (same as: make test)
```
The last lines should read `Ran 83 tests` and `OK`. To run without the offline wrapper:
`PYTHONPATH=src python3 -m unittest discover -s tests` (same as `make test-fast`).

## 4. Run the demo
```bash
./scripts/demo.sh                      # every scenario, with a readable trace
./scripts/demo.sh --scenario approve   # one scenario: research | blocked | approve | forged | faults | crash | injection
```
The demo uses temporary folders and leaves nothing behind.

## 5. Human-in-the-loop flow with saved state
This is the real CLI path: a run stops at the gate, a human approves the exact request, and the run resumes from its
checkpoint.
```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print('hex:' + secrets.token_hex(32))"   # paste the output after JARVIS_APPROVAL_KEY= in .env
export PYTHONPATH=src
python3 -m jarvis_workforce.run     --workdir ./var --task examples/tasks/research_and_email.json --no-json   # blocked, exit 10
python3 -m jarvis_workforce.approve --workdir ./var --run demo-email-001          # dry run: shows the exact request
python3 -m jarvis_workforce.approve --workdir ./var --run demo-email-001 --yes    # approve it (single-use, 10 min)
python3 -m jarvis_workforce.run     --workdir ./var --task examples/tasks/research_and_email.json --no-json   # resumes, exit 0
```
The `.env` file must have mode `0600`, or the key is refused. With no key at all, the run still works but every
consequential action is denied (fail closed), so it stays blocked.
`examples/tasks/research_only.json` needs no approval and completes in one run.

### Exit codes
| Command | Code | Meaning |
|---|---|---|
| `run` | 0 | completed |
| `run` | 10 | blocked: an approval is required (resume after `approve --yes`) |
| `run` | 1 | failed |
| `run` | 2 | rejected at intake (invalid or unsafe task) |
| `run`, `approve` | 3 | refusing: the approval key is malformed or too short, or `.env` is readable by group/others |
| `approve` | 2 | no such run, no pending request, or the request is not bound to this run and task |

## 6. Reset local state
Run logs, approvals and the mock outbox live under the `--workdir` folder. `./var/`, `runs/`, `approvals/`,
`mock_outbox/` and `.env` are gitignored. To start over: `rm -rf ./var`.

## Troubleshooting
- `ModuleNotFoundError: jarvis_workforce`: set `export PYTHONPATH=src`, or use the `scripts/` wrappers, which set it.
- `refusing: ...` from `run` or `approve`: check that `.env` has mode `0600` and holds a `hex:` key of 32 bytes
  (64 hex characters), generated with the command above.
- A run stays blocked after approving: approvals expire after 10 minutes and are single-use. Approve again, then
  re-run.
