# Feasibility Spike Verdict

## Verdict: VALIDATED

The load-bearing Production Orchestrator workflow is **validated on a real Strands agent using Amazon Bedrock**, including factual tool use, persistent sessions, a real `BeforeToolCallEvent` interrupt, rejection with no domain mutation, approval with atomic mutation, exact proposal-hash binding, and a complete application audit chain.

Independent rejection and approval runs used `amazon.nova-lite-v1:0` in `us-east-1` through a named least-privilege role profile. Both passed all eight workflow checks. The paired evidence evaluator records `submission_gate_passed: true` in [`evidence/bedrock-verdict.json`](evidence/bedrock-verdict.json).

## Given / When / Then

**Given** a synthetic embroidery shop with rush order `RUSH-200`, 600 units of a required 1,200-unit thread supply, an 8-hour machine day already holding a 6-hour lower-priority order, and a deterministic proposal engine,

**when** a Strands 1.51.0 agent calls the seven shop tools and attempts to apply the generated plan,

**then** the agent detects the 600-unit inventory shortage and 2-hour capacity conflict, moves `STANDARD-100` to the next day, drafts three communications, stops on a real human interrupt, preserves revision 1 on rejection, and advances atomically to revision 2 on exact approval.

## Executed evidence

| Path | Decision | First stop | Final revision | Domain outcome | Workflow checks | Submission gate |
|---|---|---|---:|---|---|---|
| [`evidence/bedrock-rejection.json`](evidence/bedrock-rejection.json) | Reject | `interrupt` | 1 | Digest unchanged; no `plan_applied` event | 8/8 passed | Path passed |
| [`evidence/bedrock-approval.json`](evidence/bedrock-approval.json) | Approve | `interrupt` | 2 | Exact schedule and procurement task applied | 8/8 passed | Path passed |
| [`evidence/bedrock-verdict.json`](evidence/bedrock-verdict.json) | Paired gate | — | — | Both independent report hashes recorded | 2/2 paths passed | **Passed** |

Both runs used:

- `strands-agents==1.51.0`
- `BedrockModel` with `amazon.nova-lite-v1:0`
- Named least-privilege AWS profile in `us-east-1`
- Seven decorated Strands tools
- `ProductionPlanApprovalHook`
- `BeforeToolCallEvent.interrupt(...)`
- Official `interruptResponse` resume blocks
- `FileSessionManager` with persisted JSON session artifacts
- SQLite domain state and audit transactions

The earlier `rejection.json` and `approval.json` files remain explicitly labeled `ollama-fallback`; only the `bedrock-*` files are judged-provider evidence.

## What worked

1. **Real judged-provider invocation:** the installed SDK returned `end_turn` and `AgentResult` metrics through Amazon Bedrock/Nova Lite.
2. **Autonomous factual tools:** SDK metrics recorded all seven tools, while the model varied safe read-tool ordering between runs.
3. **Deterministic blockers:** exact inventory and capacity values came from tools, not model arithmetic.
4. **Immutable proposal:** both runs produced the same plan ID and SHA-256 content hash from the same revision-1 state.
5. **Real interrupt:** the first invocation stopped with `stop_reason == "interrupt"` and a `production-orchestrator-apply-plan` interrupt containing the reviewed changes.
6. **Rejection safety:** an `n` response recorded `approval_rejected`; domain digest and revision remained unchanged.
7. **Approval safety:** a `y` response bound to the exact hash, applied the schedule/procurement update, and appended `plan_applied` in one SQLite transaction.
8. **Persistent session state:** `FileSessionManager` wrote session, agent, and message artifacts for both executions.
9. **Durable audit:** evidence links factual reads, blockers, proposal, drafts, approval, and mutation.
10. **Repeatable core:** 24 automated tests cover deterministic planning, default denial, hash integrity, stale replay, rollback, hook behavior, audit completeness, explicit provider construction, and fail-closed paired-evidence evaluation.

## Important metric interpretation

Strands counts `apply_production_plan` at the interrupt boundary and again after resume. Therefore:

- Approval evidence shows one interrupted/error count and one successful resumed count.
- Rejection evidence shows interrupted/cancelled error counts.
- This does **not** indicate two domain mutations. The SQLite audit contains exactly one `plan_applied` event after approval and none after rejection.

## TDD evidence

Significant RED checkpoints observed before implementation:

- Missing production package for the shortage test
- No capacity blocker returned
- Missing proposal generator
- Missing approval and persistence modules
- Approved path ending in `NotImplementedError`
- Missing proposal-integrity and stale-replay errors
- Missing Strands workflow module
- Missing factual-read/proposal/draft audit events

Final local gate:

```text
24 passed
77.53% whole-package coverage (70% floor)
ruff: All checks passed
```

The current whole-package report is 77.53%. The live provider CLI is exercised through committed integration evidence rather than mocked network calls.

## Remaining hardening outside the completed feasibility gate

### Cross-process resume hardening

The executed proof resumes the interrupted agent within the same process. `FileSessionManager` persisted the Strands session artifacts, but `ShopService` intentionally keeps the narrow spike's proposal registry in memory. Before a server or UI is built, persist complete immutable proposals by content hash and add a test that reconstructs the agent and shop service in a fresh process before sending `interruptResponse`. This is a production-hardening requirement, not evidence of a safety failure in the executed same-process spike.

### Bedrock gate result

The previously blocked Bedrock criterion is complete:

- Region: `us-east-1`
- Model: `amazon.nova-lite-v1:0`
- Credential path: named least-privilege role profile
- Rejection: 8/8 checks passed; revision remained 1
- Approval: 8/8 checks passed; exact reviewed hash advanced state to revision 2
- Paired evaluator: `VALIDATED`

No credential values, session tokens, or credential-file contents appear in evidence.

## Recommendation for the real build

The architecture is viable. Retain the deterministic planning and SQLite transaction boundary unchanged. Treat the model as an orchestrator and explainer, not the source of inventory or scheduling truth. Build only the smallest before/interrupt/after interface needed to make this evidence legible to judges, and complete cross-process proposal reconstruction before deploying a server or UI that promises restart-safe approval.
