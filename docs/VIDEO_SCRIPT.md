# Submission video — shot-by-shot script

**Length:** target **4:15**, hard cap 5:00 (rules). **Language:** English. **Host:** public YouTube or Vimeo.
**Frame:** the shop's pain first, then the trust problem, then the proof. Name the problem, who it is for, and why it matters before any capability claim. Hero claim: *zero unapproved writes — provably fail-closed under forged, stale, and replayed inputs, across a real process boundary.*

The rules allow judges not to run the project, so this video is effectively the submission. Every number spoken below is produced by a command in this repository; the "Proof" column names it. **Do not narrate a capability that is still marked pending in [`ARCHITECTURE.md`](ARCHITECTURE.md).**

Produce after the Sep 7 scope freeze. Shots A–H can all be captured today — shot G's staging is verified in the appendix. Only the optional cloud insert depends on issue #12.

## Shot list

| # | Time | On screen | Narration (spoken) | Proof behind it |
|---|---|---|---|---|
| **A1** | 0:00–0:14 | The production board **before** anything runs: today's column already booked, the rush order sitting unplaced, the red-thread line short | "A customer's trade-show booth just moved up. They need forty embroidered caps with a red logo — today. The one embroidery machine is already six hours booked out of eight, and there's half the red thread the job needs. Something has to move, and every option touches a customer who is also waiting. That's one afternoon in a small shop, and it happens most weeks." | `fixtures.py` `rush_order_scenario` and the `rush-order` customer email |
| **A2** | 0:14–0:30 | Cut to the slate with the hero claim in text, then the demo page header (`LOCAL · SYNTHETIC DATA`, "Strands agent · human-approved writes") | "Software could untangle that in seconds. Nobody lets it — because one bad write to a live schedule costs more than every good one saves. So this is a **Strands** production scheduling agent for small embroidery and decorated-apparel shops, and the point of it is the write it is **not** allowed to make." | Framing only; no claim yet |
| **B** | 0:30–1:03 | The customer-email card labelled **UNSTRUCTURED IN**; then the first feed row appears as the agent extracts and validates the request | "It starts where work actually starts — a customer email. Forty embroidered caps, red logo, needed today. The model reads that email and proposes structured fields. It does not get to *assert* anything: the intake tool re-validates every field against the shop's catalog and calendar, then derives the duration and the thread quantity itself. A product that doesn't exist, a negative quantity, a day that isn't a work day — all rejected before anything is written." | `tests/test_intake.py` (five fail-closed cases); `test_derivation_is_deterministic_and_whole_hours` |
| **C** | 1:03–1:53 | Activity feed filling in: order queue, thread inventory, machine capacity, blockers, one coordinated plan, drafted messages. Cut to the **Production board** showing the conflict | "Now it works the shop through real tools — the order queue, thread inventory, machine capacity. Deterministic logic, not the model, finds the two blockers: the rush job collides with a machine that's already booked for today, and there isn't enough red thread. It proposes one coordinated plan — move the lower-priority job, order the thread, tell the customer — and it drafts the messages. Every fact on this board came from a tool call. Nobody is watching this part: it runs the whole job unattended, and it will interrupt you exactly once." | Eight-tool trail in `evidence/*.json` `audit_event_types`; board rendered from the recorded run |
| **D** | 1:53–2:23 | Feed row **"Stopped at a real Strands interrupt"** / **"Holding the write for approval"**; expand **Technical proof** to show the proposal hash | "Then it stops. Not a confirmation dialog the agent could skip — a real Strands interrupt raised before the one tool that can change the shop. The plan is frozen as an immutable proposal, addressed by the hash of its own content. That hash is what you're about to approve, and it's the only thing that can be applied." | `ProductionPlanApprovalHook` on `BeforeToolCallEvent`; `test_start_prompt_binds_exact_persisted_proposal_hash` |
| **E** | 2:23–2:48 | Click **Keep current schedule**; board snaps back; feed shows **"Zero plans applied"**, **"Current shop plan kept unchanged"** | "Reject, and nothing moves. Revision one, zero plans applied, and the rejection itself is in the audit log. The agent doesn't get a second attempt at the same write." | Real run: `DECISION=reject`, `FINAL_STATE_REVISION=1`, `plan_applied_count` 0, `approval_rejected` in the audit chain |
| **F** | 2:48–3:13 | Reload, click **Approve coordinated plan**; board animates the move; feed shows **"Applied the plan exactly once"**, **"You approved the exact reviewed plan"**; drafts still marked **"Drafts remain unsent"** | "Approve, and exactly the plan you read gets applied — once. Revision two. The messages are still drafts; sending them is a separate decision a human still owns." | Real run: `FINAL_STATE_REVISION=2`, `plan_applied_count` 1; `test_exact_approval_atomically_applies_schedule_procurement_and_audit` |
| **G** | 3:13–3:53 | **Terminal, split or full screen.** Three commands, three outcomes (staging in the appendix) | "Here's the part that matters in production. The approval arrives in a *different process* — the worker restarted. So everything in that checkpoint is treated as hostile. Forge the proposal hash: refused — *checkpoint proposal does not match canonical persisted evidence*, exit one, no report written. Replay a real approval after the shop has moved: refused — *domain state changed after the interrupt checkpoint*. Wrong interrupt id, wrong session, altered provider binding, stale state — every one fails closed, and the attempt is recorded." | Verified runs, exit code 1 each: forged hash and post-advance replay; plus `test_wrong_interrupt_id_fails_closed_after_restart`, `test_wrong_session_identity_fails_closed_after_restart`, `test_altered_provider_binding_fails_before_model_construction`, `test_stale_domain_state_fails_closed_after_restart`, `test_workflow_provider_rejects_forged_proposal_before_mutation` |
| **H** | 3:53–4:15 | Architecture diagram from `ARCHITECTURE.md`, then the hero claim card | "So the owner gets the afternoon back and still owns every decision that touches a customer — one screen, one approval, nothing moved behind their back. The scheduling is the demo. The governance layer is the product: one gated write, immutable proposals, approval bound to the exact reviewed hash, fail-closed across restarts, and a complete audit chain. That's what any shop — and any company — needs before it lets an agent touch the system of record." | `ARCHITECTURE.md` proof table |

