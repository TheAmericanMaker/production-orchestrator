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
10. **Repeatable core:** 41 automated tests cover deterministic planning, default denial, hash integrity, stale replay, rollback, hook behavior, audit completeness, explicit provider construction, fail-closed paired-evidence evaluation, immutable proposal persistence, and fresh-process Strands reconstruction.
11. **Durable proposal reconstruction:** complete canonical proposals are stored by content hash in SQLite. Fresh service instances and a separate Python interpreter reconstructed and applied the exact approved proposal; tampering, conflicting payloads, forged identities, stale revisions, and replay failed closed.
12. **Fresh-process Strands resume:** independent Bedrock rejection and approval paths reconstructed the same persisted `FileSessionManager` session in process B and submitted the official `interruptResponse`. Rejection preserved revision 1; approval applied one exact plan and advanced to revision 2.

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
41 passed
84.28% whole-package coverage (70% floor)
ruff: All checks passed
```

Coverage.py's subprocess patch measures the restart code executed in fresh interpreters. The live provider CLI is exercised through committed integration evidence rather than mocked network calls.

## Remaining hardening outside the completed feasibility gate

### Completed Strands cross-process resume proof

Complete immutable proposals are now persisted by canonical content hash. A fresh `ShopService` and a separate Python interpreter can reconstruct the exact proposal from SQLite, validate its identity, enforce the stored approval and base revision, and apply it atomically. Tampered, conflicting, forged, stale, and replayed proposals fail closed.

Independent live Bedrock rejection and approval paths started in process A, stopped at a real `BeforeToolCallEvent` interrupt, and exited. Process B reconstructed the same stable agent and `FileSessionManager` session from disk and submitted the official `interruptResponse`. The rejection audit contains no `plan_applied` event and stayed at revision 1. Approval contains exactly one `plan_applied` event for the reviewed hash and advanced to revision 2. See [`evidence/bedrock-restart-rejection.json`](evidence/bedrock-restart-rejection.json) and [`evidence/bedrock-restart-approval.json`](evidence/bedrock-restart-approval.json).

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

The architecture is viable. Retain the deterministic planning and SQLite transaction boundary unchanged. Treat the model as an orchestrator and explainer, not the source of inventory or scheduling truth. Build only the smallest before/interrupt/after interface needed to make the validated restart-safe approval evidence legible to judges.
