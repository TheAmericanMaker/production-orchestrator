# Production Orchestrator

> A Strands-powered production scheduling agent for small embroidery and decorated-apparel shops.

**Status:** **VALIDATED FEASIBILITY SPIKE** — paired Amazon Bedrock rejection and exact-approval workflows passed

**Hackathon:** Agents for Humans — Professional Agents track

**Repository visibility:** **Private during development.** The official rules require a public repository at submission; publish only after the full-history pre-publication, security, license, and setup gates pass.

**Completed feasibility issue:** [#1 — validate scheduling, approval, and audit loop](https://github.com/TheAmericanMaker/production-orchestrator/issues/1)

**Restart hardening:** [#3 — persist proposals for cross-process approval resume](https://github.com/TheAmericanMaker/production-orchestrator/issues/3)

**Fresh-process Strands resume:** [#4 — restore a persisted approval interrupt in a new process](https://github.com/TheAmericanMaker/production-orchestrator/issues/4)

**Executed result:** Both rejection and approval stopped on a real Strands interrupt. Rejection preserved revision 1; exact approval atomically advanced the schedule and procurement task to revision 2. See [`SPIKE_VERDICT.md`](SPIKE_VERDICT.md) and [`evidence/`](evidence/).

## Judge-facing local demo

Run the focused before → interrupt → after interface:

```bash
uv sync --locked
uv run production-orchestrator-demo
```

Open `http://127.0.0.1:8765`. The demo runs the **complete seven-tool Strands workflow**: the agent checks orders, inventory, and machine capacity through real tools, analyzes blockers with the deterministic planner, persists an immutable hash-addressed proposal, drafts unsent communications, and stops at a real Strands interrupt before the consequential write. The page renders the recorded tool trail as an activity feed, a before/after production board, the readable message drafts, and the exact decision consequences. Choose **Keep current schedule** or **Approve coordinated plan** and a fresh process reconstructs the persisted session and resumes the official interrupt.

Three synthetic scenarios are selectable from the page — a rush order with a capacity conflict and thread shortage, a team-jersey order that displaces two smaller jobs, and a metallic monogram batch with a material shortage. Expand **Technical proof** to inspect the immutable proposal hash, model/provider facts, distinct start/resume process IDs, and the full audit chain.

The local demo drives the workflow with a deterministic local tool-calling model, so it requires no paid model call; every shop fact still comes from a real tool call and the judged-provider evidence for the same workflow was executed through Amazon Bedrock (see `evidence/`). It binds only to localhost, stores transient SQLite/session state under the ignored `data/demo-runtime/` path, prepares communications as unsent drafts, and does not provide production authentication, multi-tenancy, deployment, or external integrations.

## Problem

Small production shops coordinate due dates, customer approvals, material availability, machine compatibility, operator capacity, and customer communication. A rush order can force several connected decisions, and the cost of missing one is rework, a late delivery, or an avoidable customer escalation.

Production Orchestrator is intended to inspect the real shop state, identify blockers, propose an evidence-backed schedule, surface consequential decisions for approval, and apply only the exact plan a human reviewed.

## Spike objective

Given a synthetic shop containing a rush order, material shortage, machine-capacity conflict, and movable lower-priority work, prove that a Python Strands agent can:

1. Invoke factual shop tools
2. Detect both blockers through deterministic logic
3. Produce a versioned schedule proposal
4. Draft communications tied to that proposal
5. Stop before a consequential write
6. Leave state unchanged after rejection
7. Apply exactly the reviewed proposal after approval
8. Preserve a complete audit chain

See [`DEVELOPMENT_CONTRACT.md`](DEVELOPMENT_CONTRACT.md) for the non-negotiable implementation and contest boundaries.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system and cross-process approval diagrams, and the table mapping every guarantee to the test or committed evidence that proves it
- [`docs/VIDEO_SCRIPT.md`](docs/VIDEO_SCRIPT.md) — shot-by-shot submission video script, including verified commands for the fail-closed capture
- [`evidence/OLLAMA_WORKFLOW_RUNS.md`](evidence/OLLAMA_WORKFLOW_RUNS.md) — live local-model rejection/approval results, hardware, token counts, latency, and measurement caveats

## Current verdict

The deterministic core and real Strands interrupt loop are operational. All seven Strands tools were observed in independent rejection and approval runs, `FileSessionManager` persisted each session, and all eight machine-evaluated workflow checks passed through Amazon Bedrock.

The judged-provider feasibility gate is validated with `amazon.nova-lite-v1:0` in `us-east-1`. Rejection preserved revision 1 with no `plan_applied` event; exact approval advanced atomically to revision 2, and the sole applied hash matched the proposal reviewed at the interrupt. Local Ollama development runs now also pass the complete eight-tool intake workflow and fresh-process rejection/approval paths with `gemma4:e4b`; they demonstrate provider independence but remain development evidence, not judged-provider proof.

Immutable proposals are persisted in SQLite by canonical content hash. Fresh-process Bedrock rejection and exact-approval runs now also prove that a new Python interpreter can reconstruct the same Strands agent and `FileSessionManager` session, restore the pending interrupt, and submit the official `interruptResponse`. Rejection preserved revision 1; approval applied the exact persisted proposal once and advanced to revision 2. Wrong interrupt IDs, altered session/proposal/provider bindings, stale state, and replay fail closed.

## Spike questions

| Priority | Question | Pass evidence |
|---:|---|---|
| 1 | Can a real Strands interrupt gate a consequential tool call? | Observable stop/resume with rejection and approval runs |
| 2 | Can deterministic scheduling detect and resolve the fixture's two blockers? | Repeatable behavioral tests and versioned proposal |
| 3 | Can approval bind to the exact reviewed proposal? | Stale/altered hash rejection and atomic state test |
| 4 | Can the agent produce judge-readable execution evidence? | Tool evidence, metrics, audit events, and CLI transcript |

## Development

Prerequisites:

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Ollama with an accessible tool-capable model for fallback reproduction
- A named least-privilege AWS profile, explicit region, and Bedrock model access for the judged path

Initial test/tooling setup:

```bash
uv sync
uv run pytest
uv run ruff check .
```

Run independent live fallback scenarios with unused runtime directories:

```bash
uv run production-orchestrator-spike \
  --decision reject \
  --runtime-dir data/runtime/reject-local \
  --report evidence/rejection-local.json

uv run production-orchestrator-spike \
  --decision approve \
  --runtime-dir data/runtime/approve-local \
  --report evidence/approval-local.json
```

The committed [`evidence/rejection.json`](evidence/rejection.json) and [`evidence/approval.json`](evidence/approval.json) are the audited baseline runs. Runtime databases and session files are ignored.

Run the judged Bedrock paths with a named profile and explicit region:

```bash
uv run production-orchestrator-spike \
  --decision reject \
  --provider bedrock \
  --model amazon.nova-lite-v1:0 \
  --aws-profile production-orchestrator-bedrock \
  --aws-region us-east-1 \
  --runtime-dir data/runtime/bedrock-reject-local \
  --report evidence/bedrock-rejection-local.json

uv run production-orchestrator-spike \
  --decision approve \
  --provider bedrock \
  --model amazon.nova-lite-v1:0 \
  --aws-profile production-orchestrator-bedrock \
  --aws-region us-east-1 \
  --runtime-dir data/runtime/bedrock-approve-local \
  --report evidence/bedrock-approval-local.json
```

The audited judged-provider reports are [`evidence/bedrock-rejection.json`](evidence/bedrock-rejection.json) and [`evidence/bedrock-approval.json`](evidence/bedrock-approval.json). Their hashes and paired gate result are recorded in [`evidence/bedrock-verdict.json`](evidence/bedrock-verdict.json).

The narrower restart proof is executed in two phases. Use a new runtime directory for each decision and pass the same explicit provider configuration to both commands:

```bash
uv run production-orchestrator-restart-spike start \
  --runtime-dir data/runtime/restart-reject-local \
  --checkpoint data/runtime/restart-reject-local/checkpoint.json \
  --provider bedrock \
  --model amazon.nova-lite-v1:0 \
  --aws-profile production-orchestrator-bedrock \
  --aws-region us-east-1

uv run production-orchestrator-restart-spike resume \
  --runtime-dir data/runtime/restart-reject-local \
  --checkpoint data/runtime/restart-reject-local/checkpoint.json \
  --decision reject \
  --report evidence/bedrock-restart-rejection-local.json \
  --provider bedrock \
  --model amazon.nova-lite-v1:0 \
  --aws-profile production-orchestrator-bedrock \
  --aws-region us-east-1
```

The independently executed restart reports are [`evidence/bedrock-restart-rejection.json`](evidence/bedrock-restart-rejection.json) and [`evidence/bedrock-restart-approval.json`](evidence/bedrock-restart-approval.json). They prove session reconstruction and approval safety across real process boundaries; the earlier paired reports remain the evidence for the complete seven-tool workflow.

### Local-model workflow (no cloud account)

The full intake workflow — customer email in, real model extraction, eight tools, interrupt, fresh-process resume — can also be driven by a local Ollama model. This exists to demonstrate that the governance layer is provider-independent: the interrupt, hash binding, checkpoint verification, and fail-closed resume are identical code for every provider. It needs no API key or cloud account, only a locally running Ollama with a tool-capable model.

The path was exercised live on August 13, 2026 with **`gemma4:e4b`**, an 8.0B-parameter Q4_K_M model running fully on an NVIDIA RTX 3060 12 GB through Ollama 0.32.9. Both runs invoked all eight tools in the required order and resumed a persisted interrupt in a new process. Rejection left revision 1 unchanged with zero applications; approval applied the exact same proposal hash once and advanced to revision 2. Both reported `WORKFLOW_PASSED=true`.

Pull the tested model if it is not already installed, then run each decision with a fresh runtime directory:

```bash
ollama pull gemma4:e4b

uv run production-orchestrator-restart-spike start \
  --runtime-dir data/runtime/ollama-intake-reject \
  --checkpoint data/runtime/ollama-intake-reject/checkpoint.json \
  --provider ollama-workflow --model gemma4:e4b \
  --ollama-host http://localhost:11434 \
  --scenario rush-order

uv run production-orchestrator-restart-spike resume \
  --runtime-dir data/runtime/ollama-intake-reject \
  --checkpoint data/runtime/ollama-intake-reject/checkpoint.json \
  --decision reject --report evidence/ollama-intake-rejection-local.json \
  --provider ollama-workflow --model gemma4:e4b \
  --ollama-host http://localhost:11434
```

Observed performance for nine model turns (eight tool selections plus the post-resume final turn):

| Run | Tokens (input / output) | Provider-reported model latency | First turn | Effective output rate | Outcome |
|---|---:|---:|---:|---:|---|
| Cold-start rejection | 14,925 / 1,948 | 93.235 s | 69.621 s | 20.9 tokens/s | Revision 1, 0 applied |
| Warm-model approval | 14,997 / 1,840 | 31.977 s | 6.853 s | 57.5 tokens/s | Revision 2, 1 applied |

The warm run's accumulated model latency was 65.7% lower (2.92× faster), primarily because the model was already resident. These are two development observations, not a controlled benchmark: the decisions and response lengths differ, and provider latency excludes tool execution and operator delay between processes. See [`evidence/OLLAMA_WORKFLOW_RUNS.md`](evidence/OLLAMA_WORKFLOW_RUNS.md) for the complete environment, methodology, tool sequence, correctness evidence, and limitations; the machine-readable reports are [`evidence/ollama-workflow-rejection.json`](evidence/ollama-workflow-rejection.json), [`evidence/ollama-workflow-approval.json`](evidence/ollama-workflow-approval.json), and [`evidence/ollama-workflow-performance.json`](evidence/ollama-workflow-performance.json).

The Ollama host is part of the checkpoint's trusted provider configuration: resuming against a different host fails closed, exactly like a swapped AWS profile. This path is **not** the judged provider — the contest evidence is the Bedrock runs above — so treat the local reports as development evidence only.

No AWS credentials, customer information, or runtime state belong in git.

Because the repository will eventually become public, review the complete reachable Git history—not only the current tree—before changing visibility. Publication authorization and timing are tracked in [issue #2](https://github.com/TheAmericanMaker/production-orchestrator/issues/2) and the private Hackathon Arena strategy issue `TheAmericanMaker/hackathon-arena#2`.

## Contest-period and prior-work disclosure

Production Orchestrator is a new project created during the Agents for Humans submission period.

The team previously developed and studied BobbinBoss/Aimbroidery, an Apache-2.0 embroidery-shop management application, and used that experience only as domain research to identify real scheduling, inventory, approval, and communication pain points. No BobbinBoss source code, prompts, UI, assets, database schema, customer data, fixtures, or implementation are incorporated into this repository. All submitted product code, Strands tools, agent behavior, interface, synthetic data, tests, documentation, architecture, and demo materials are created during the submission period. Third-party frameworks and dependencies will be listed with their licenses.

## Entrant

- Structure: Team of eligible individuals
- Authorized Representative: James Sesler
- Additional contributors must acknowledge eligibility, Team ownership, Representative authority, and prize-allocation terms before receiving access

## License

Apache License 2.0. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
