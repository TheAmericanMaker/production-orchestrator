# Feasibility Spike Verdict

## Verdict: PARTIAL

The load-bearing Production Orchestrator workflow is **validated on a real Strands agent using the documented Ollama fallback**, including factual tool use, persistent sessions, a real `BeforeToolCallEvent` interrupt, rejection with no domain mutation, approval with atomic mutation, exact proposal-hash binding, and a complete application audit chain.

The overall spike remains **PARTIAL**, not VALIDATED, because no AWS credential chain is available in the execution environment and an Amazon Bedrock model invocation has not been run. The judged/submission path therefore remains gated.

## Given / When / Then

**Given** a synthetic embroidery shop with rush order `RUSH-200`, 600 units of a required 1,200-unit thread supply, an 8-hour machine day already holding a 6-hour lower-priority order, and a deterministic proposal engine,

**when** a Strands 1.51.0 agent calls the seven shop tools and attempts to apply the generated plan,

**then** the agent detects the 600-unit inventory shortage and 2-hour capacity conflict, moves `STANDARD-100` to the next day, drafts three communications, stops on a real human interrupt, preserves revision 1 on rejection, and advances atomically to revision 2 on exact approval.

## Executed evidence

| Path | Decision | First stop | Final revision | Domain outcome | Workflow checks | Submission gate |
|---|---|---|---:|---|---|---|
| [`evidence/rejection.json`](evidence/rejection.json) | Reject | `interrupt` | 1 | Digest unchanged; no `plan_applied` event | 8/8 passed | Blocked |
| [`evidence/approval.json`](evidence/approval.json) | Approve | `interrupt` | 2 | Exact schedule and procurement task applied | 8/8 passed | Blocked |

Both runs used:

- `strands-agents==1.51.0`
- `OllamaModel` with `glm-5.2:cloud`
- Seven decorated Strands tools
- `ProductionPlanApprovalHook`
- `BeforeToolCallEvent.interrupt(...)`
- Official `interruptResponse` resume blocks
- `FileSessionManager` with persisted JSON session artifacts
- SQLite domain state and audit transactions

The provider is explicitly labeled `ollama-fallback`; these files are not Bedrock evidence.

## What worked

1. **Real provider invocation:** the installed SDK returned `end_turn` and `AgentResult` metrics through the Ollama fallback.
2. **Autonomous factual tools:** SDK metrics recorded all seven tools, while the model varied safe read-tool ordering between runs.
3. **Deterministic blockers:** exact inventory and capacity values came from tools, not model arithmetic.
4. **Immutable proposal:** both runs produced the same plan ID and SHA-256 content hash from the same revision-1 state.
5. **Real interrupt:** the first invocation stopped with `stop_reason == "interrupt"` and a `production-orchestrator-apply-plan` interrupt containing the reviewed changes.
6. **Rejection safety:** an `n` response recorded `approval_rejected`; domain digest and revision remained unchanged.
7. **Approval safety:** a `y` response bound to the exact hash, applied the schedule/procurement update, and appended `plan_applied` in one SQLite transaction.
8. **Persistent session state:** `FileSessionManager` wrote session, agent, and message artifacts for both executions.
9. **Durable audit:** evidence links factual reads, blockers, proposal, drafts, approval, and mutation.
10. **Repeatable core:** 12 automated tests cover deterministic planning, default denial, hash integrity, stale replay, rollback, hook behavior, and audit completeness.

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
12 passed
ruff: All checks passed
```

The deterministic core reached 97% coverage before framework/CLI integration. The current whole-package report is 75% because the live provider CLI is exercised through committed integration evidence rather than mocked unit calls.

## What remains blocked

### Cross-process resume hardening

The executed proof resumes the interrupted agent within the same process. `FileSessionManager` persisted the Strands session artifacts, but `ShopService` intentionally keeps the narrow spike's proposal registry in memory. Before a server or UI is built, persist complete immutable proposals by content hash and add a test that reconstructs the agent and shop service in a fresh process before sending `interruptResponse`. This is a production-hardening requirement, not evidence of a safety failure in the executed same-process spike.

### Bedrock submission gate

Direct boto3 discovery found:

- AWS credential chain: missing
- AWS region: unset
- Bedrock invocation: not attempted because credentials are unavailable

The final spike criterion requires a real invocation of a model enabled in the entrant AWS account. Until that occurs:

- Do not change this verdict to VALIDATED.
- Do not claim judged-path Bedrock compatibility.
- Do not start full application/UI scaffolding.
- Do not close issue #1.

## Next required action

Provide a safe local AWS credential/profile or role with Bedrock model invocation permission and an explicit region. Then:

1. Discover an enabled text model without recording credentials.
2. Add a tested Bedrock provider selection path.
3. Execute rejection and approval evidence through Bedrock.
4. Confirm the same eight workflow checks.
5. Replace this PARTIAL verdict with VALIDATED only if both runs pass.

## Recommendation for the real build

The architecture is viable. Retain the deterministic planning and SQLite transaction boundary unchanged. Treat the model as an orchestrator and explainer, not the source of inventory or scheduling truth. After Bedrock passes, build only the smallest before/interrupt/after interface needed to make this evidence legible to judges.
