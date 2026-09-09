# Post 2 of 3 — draft for builder.aws

**Title:** Restart-Safe Strands Interrupts: Approving an Agent's Write From a New Process — Agents for Humans

---

The hardest question we got while building [Production Orchestrator](https://github.com/TheAmericanMaker/production-orchestrator) was not "does the agent work?" It was: **"what happens if the agent dies while it's waiting for your decision?"**

In production, approval latency is measured in human time — minutes, hours, a coffee break. The agent's process will not survive that. The browser tab closes, the Lambda times out, the worker gets recycled, the machine reboots. If a Strands interrupt dies with its process, then every consequential decision forces you to hold compute hostage for however long the human takes — or lose the work.

For the Agents for Humans hackathon we made the interrupt survivable, and this post is the recipe.

## The lifecycle, end to end

1. **Start.** The Strands agent runs its eight-tool loop — intake, order queue, inventory, capacity, blockers, plan, drafts — until the approval hook raises `BeforeToolCallEvent` before `apply_production_plan`. The turn ends. We persist three things: the Strands session (via `FileSessionManager`), an immutable proposal record addressed by the hash of its own content, and a checkpoint file carrying the interrupt id, session id, proposal hash, and digests of the domain state and provider configuration.
2. **Exit.** The process exits. Stdout carries `INTERRUPT_ID=…` and `PROPOSAL_HASH=…` — machine-readable for the caller, and for the blog post.
3. **Human time passes.** Minutes. Reboots. Whatever.
4. **Resume.** A fresh `python -m production_orchestrator.restart_spike resume …` restores the session from disk, re-verifies the checkpoint against persisted evidence, and submits the official `interruptResponse` — the same mechanism Strands uses for a same-process interrupt, now crossing a process boundary. The response is exact: **reject** and the domain state stays at revision 1 with zero applications; **approve** and exactly the reviewed plan applies, revision 2, one `plan_applied` audit event.

## What must be verified before the model is even constructed

This is the load-bearing part. Resume does not trust the checkpoint. It re-derives the expected configuration and refuses — before any model construction or write path exists — on mismatch:

- **Wrong interrupt id** → refuse. **Wrong session id** → refuse.
- **Altered provider binding** (e.g. the checkpoint says Bedrock but the runtime is handed a different provider, model, or AWS region) → refuse, because agent identity is a hash of provider+model+proposal+scenario: a swapped backend silently becoming the same "agent" would be a governance hole.
- **Stale domain state** — the shop database changed after the interrupt was checkpointed → refuse: `Domain state changed after the interrupt checkpoint`. The decision was made about a world that no longer exists.
- **Forged proposal hash** → refuse against the canonical persisted proposal.
- **Replay** — the same decision submitted twice → the second is refused, because the first application changed the domain digest.

All of these are contract tests in the suite, and the interesting ones are CLI transcripts in the repo, run with the same code path the judged Bedrock runs take.

## Proving it on real infrastructure

The claims are committed evidence, not prose:

- **Live Amazon Bedrock (Nova Lite)** ran both paths fresh-process: rejection held revision 1 with zero applications; approval reached revision 2 with exactly one application of the reviewed hash, official `interruptResponse`, distinct start/resume process IDs. Both runs produce proposal hash `6ef62d9f…` — the same hash the deterministic and the local-Ollama runs produce for the same scenario, which is itself evidence that the model extracts facts rather than inventing them.
- **Provider independence** was a happy accident of this design: because the checkpoint carries an explicit, verifiable provider binding, running the identical workflow across three backends (deterministic, Bedrock, Ollama) needed no special cases — the checkpoint validation just checks that what resumes matches what started.
- **114 tests, ~85% coverage**, including a regression test for the one real bug this design had: our first resume gate compared `provider == "bedrock"` literally, so the Bedrock-workflow checkpoint could never resume — a live evidence run caught it, and the fix made provider identity derive from a single source of truth (that story is in post 1).

## Why this matters beyond the demo

"Agent is waiting for approval" should be a *state on disk*, not a process you're afraid to kill. The moment it is, you get: zero compute held during human deliberation, deployments that don't orphan pending decisions, and an audit trail that shows what was decided, against what state, under which provider, by which process. That's the difference between a demo that impresses and an agent a shop owner will actually let touch their Friday.

Apache-2.0, at [github.com/TheAmericanMaker/production-orchestrator](https://github.com/TheAmericanMaker/production-orchestrator) — `evidence/` has the transcripts; `restart_spike.py` is the workflow entry point.

*Part 2 of my Agents for Humans hackathon series. Next: taking the governed agent from localhost to Bedrock AgentCore Runtime.*