# Roadmap

Rule: one complete, tested workflow before breadth. Phases 0-3 are done; everything after is **planned**.

## Done
- **Phase 0 (2026-10-16):** scaffold, secret-scan test, design review of the earlier prototype (`design-lineage.md`).
- **Phase 1 (2026-10-17, M1):** orchestrator → researcher + writer (mocks); default-deny gate; validated outputs;
  JSONL log; retries with backoff; demo CLI; tests for allow, deny, forged approval, malformed output.
- **Phase 2 (M2):** HMAC single-use expiring approvals with the key from env/.env; `approve` and `run` CLIs;
  checkpoint resume with idempotency keys; tests for replay, expiry, tamper, concurrency, resume.
- **Phase 3 (M3):** ported intake policy and agent scope; offline runner; scripts and Makefile; CI workflow file.
- **Demo recording:** `docs/media/demo.gif` (+ asciicast) rendered from real CLI runs by `scripts/record_demo.py`.

## Next (planned)
1. **Tamper-evident log:** hash-chain each event (`prev_sha256`) and add a `verify` command.
2. **Pluggable LLM agent:** a `LLMWriter` behind the same `AgentOutput` validation. The mock stays the default.
   Live calls are themselves a gated action (request-bound approval + token cap). Recorded-response tests keep CI offline.
3. **Off-host approval signer:** Ed25519, with the public key pinned in config and the private key on another device. HMAC stays as a dev mode.
4. **Packaging:** `pyproject.toml`, `jarvis` console entry points, a type check (mypy) and a linter in CI.

## AWS deployment (design first; deploy only with explicit approval and a budget alarm)
- **Shape:** API Gateway (HTTP API, IAM/JWT auth) → Lambda `submit` → Step Functions Standard workflow
  (research → write → `WaitForTaskToken` for human approval → deliver). Each agent is a Lambda.
- **State:** DynamoDB for tasks, run events (or S3 JSONL) and approvals. Single-use approval is a conditional
  write (`attribute_not_exists(nonce)`). Idempotency keys use a conditional put.
- **Approval signer:** a KMS asymmetric key (ECC_NIST_P256). Only the approver's role has `kms:Sign`; agent roles get
  `kms:Verify` only. This removes the same-user weakness of HMAC.
- **Audit:** S3 with Object Lock (governance mode) plus CloudWatch Logs with 14-day retention.
- **Delivery:** SES in sandbox mode first (verified recipients only).
- **Cost notes:** at portfolio traffic (≤ 1k runs/month), Lambda, Step Functions, DynamoDB on-demand and API
  Gateway are expected to be a few USD at most, partly in free tier. A KMS key is ~USD 1/month. Avoid a VPC/NAT
  Gateway (~USD 30+/month) because nothing needs it. LLM tokens are the main variable cost: cap them per request
  and set an AWS Budget alarm (e.g. USD 5) **before** the first deploy.
- **Security notes:** least-privilege IAM per Lambda; no long-lived keys (GitHub OIDC for CI deploys); secrets in
  Secrets Manager/SSM; throttling on the API; IaC (SAM/CDK) reviewed and `cdk diff`/`sam validate` run before
  deploy; separate dev account; tear-down script.
