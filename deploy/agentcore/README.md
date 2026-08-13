# AgentCore Runtime deployment

Runbook for issue #12. **Nothing here has been deployed yet** — this documents the path and the artifacts that exist. No claim about a running deployment may be made until the evidence in the final step is committed from a real run.

## What is deployed

The same single agent and the same eight tools, behind the AgentCore Runtime HTTP contract:

| Requirement | How it is met |
|---|---|
| `linux/arm64` container | `deploy/agentcore/Dockerfile` (explicit `--platform`) |
| Listens on `0.0.0.0:8080` | `agentcore_app` default host/port |
| `GET /ping` health | Returns `{"status": "Healthy"}` |
| `POST /invocations` JSON | Actions `start`, `decide`, `status` |
| Session continuity | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header, validated against a strict allowlist before use as a path segment |

## How the approval boundary maps onto invocations

This is the honesty-critical part of #12, and it is documented rather than glossed:

A Strands interrupt cannot be held *inside* one request/response invocation while a human deliberates. It is held in persisted state **between two invocations of the same session**:

1. `{"action": "start"}` — runs the workflow to the interrupt, persists the immutable proposal and checkpoint, returns the proposal. Nothing beyond the sanctioned intake has mutated; revision stays 1.
2. `{"action": "decide", "decision": "approve"|"reject", "proposal_hash": "…"}` — reconstructs the session **in a fresh subprocess**, re-verifies the checkpoint, submits the official `interruptResponse`, and applies exactly the reviewed proposal once or refuses.

The process boundary is therefore real inside the container, not a figure of speech: `start_process_id` and `resume_process_id` differ in every response, and the same fail-closed gates that protect the local path protect this one.

## Ephemeral vs durable — do not overstate this

Session state (SQLite shop database, Strands session files, checkpoint, report) lives under a per-session directory on the container filesystem. AgentCore gives each session an isolated microVM, so that state survives across invocations **within a session** and dies with it.

It is **not** durable across sessions or beyond session expiry. This slice deliberately adds no external store, so the submission must not describe it as durable cloud persistence.

## Local verification before deploying

```bash
uv run pytest tests/test_agentcore_app.py -q     # 20 contract tests, no AWS needed

# Serve locally with the credential-free provider and drive it by hand:
uv run python -m production_orchestrator.agentcore_app --host 127.0.0.1 --port 8080 \
  --root /tmp/po-sessions

curl -s localhost:8080/ping
curl -s localhost:8080/invocations -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: session-0123456789abcdef' \
  -d '{"action":"start"}'
```

## Deploy

Requires the AWS profile with Bedrock access, an ECR repository, and an execution role permitted to invoke the model.

```bash
# 1. Build for arm64 (required — an amd64 image will not start)
docker buildx build --platform linux/arm64 \
  -f deploy/agentcore/Dockerfile -t production-orchestrator-agentcore:latest .

# 2. Push to ECR (substitute account/region; do not commit either)
aws ecr get-login-password --region <region> --profile <profile> \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker tag production-orchestrator-agentcore:latest \
  <account>.dkr.ecr.<region>.amazonaws.com/production-orchestrator-agentcore:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/production-orchestrator-agentcore:latest

# 3. Create the AgentCore runtime pointing at that image, with the judged
#    provider passed as container arguments:
#      --provider bedrock-workflow --model amazon.nova-lite-v1:0
#      --aws-region <region> --aws-credential-source container-role
#    The execution role supplies credentials; there is no named profile in
#    the container, which is exactly why credential-source is explicit.
```

Verify the current create/update commands against live AWS documentation before running them — the AgentCore control-plane surface is newer than this repository, and a stale command here should be corrected rather than trusted.

## Evidence to capture (nothing may be claimed without it)

- Invocation transcripts for a reject pair and an approve pair against the deployed endpoint
- Rejection: revision 1, zero applications. Approval: revision 2, exactly one application, hash matching the reviewed proposal
- Distinct `start_process_id` / `resume_process_id` in the responses
- X-Ray / CloudWatch traces for a full workflow run
- The deployed image digest and runtime identifier

Commit those as `evidence/agentcore-*.json` plus trace exports, never edited. If the gate fails, record the failure honestly and fall back to the localhost demo per the issue's kill condition — the organizer confirmed AgentCore is optional, so this costs Technical Implementation score, not eligibility.
