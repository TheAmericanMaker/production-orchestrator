# Production Orchestrator

> A Strands-powered production scheduling agent for small embroidery and decorated-apparel shops.

**Status:** **PARTIAL** — full Strands safety workflow passed on Ollama fallback; Bedrock execution blocked by missing AWS credentials

**Hackathon:** Agents for Humans — Professional Agents track

**Issue:** [#1 — validate scheduling, approval, and audit loop](https://github.com/TheAmericanMaker/production-orchestrator/issues/1)

**Executed result:** Both rejection and approval stopped on a real Strands interrupt. Rejection preserved revision 1; exact approval atomically advanced the schedule and procurement task to revision 2. See [`SPIKE_VERDICT.md`](SPIKE_VERDICT.md) and [`evidence/`](evidence/).

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

## Current verdict

The deterministic core and real Strands interrupt loop are operational. Twelve automated tests pass, all seven Strands tools were observed in both live runs, `FileSessionManager` persisted each session, and eight machine-evaluated workflow checks passed for rejection and approval.

The overall spike is not yet fully validated because this environment has no AWS credential chain or region. The committed evidence uses `glm-5.2:cloud` through the documented Ollama fallback and explicitly marks `submission_gate_passed: false`. Full UI/application scaffolding remains gated until a Bedrock run reproduces the result.

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
- AWS credentials, region, and Bedrock model access for the judged path

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

No AWS credentials, customer information, or runtime state belong in git.

## Contest-period and prior-work disclosure

Production Orchestrator is a new project created during the Agents for Humans submission period.

The team previously developed and studied BobbinBoss/Aimbroidery, an Apache-2.0 embroidery-shop management application, and used that experience only as domain research to identify real scheduling, inventory, approval, and communication pain points. No BobbinBoss source code, prompts, UI, assets, database schema, customer data, fixtures, or implementation are incorporated into this repository. All submitted product code, Strands tools, agent behavior, interface, synthetic data, tests, documentation, architecture, and demo materials are created during the submission period. Third-party frameworks and dependencies will be listed with their licenses.

## Entrant

- Structure: Team of eligible individuals
- Authorized Representative: James Sesler
- Additional contributors must acknowledge eligibility, Team ownership, Representative authority, and prize-allocation terms before receiving access

## License

Apache License 2.0. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
