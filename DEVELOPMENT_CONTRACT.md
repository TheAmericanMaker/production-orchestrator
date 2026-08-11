# Development Contract

This document is the durable execution contract for future agents and contributors.

## Authority

1. Official Agents for Humans rules
2. `TheAmericanMaker/hackathon-arena` rules audit and Strands feasibility notes
3. This contract
4. GitHub issue #1 and later issue decisions

If current official rules differ, stop, update the audit, and reconcile the implementation.

## Repository visibility

- Keep this repository private during development.
- The official Agents for Humans rules require a public repository URL at submission, not an explicitly public repository throughout development.
- Do not change visibility until the Arena pre-publication checklist passes and the publication decision is recorded in [publication gate issue #2](https://github.com/TheAmericanMaker/production-orchestrator/issues/2) and [`TheAmericanMaker/hackathon-arena#2`](https://github.com/TheAmericanMaker/hackathon-arena/issues/2).
- The gate must review the complete reachable Git history for secrets, customer/proprietary material, prohibited prior work, and private strategy artifacts; then rerun tests, dependency/security scans, license detection, and clean setup.
- After publication, verify anonymous access and keep it public for the rules-required judging period. Return it to private afterward when permitted.
- Never publish the Hackathon Arena strategy repository.

## New-work boundary

All submission work is fresh contest-period work. BobbinBoss/Aimbroidery is domain research only.

Never copy or import BobbinBoss source, git history, prompts, skills, schema, migrations, repository code, UI, assets, branding, docs text, fixtures, customer data, or its Tauri/pi-agent integration.

CloudStack Canvas supplies no code. CodeCartographer supplies no product code.

## Architecture invariants

- Python and the official `strands-agents` SDK
- Amazon Bedrock preferred for judged execution
- One agent until a measured need justifies multi-agent orchestration
- Deterministic Python logic owns inventory, capacity, blocker, duration, and scheduling calculations
- The model must retrieve facts through narrow tools and must not invent shop state
- Domain state lives outside model conversation state
- Every proposal has an immutable ID/version and content hash
- Approval binds to that exact proposal hash
- Missing, malformed, expired, altered, or stale approval defaults to denial
- Rejection causes zero domain mutation
- Approved plan application and audit append are atomic
- Communications remain drafts during the spike
- Synthetic data only

## Documented Strands API boundary

For human approval, use the official interrupt pattern:

- `HookProvider`
- `HookRegistry`
- `BeforeToolCallEvent`
- `event.interrupt(...)`
- `result.stop_reason`
- `result.interrupts`
- `interruptResponse`

Do not use invented patterns such as `Agent(storage=...)` or returning an interrupt dictionary from a hook.

## TDD

No deterministic production behavior without a failing behavioral test first.

Required RED → GREEN coverage:

- Inventory shortage
- Machine-capacity conflict
- Lower-priority displacement
- Deterministic proposal generation
- Proposal version/hash
- Default-deny approval
- Rejection with no mutation
- Stale/altered proposal rejection
- Atomic approved mutation
- Audit completeness

Record the significant RED and GREEN commands in the spike verdict.

## Deferred

Do not add these until issue #1 passes:

- Full web UI
- AgentCore deployment
- Graph/swarm architecture
- Real email or supplier APIs
- Production authentication or multi-tenancy
- Generic starter framework
- Submission/blog polish

## Team boundary

Entrant: Team. Representative: James Sesler.

Before granting another human repository access, record their eligibility, ownership acknowledgment, authorization of the Representative, and prize-allocation understanding.