Runtime budget: the shot list above lands at **4:15**; the optional cloud insert below takes it to **4:30**, still inside the 5:00 cap. Optional insert if the runtime is under target: a five-second cut showing the scenario chips (rush order, team jerseys, metallic monogram) with the narration "Three synthetic scenarios, same governed loop."

### Pending shot — cloud execution (issue #12)

If AgentCore deployment passes its Aug 29 gate, insert a 15-second shot between G and H: the deployed endpoint serving the workflow with visible logs and session isolation, narrated as "and it runs deployed, not just locally." **If the gate does not pass, cut the shot entirely** and leave H as written — do not narrate deployment from configuration files. The localhost demo plus committed deployment evidence is the honest fallback, and the video must not imply otherwise.

### Pending narration — live Bedrock intake (issue #11)

Shot B's narration is true today on the deterministic path. Once the live Bedrock intake evidence lands, add six words to B: "…and this runs on Amazon Bedrock." Until those two reports are committed, do not say it — the judged-provider evidence currently covers the seven-tool workflow and the restart proof, not intake.

## Appendix: staging the fail-closed capture (shot G)

Verified on `main` at `d8b7662`. Uses the deterministic workflow provider, so it needs no AWS credentials and costs nothing — the same code path the Bedrock runs take.

```bash
# 1. Reach a real interrupt and checkpoint it.
uv run python -m production_orchestrator.restart_spike start \
  --runtime-dir data/runtime/attack-demo \
  --checkpoint data/runtime/attack-demo/checkpoint.json \
  --provider deterministic-workflow --model deterministic-workflow-model \
  --scenario rush-order
# prints INTERRUPT_ID=… and PROPOSAL_HASH=…

# 2. Forge the proposal hash in the checkpoint, then try to approve.
cp -r data/runtime/attack-demo data/runtime/attack-forged
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("data/runtime/attack-forged/checkpoint.json")
c = json.loads(p.read_text()); c["proposal_hash"] = "f" * 64
p.write_text(json.dumps(c, indent=2))
PY
uv run python -m production_orchestrator.restart_spike resume \
  --runtime-dir data/runtime/attack-forged \
  --checkpoint data/runtime/attack-forged/checkpoint.json \
  --decision approve --report data/runtime/attack-forged/should-not-exist.json \
  --provider deterministic-workflow --model deterministic-workflow-model
# ValueError: Checkpoint proposal does not match canonical persisted evidence
# exit 1 — and should-not-exist.json is never created

# 3. Approve legitimately, then replay the same interrupt.
cp -r data/runtime/attack-demo data/runtime/attack-replay
uv run python -m production_orchestrator.restart_spike resume \
  --runtime-dir data/runtime/attack-replay \
  --checkpoint data/runtime/attack-replay/checkpoint.json \
  --decision approve --report data/runtime/attack-replay/approval.json \
  --provider deterministic-workflow --model deterministic-workflow-model
# FINAL_STATE_REVISION=2 / WORKFLOW_PASSED=true
uv run python -m production_orchestrator.restart_spike resume \
  --runtime-dir data/runtime/attack-replay \
  --checkpoint data/runtime/attack-replay/checkpoint.json \
  --decision approve --report data/runtime/attack-replay/replay.json \
  --provider deterministic-workflow --model deterministic-workflow-model
# ValueError: Domain state changed after the interrupt checkpoint — exit 1
```

Show the exit codes on camera (`echo "exit=$?"`); a stack trace alone reads as a crash, while a non-zero exit with no report written reads as a refusal. Runtime directories under `data/runtime/` are gitignored — delete them after capture.

## Recording notes

- **1920×1080, 30 fps minimum.** Browser at 100% zoom, window sized so the board and feed both fit without scrolling mid-shot.
- **Terminal:** light background, 16pt+, wide enough that no error line wraps. One command visible at a time.
- **Nothing sensitive on camera.** No AWS account ids, ARNs, profile names, access keys, real customer names, or personal paths. Prefer a clean shell in a plain directory; if a Bedrock shot is used, mask the account id in post.
- **Synthetic data only**, and say so on screen once (the demo header already carries `LOCAL · SYNTHETIC DATA`).
- Record narration separately from screen capture; the demo animations are short and easier to cut to a finished voice track.
- Keep the hero claim on screen as text at least twice (open and close) — judges skim.

## Pre-publish claims audit

Before upload, re-read the script against `ARCHITECTURE.md` and strike any sentence that:

1. states a capability whose proof row says **Pending**;
2. implies deployment, model access, or test results not backed by a committed report;
3. describes the demo's deterministic model as the judged provider, or vice versa;
4. says "secure" or "impossible" where the honest word is "fails closed";
5. leaves **Strands Agents** unnamed — the organizer guidance asks for it in the spoken narration, on screen, in the Devpost description, and in "Built With." A2 and D carry it spoken; confirm both survived the edit.

The submission text, requirements matrix, and builder.aws posts must use the same numbers as the finished cut. Tracking: issue #13.
