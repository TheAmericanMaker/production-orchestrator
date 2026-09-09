# Post 1 of 3 — draft for builder.aws

**Title:** Hash-Bound Human Approval for Agent Writes — Agents for Humans

---

Agents are getting good at *reading* systems — summarizing, planning, drafting. The unsolved problem is the write. Nobody lets an agent touch the system of record, because one bad write costs more than every good one saves: the wrong schedule wrecks real production, the wrong email burns a real customer.

For the [Agents for Humans](https://agentsforhumans.devpost.com/) hackathon we built **Production Orchestrator**, a Strands Agents scheduling agent for small embroidery and decorated-apparel shops, and the part worth writing about is not the scheduling. It's the approval gate: a consequential write that an agent can *propose* but never *perform* without a human decision bound to the exact bytes that human reviewed.

## The pattern in one paragraph

The agent runs a real tool loop — read the order queue, check thread inventory, check machine capacity, find blockers, draft the plan and the customer/supplier messages. The final tool, `apply_production_plan`, is the only one that can mutate the shop. We wrap it with a Strands `BeforeToolCallEvent` hook that raises a real interrupt: the agent's turn ends, the plan is persisted as an immutable, hash-addressed proposal, and the workflow exits with the proposal hash on stdout. Nothing has changed yet. When the human decides, a **fresh process** restores the persisted Strands session, re-verifies the checkpoint, and submits the official `interruptResponse`. The write happens exactly once — or not at all.

## Why hash-bound, not just human-in-the-loop

A "human approved this" flag is easy to forge and easy to stale out. The decision our resume phase accepts must name the proposal hash, and that hash must still match canonical persisted evidence. That single check makes three attacks fail closed, each verified by committed CLI transcripts:

1. **Forged hash** — edit the checkpoint's `proposal_hash` to 64 zeros and approve: `ValueError: Checkpoint proposal does not match canonical persisted evidence`, exit 1, no report written, revision unchanged.
2. **Stale state** — mutate the shop database behind the checkpoint, then approve: `ValueError: Domain state changed after the interrupt checkpoint`. The digest recorded at interrupt time no longer describes reality.
3. **Replay** — submit the same approval twice: the second is refused, and the audit trail shows exactly one `plan_applied` event from the legitimate one.

Every refusal is an audited event, not a crash: the rejection itself lands in the log with the proposal hash and the reason. "Zero unapproved writes — provably fail-closed under forged, stale, and replayed inputs" became the product's hero claim because the test suite can say so, not because the landing page does.

## What the journey actually looked like

Two defects we shipped and then caught are the honest part of this story:

- **The resume gate that gated nothing.** Our resume phase originally checked `provider == "bedrock"` with a literal string comparison. A live Bedrock evidence run revealed that a genuine `bedrock-workflow` checkpoint could never pass its own trusted-configuration check — the suite never exercised that path. The fix made a single `_provider_configuration()` helper the source of truth for provider identity across `agent_id_for()`, `start()`, and `resume()`, with a parametrized contract test over every provider.
- **The forged-hash window.** Early on, the resume path trusted the checkpoint's proposal hash without re-checking it against the canonical proposal persisted at interrupt time. If a checkpoint file were tampered with mid-flight, the tampered hash would have been applied. The fix validates checkpoint identity against persisted evidence *before* the model is even constructed — the refusal happens before any write path exists.

Both were found by running the workflow against real Bedrock and attacking our own checkpoint files — the second finding became the on-camera attack demo.

## The numbers behind the claim

- 114 automated tests, ~85% coverage, CI green on every merge; the fail-closed behaviors are contract tests, not manual checks
- The same scenario produces the **same canonical proposal hash** (`6ef62d9f…`) across three model backends — deterministic, Amazon Bedrock (Nova Lite), and a local Ollama model — because deterministic validation owns every shop fact and the model only extracts
- Live Bedrock runs of both paths are committed as evidence reports: rejection holds revision 1 with zero applications; approval reaches revision 2 with exactly one

## Why this generalizes

The scheduling domain is the demo; the governance layer is the product. Every enterprise that has evaluated an agent and said "not in production" said it about the write, not the read. Immutable hash-addressed proposals, interrupts that genuinely end the agent's turn, decisions bound to reviewed content, and fail-closed verification across process death are the pieces any company needs before an agent touches a system of record — and they're all just Strands SDK primitives used with intent.

The code is Apache-2.0 at [github.com/TheAmericanMaker/production-orchestrator](https://github.com/TheAmericanMaker/production-orchestrator), with the attack transcripts, evidence reports, and the full audit-chain design in the repo.

*This post is part of my entry in the Agents for Humans hackathon. Next: making the approval survive process death.*