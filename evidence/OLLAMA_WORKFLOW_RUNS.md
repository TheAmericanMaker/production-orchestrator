# Local Ollama Workflow Runs

On August 13, 2026, the `ollama-workflow` provider was exercised end to end against a real Ollama server on the development workstation. These were the first live tests of the provider after its cloud-only implementation review.

The complete workflow ran twice: once through rejection and once through approval. Both runs started from the customer email, used a real local model for extraction and tool selection, invoked all eight tools in the required order, stopped at the real Strands approval interrupt, and resumed that persisted interrupt in a new Python process.

## Environment

| Component | Observed value |
|---|---|
| Ollama | 0.32.9 at `http://localhost:11434` |
| Model | `gemma4:e4b` (`c6eb396dbd59`) |
| Architecture | Gemma 4, 8.0B parameters, Q4_K_M |
| Model size | 9.6 GB on disk |
| Capabilities | Completion, tools, thinking, vision, audio |
| Runtime placement | 100% GPU during the run |
| Runtime context | 4,096 tokens observed (`ollama ps`); model declares 131,072 |
| GPU | NVIDIA GeForce RTX 3060, 12,288 MiB |
| CPU / RAM | AMD Ryzen 7 5800X, 16 logical CPUs / 31 GiB |
| Model request temperature | 0, set by `build_model()` |

`gemma4:e4b` was selected because it was already installed, advertised tool support, and fit fully on the available GPU. The README's original untested `qwen3:4b` example was replaced with the model actually exercised.

## Correctness results

| Run | Decision | Tool sequence | Interrupt restored in new process | Final revision | Applied plans | Result |
|---|---|---:|---:|---:|---:|---|
| Cold-start run | Reject | 8/8, correct order | Yes | 1 | 0 | `WORKFLOW_PASSED=true` |
| Warm-model run | Approve | 8/8, correct order | Yes | 2 | 1 | `WORKFLOW_PASSED=true` |

Both runs produced the same canonical proposal hash:

```text
6ef62d9fdaf1bb07f35b6d95385a0991035a894988afa7a7fdf17711d449d7dc
```

The exact tool sequence was:

1. `intake_customer_request`
2. `list_active_orders`
3. `get_inventory`
4. `get_machine_capacity`
5. `analyze_shop_blockers`
6. `propose_schedule`
7. `draft_communications`
8. `apply_production_plan`

Rejection recorded `approval_rejected`, left the domain at revision 1, and created no `plan_applied` event. Approval recorded `approval_granted` followed by one `plan_applied` event and advanced the domain atomically to revision 2.

## Observed performance

Each run contained nine model turns: eight tool-selection turns before the interrupt and one final turn after resume.

| Metric | Cold-start rejection | Warm-model approval |
|---|---:|---:|
| Input tokens | 14,925 | 14,997 |
| Output tokens | 1,948 | 1,840 |
| Total tokens | 16,873 | 16,837 |
| Provider-reported model latency | 93.235 s | 31.977 s |
| First-turn latency | 69.621 s | 6.853 s |
| Remaining eight turns | 23.614 s | 25.124 s |
| Effective output rate | 20.9 tokens/s | 57.5 tokens/s |
| Recorded cross-process session span | 102.624 s | 37.786 s |

The warm run reduced accumulated model latency by 65.7% (2.92× faster overall) because the model was already resident. The nearly identical remaining-turn totals—23.6 and 25.1 seconds—show that most of the cold-run difference was concentrated in initial model loading and first-turn processing.

These figures are development observations, **not a controlled model benchmark**:

- there were only two runs;
- the first loaded the model and the second reused it;
- rejection and approval have slightly different final responses and token counts;
- provider-reported latency excludes tool execution and operator delay between processes;
- effective output rate is aggregate output tokens divided by provider-reported model latency, not raw decode throughput;
- the recorded session span includes tool execution and the process handoff.

## Evidence files

- [`ollama-workflow-rejection.json`](ollama-workflow-rejection.json) — fresh-process rejection report
- [`ollama-workflow-approval.json`](ollama-workflow-approval.json) — fresh-process exact-approval report
- [`ollama-workflow-performance.json`](ollama-workflow-performance.json) — environment, methodology, token, latency, and integrity details

Runtime SQLite databases and `FileSessionManager` conversations remain intentionally ignored. The committed reports capture the provider/model identity, distinct process IDs, restored interrupt, exact proposal hash, final revision, audit events, and application count without committing transient conversation state.

This local evidence demonstrates provider independence and practical offline execution. It does not replace the Amazon Bedrock evidence used for the judged-provider feasibility claim.
