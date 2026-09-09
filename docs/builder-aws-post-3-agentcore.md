# Post 3 of 3 — draft for builder.aws

**Title:** Deploying a Governed Strands Agent to Bedrock AgentCore Runtime — Agents for Humans

---

Posts 1 and 2 covered *why* our agent can't write without a hash-bound human approval, and *how* that approval survives process death. This one is the deployment story: taking [Production Orchestrator](https://github.com/TheAmericanMaker/production-orchestrator) from a localhost demo to **Bedrock AgentCore Runtime** — and being precise about what that does and does not change.

## What AgentCore gives a Strands agent

AgentCore Runtime serves a container per session, isolated in its own microVM, behind an invocation API, with the execution role supplying credentials. For our agent that's a good fit for exactly one reason worth stating carefully: **the process boundary is already the product.** Our governance model treats "a fresh process resumes the approval" as a feature. AgentCore makes every invocation a fresh environment, so the discipline the local spike proved is the discipline production enforces.

## The HTTP contract, honestly mapped

A Strands interrupt cannot be held open *inside* one request/response invocation while a human deliberates — the invocation returns, the microVM idles. So the deployed contract maps the two-phase approval onto two invocations of one session:

1. `POST /invocations {"action": "start"}` — runs the eight-tool loop to the interrupt, persists the immutable proposal and checkpoint inside the session's state, returns the proposal for review. Revision stays 1; nothing beyond the sanctioned intake has mutated.
2. `POST /invocations {"action": "decide", "decision": "approve"|"reject", "proposal_hash": "…"}` — reconstructs the session in a fresh subprocess inside the container, re-verifies the checkpoint, submits the official `interruptResponse`, and applies the reviewed plan exactly once — or refuses.

Everything else is fail-closed, same as local: the decision must name the reviewed hash; a decided proposal refuses further decisions; session ids are allowlist-validated before they become path segments; per-session state is microVM-scoped and documented as such, not dressed up as durable cloud persistence.

## The permission ladder (what actually took the time)

The agent was done; the deployment was IAM homework. For anyone following the same path, the role setup is a checklist worth having in one place:

- **ECR**: a repo, and push permissions scoped to it — plus `BatchGetImage`, which the push finalize step needs and which is easy to miss.
- **Execution role** trusted by `bedrock-agentcore.amazonaws.com` (with a `SourceAccount`/`SourceArn` confused-deputy guard), carrying Bedrock invoke (note: include the account-scoped ARN pattern, not only `foundation-model/*`, or inference-profile model ids deny), workload access token permissions, logs, namespace-conditioned CloudWatch metrics, and X-Ray.
- **Control-plane caller**: `CreateAgentRuntime`, `UpdateAgentRuntime`, `Get/List`, `InvokeAgentRuntime`, `iam:PassRole` scoped to the one execution role and the one service — and, on first deployment, permission to create the AgentCore **service-linked role**. That one cost us a cycle: the error is a generic "Failed creating service linked role," and pre-creating it via `iam create-service-linked-role --aws-service-name bedrock-agentcore.amazonaws.com` was the clean fix.
- **Invocation auth**: the data plane accepts OAuth workload-identity tokens *or* SigV4 — SigV4 via `aws bedrock-agentcore invoke-agent-runtime` was the path of least resistance from a CLI with an existing profile.

Arm64 was a hard requirement (`linux/arm64` image; an amd64 image will not start), and qemu-user-static plus a binfmt registration on an x86 build host made the cross-build a non-event.

## The evidence standard, unchanged

Deployment claims get the same bar as every other claim in this project — committed output from real runs:

- Runtime `production_orchestrator-3S24euH1Cz` READY, endpoint READY, image digest pinned in ECR
- **Live invocation pairs**: reject → phase `rejected`, zero applications; approve → phase `approved`, exactly one application, hash `6ef62d9f…` — the same canonical hash as the deterministic, Bedrock, and Ollama runs of the same scenario
- Distinct start/resume process IDs inside the container on every decide — the restart proof, in the deployed path
- Contract verified live: a decide without the reviewed hash 400s; a decided proposal 409s

What we did *not* claim: durable cross-session persistence (state is microVM-scoped by design in this slice), production authentication, multi-tenancy. A governed agent that overstates its deployment is a governance product undermining itself.

## The takeaway

If your agent's safety model is "a human gates the consequential write," deploy it where the boundary is physical: per-session isolated runtimes, hash-bound approvals, invocation transcripts as evidence. The tools make the architecture easier to *enforce* — the honesty about what's ephemeral is on you.

Apache-2.0, at [github.com/TheAmericanMaker/production-orchestrator](https://github.com/TheAmericanMaker/production-orchestrator) — `deploy/agentcore/` has the Dockerfile and runbook; `evidence/agentcore-invocation-evidence.json` has the transcripts.

*Part 3 of my Agents for Humans hackathon series.*