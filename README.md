# Production Orchestrator

> A Strands-powered production scheduling agent for small embroidery and decorated-apparel shops.

**Status:** Load-bearing feasibility spike  
**Hackathon:** Agents for Humans — Professional Agents track  
**Issue:** [#1 — validate scheduling, approval, and audit loop](https://github.com/TheAmericanMaker/production-orchestrator/issues/1)

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
- AWS credentials and Bedrock model access for the Strands integration phase

Initial test/tooling setup:

```bash
uv sync
uv run pytest
uv run ruff check .
```

No AWS credentials, customer information, or runtime state belong in git.

## Contest-period and prior-work disclosure

Production Orchestrator is a new project created during the Agents for Humans submission period.

The team previously developed and studied BobbinBoss/Aimbroidery, an Apache-2.0 embroidery-shop management application, and used that experience only as domain research to identify real scheduling, inventory, approval, and communication pain points. No BobbinBoss source code, prompts, UI, assets, database schema, customer data, fixtures, or implementation are incorporated into this repository. All submitted product code, Strands tools, agent behavior, interface, synthetic data, tests, documentation, architecture, and demo materials are created during the submission period. Third-party frameworks and dependencies will be listed with their licenses.

## Entrant

- Structure: Team of eligible individuals
- Authorized Representative: James Sesler
- Additional contributors must acknowledge eligibility, Team ownership, Representative authority, and prize-allocation terms before receiving access

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
