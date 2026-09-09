# Devpost Submission Copy — Production Orchestrator

> Draft for `production-orchestrator#13`. Track: **Professional Agents**. All claims below are backed by committed evidence in the repo; nothing here asserts deployment or model access beyond what exists.

---

## Project description (Devpost "About" field)

**Production Orchestrator** — a governed Strands agent that runs the production side of a small embroidery and decorated-apparel shop.

Small shops drown in coordination: rush orders land mid-day, machines are double-booked, thread runs short, customers want updates. The usual answer is a chatbot that answers questions. Production Orchestrator *does the work*: it reads the customer's email, extracts the order, checks inventory and machine capacity against every active job, detects the blockers, and proposes a coordinated plan — rescheduled work, a procurement action, drafts for the customer, the operator, and the supplier — all waiting on one human decision.

What makes it different is the **governance layer around the write**:

- The consequential write is stopped by a **real Strands interrupt**. The plan is presented as an immutable, hash-addressed proposal; nothing changes until a human decides.
- **Approval is bound to the exact reviewed plan.** The decision is accepted only when the hash the human saw still matches canonical persisted evidence. Forged, stale, and replayed submissions fail closed — demonstrated on camera, with zero writes and the attack recorded in the audit log.
- **The approval survives process death.** The workflow is stopped mid-flight, a fresh process restores the persisted Strands session, and the official `interruptResponse` completes the loop — proven across distinct process IDs.
- **Deterministic logic owns every shop fact.** The model extracts; it never invents. Validation rejects any extraction that doesn't match reality, and the same scenario run across three model backends (deterministic, Amazon Bedrock, local Ollama) produces the *same canonical proposal hash*.

Built with the **Strands Agents SDK** (Amazon Bedrock, with AgentCore deployment for judge access). Apache-2.0. Every demo fact is synthetic; every claim is backed by a committed evidence report.

**Hero metric: zero unapproved writes — provably fail-closed under forged, stale, and replayed inputs, across a real process boundary.**

---

## Professional Agents track answers

**What does your agent do? (end-to-end work, not Q&A)**
It runs the production-scheduling workflow for a rush order: intake from a free-text customer email → validated order extraction → active-order, inventory, and machine-capacity reads → blocker analysis (shortage + capacity conflict) → an immutable proposal with schedule changes, a procurement action, and three drafted communications → human approval through a Strands interrupt → exactly-once application of the approved plan with a complete audit trail.

**Which parts are agent-driven vs. deterministic, and why?**
The Strands agent drives the tool loop (eight tools, including the validated intake tool added for email intake) and proposes the plan. Deterministic logic owns validation and every shop fact: machine types, quantities, dates, and inventory are checked against the real shop state, and invalid extractions are rejected fail-closed. This split is deliberate — LLM cognition where it helps, deterministic guarantees where correctness matters.

**How is human oversight enforced?**
The plan application is a human-gated consequential write. The agent cannot apply it; it can only propose. The apply step raises a Strands `BeforeToolCallEvent` interrupt that ends the agent's turn. A human approves or rejects the exact hash-addressed proposal; the decision is verified against canonical persisted evidence before anything mutates. Rejection mutates nothing; approval applies the reviewed plan exactly once; altered, stale, and replayed decisions fail closed.

**What happens when something goes wrong?**
Wrong hash → refused before mutation. Domain state changed after the checkpoint → refused. Same decision submitted twice → refused. Session/proposal/provider mismatch → refused. Missing or invalid fields at intake → rejected before the workflow starts. Each failure is fail-closed, exit-code-visible, and recorded in the audit trail — several are demonstrated on video.

---

## Built With

- `strands-agents` — Strands Agents SDK (agent orchestration, tool loop, interrupts)
- `amazon-bedrock` — judged model path (amazon.nova-lite-v1:0)
- `amazon-bedrock-agentcore` — runtime deployment (see repo deploy/ runbook)
- `python`, `sqlite`, `pytest`, `github-actions`

*(Strands Agents appears here and in the description text per organizer guidance.)*

---

## Prior-work disclosure (must be pasted into the submission form)

> **Prior-work disclosure:** Production Orchestrator was created as a new project during the Agents for Humans submission period. The team previously developed and studied BobbinBoss/Aimbroidery, an Apache-2.0 embroidery-shop management application, and used that experience only as domain research to identify real scheduling, inventory, approval, and communication pain points. No BobbinBoss source code, prompts, UI, assets, database schema, customer data, fixtures, or implementation were incorporated into this submission. All submitted product code, Strands tools, agent behavior, interface, synthetic data, tests, documentation, architecture, and demo materials were created during the submission period. Third-party frameworks and dependencies are listed with their licenses.

---

## Testing access (paste into the "Testing access" field)

1. `git clone https://github.com/TheAmericanMaker/production-orchestrator && cd production-orchestrator`
2. `python -m venv .venv && source .venv/bin/activate && pip install -e .`
3. `python -m production_orchestrator.demo --port 8080` → open `http://localhost:8080`
4. Pick a scenario. The agent runs the full workflow and stops at the approval interrupt.
5. Approve or reject from the browser; the before/after production board and audit trail update live.

No credentials needed. The full CLI evidence path (including live-model and attack demos) is documented in the repo README.

---

## Requirements matrix (rules → where satisfied)

| Rule requirement | Where |
|---|---|
| New Strands agent, real work end-to-end | Eight-tool Strands loop; `docs/ARCHITECTURE.md` |
| Built during submission period | Fresh repo, history starts Aug 11 2026; disclosure in submission |
| Public repo, MIT/Apache, license detected | Apache-2.0 file + About detection (verify at publish) |
| Install/run consistently | README quickstart; CI runs the install (uv) + 114 tests |
| Free unrestricted judge access through judging | Localhost demo; keep-public obligation through ~Oct 14 |
| Architecture diagram | `docs/ARCHITECTURE.md` |
| Video ≤5 min, public, problem/audience/importance | `docs/VIDEO_SCRIPT.md` (4:00 target); upload Sep 10–11 |
| Judges may elect not to test | Video shows the complete workflow + attack demo |
| Prior-work disclosure with submission | Text above, pasted into form |
| builder.aws posts (0.2 each) | Three posts, "Agents for Humans" in each title (below) |