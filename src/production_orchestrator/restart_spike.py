import argparse
import hashlib
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models.model import Model
from strands.session import FileSessionManager

from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.spike import build_model, utc_now
from production_orchestrator.workflow import (
    ProductionPlanApprovalHook,
    ShopService,
    build_strands_tools,
)

SESSION_ID = "production-orchestrator-restart"
AGENT_ID_PREFIX = "production-orchestrator"


def start_prompt(proposal_hash: str) -> str:
    return (
        "Call apply_production_plan with proposal_hash "
        f"{proposal_hash}. Do not call any other tool."
    )


def agent_id_for(
    *,
    provider: str,
    model_id: str,
    proposal_hash: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> str:
    configuration = json.dumps(
        {
            "provider": provider,
            "model_id": model_id,
            "proposal_hash": proposal_hash,
            "aws_profile": aws_profile if provider == "bedrock" else None,
            "aws_region": aws_region if provider == "bedrock" else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(configuration.encode()).hexdigest()[:16]
    return f"{AGENT_ID_PREFIX}-{digest}"


class DeterministicApplyModel(Model):
    """Exercise the real Strands tool/interrupt loop without network inference."""

    def __init__(self, proposal_hash: str) -> None:
        self.proposal_hash = proposal_hash
        self.config: dict[str, Any] = {"model_id": "deterministic-apply-model"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    async def structured_output(
        self, *args: Any, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        has_tool_result = any(
            "toolResult" in block for message in messages for block in message["content"]
        )
        yield {"messageStart": {"role": "assistant"}}
        if has_tool_result:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Approval decision processed."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": "apply_production_plan",
                            "toolUseId": "restart-apply-plan",
                        }
                    }
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {"input": json.dumps({"proposal_hash": self.proposal_hash})}
                    }
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 0},
            }
        }


def _configured_model(
    *,
    provider: str,
    model_id: str,
    proposal_hash: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> Any:
    if provider == "deterministic":
        if model_id != "deterministic-apply-model":
            raise ValueError("Deterministic restart proof requires deterministic-apply-model")
        return DeterministicApplyModel(proposal_hash)
    if provider == "bedrock":
        return build_model(
            provider="bedrock",
            model_id=model_id,
            host=None,
            aws_profile=aws_profile,
            aws_region=aws_region,
        )
    raise ValueError(f"Unsupported restart provider: {provider}")


def _build_agent(
    runtime_dir: Path,
    service: ShopService,
    proposal_hash: str,
    *,
    provider: str,
    model_id: str,
    aws_profile: str | None,
    aws_region: str | None,
    agent_id: str,
) -> Agent:
    return Agent(
        model=_configured_model(
            provider=provider,
            model_id=model_id,
            proposal_hash=proposal_hash,
            aws_profile=aws_profile,
            aws_region=aws_region,
        ),
        tools=build_strands_tools(service),
        hooks=[ProductionPlanApprovalHook(service, actor="restart-spike-operator")],
        session_manager=FileSessionManager(
            session_id=SESSION_ID,
            storage_dir=str(runtime_dir / "sessions"),
        ),
        agent_id=agent_id,
        system_prompt="Apply only the supplied persisted production proposal.",
        callback_handler=None,
        name="Production Orchestrator",
        description="Fresh-process Strands interrupt proof",
    )


def start(
    runtime_dir: Path,
    checkpoint_path: Path,
    *,
    provider: str,
    model_id: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> dict[str, object]:
    runtime_dir.mkdir(parents=True, exist_ok=False)
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=utc_now)
    repository.initialize(rush_order_scenario())
    service = ShopService(repository)
    proposal = service.propose_schedule("RUSH-200")
    proposal_hash = str(proposal["content_hash"])
    initial_digest = repository.domain_digest()
    agent_id = agent_id_for(
        provider=provider,
        model_id=model_id,
        proposal_hash=proposal_hash,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )
    agent = _build_agent(
        runtime_dir,
        service,
        proposal_hash,
        provider=provider,
        model_id=model_id,
        aws_profile=aws_profile,
        aws_region=aws_region,
        agent_id=agent_id,
    )

    result = agent(start_prompt(proposal_hash))
    interrupts = list(result.interrupts or [])
    if result.stop_reason != "interrupt" or len(interrupts) != 1:
        raise RuntimeError("Expected exactly one real Strands interrupt")
    interrupt = interrupts[0]
    reason = interrupt.reason
    if not isinstance(reason, dict) or reason.get("proposal_hash") != proposal_hash:
        raise RuntimeError("Interrupt did not bind the persisted proposal hash")
    digest_at_interrupt = repository.domain_digest()
    if digest_at_interrupt != initial_digest:
        raise RuntimeError("Domain state changed before approval")

    checkpoint: dict[str, object] = {
        "agent_id": agent_id,
        "first_stop_reason": result.stop_reason,
        "interrupt_id": interrupt.id,
        "interrupt_name": interrupt.name,
        "proposal_hash": proposal_hash,
        "session_id": SESSION_ID,
        "start_process_id": os.getpid(),
        "initial_domain_digest": initial_digest,
        "digest_at_interrupt": digest_at_interrupt,
        "provider": provider,
        "model_id": model_id,
        "aws_profile": aws_profile if provider == "bedrock" else None,
        "aws_region": aws_region if provider == "bedrock" else None,
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    print(f"INTERRUPT_ID={interrupt.id}")
    print(f"PROPOSAL_HASH={proposal_hash}")
    return checkpoint


def _load_checkpoint(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    required = {
        "agent_id",
        "first_stop_reason",
        "interrupt_id",
        "interrupt_name",
        "proposal_hash",
        "session_id",
        "start_process_id",
        "initial_domain_digest",
        "digest_at_interrupt",
        "provider",
        "model_id",
        "aws_profile",
        "aws_region",
    }
    if set(data) != required:
        raise ValueError("Checkpoint fields are malformed")
    if data["session_id"] != SESSION_ID:
        raise ValueError("Checkpoint session identity is invalid")
    if data["first_stop_reason"] != "interrupt":
        raise ValueError("Checkpoint is not at an interrupt")
    for key in ("interrupt_id", "proposal_hash", "initial_domain_digest", "digest_at_interrupt"):
        if not isinstance(data[key], str) or not data[key]:
            raise ValueError(f"Checkpoint {key} is invalid")
    if not isinstance(data["start_process_id"], int):
        raise TypeError("Checkpoint process identity is invalid")
    if data["provider"] not in {"deterministic", "bedrock"}:
        raise ValueError("Checkpoint provider is invalid")
    if not isinstance(data["model_id"], str) or not data["model_id"]:
        raise ValueError("Checkpoint model identity is invalid")
    if data["provider"] == "bedrock" and (
        not isinstance(data["aws_profile"], str)
        or not data["aws_profile"]
        or not isinstance(data["aws_region"], str)
        or not data["aws_region"]
    ):
        raise ValueError("Checkpoint Bedrock configuration is incomplete")
    expected_agent_id = agent_id_for(
        provider=str(data["provider"]),
        model_id=str(data["model_id"]),
        proposal_hash=str(data["proposal_hash"]),
        aws_profile=data["aws_profile"] if isinstance(data["aws_profile"], str) else None,
        aws_region=data["aws_region"] if isinstance(data["aws_region"], str) else None,
    )
    if data["agent_id"] != expected_agent_id:
        raise ValueError(
            "Checkpoint agent identity does not match proposal or provider configuration"
        )
    return data


def resume(
    runtime_dir: Path,
    checkpoint_path: Path,
    decision: str,
    report_path: Path,
    *,
    provider: str,
    model_id: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> dict[str, object]:
    checkpoint = _load_checkpoint(checkpoint_path)
    trusted_configuration = {
        "provider": provider,
        "model_id": model_id,
        "aws_profile": aws_profile if provider == "bedrock" else None,
        "aws_region": aws_region if provider == "bedrock" else None,
    }
    persisted_configuration = {key: checkpoint[key] for key in trusted_configuration}
    if persisted_configuration != trusted_configuration:
        raise ValueError("Checkpoint does not match trusted provider configuration")
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=utc_now)
    proposal_hash = str(checkpoint["proposal_hash"])
    service = ShopService(repository)
    agent_id = str(checkpoint["agent_id"])
    agent = _build_agent(
        runtime_dir,
        service,
        proposal_hash,
        provider=provider,
        model_id=model_id,
        aws_profile=aws_profile,
        aws_region=aws_region,
        agent_id=agent_id,
    )

    interrupt_id = str(checkpoint["interrupt_id"])
    if repository.domain_digest() != checkpoint["digest_at_interrupt"]:
        raise ValueError("Domain state changed after the interrupt checkpoint")
    if os.getpid() == checkpoint["start_process_id"]:
        raise RuntimeError("Resume did not cross a process boundary")

    response = "y" if decision == "approve" else "n"
    result = agent(
        [
            {
                "interruptResponse": {
                    "interruptId": interrupt_id,
                    "response": response,
                }
            }
        ]
    )

    state = repository.load_state()
    final_digest = repository.domain_digest()
    audit = repository.audit_events()
    event_types = [event.event_type for event in audit]
    applied = [event for event in audit if event.event_type == "plan_applied"]
    rejection_passed = decision != "reject" or (
        state.revision == 1 and final_digest == checkpoint["initial_domain_digest"] and not applied
    )
    approval_passed = decision != "approve" or (
        state.revision == 2 and len(applied) == 1 and applied[0].proposal_hash == proposal_hash
    )
    workflow_passed = (
        result.stop_reason == "end_turn"
        and rejection_passed
        and approval_passed
        and (
            "approval_granted" in event_types
            if decision == "approve"
            else "approval_rejected" in event_types
        )
    )
    report: dict[str, object] = {
        "decision": decision,
        "final_state_revision": state.revision,
        "final_stop_reason": result.stop_reason,
        "interrupt_id": interrupt_id,
        "official_interrupt_response_used": True,
        "plan_applied_count": len(applied),
        "process_boundary_proven": True,
        "proposal_hash": proposal_hash,
        "provider": provider,
        "model_id": model_id,
        "aws_region": aws_region,
        "resume_process_id": os.getpid(),
        "session_interrupt_restored": True,
        "start_process_id": checkpoint["start_process_id"],
        "workflow_passed": workflow_passed,
        "audit_event_types": event_types,
        "final_domain_digest": final_digest,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not workflow_passed:
        raise RuntimeError("Fresh-process Strands resume checks failed")
    print(f"DECISION={decision}")
    print(f"FINAL_STATE_REVISION={state.revision}")
    print("WORKFLOW_PASSED=true")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Prove fresh-process Strands interrupt resume")
    subparsers = parser.add_subparsers(dest="phase", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--runtime-dir", type=Path, required=True)
    start_parser.add_argument("--checkpoint", type=Path, required=True)
    start_parser.add_argument(
        "--provider", choices=("deterministic", "bedrock"), default="deterministic"
    )
    start_parser.add_argument("--model", default="deterministic-apply-model")
    start_parser.add_argument("--aws-profile")
    start_parser.add_argument("--aws-region")
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--runtime-dir", type=Path, required=True)
    resume_parser.add_argument("--checkpoint", type=Path, required=True)
    resume_parser.add_argument("--decision", choices=("reject", "approve"), required=True)
    resume_parser.add_argument("--report", type=Path, required=True)
    resume_parser.add_argument(
        "--provider", choices=("deterministic", "bedrock"), default="deterministic"
    )
    resume_parser.add_argument("--model", default="deterministic-apply-model")
    resume_parser.add_argument("--aws-profile")
    resume_parser.add_argument("--aws-region")
    args = parser.parse_args()
    if args.phase == "start":
        start(
            args.runtime_dir,
            args.checkpoint,
            provider=args.provider,
            model_id=args.model,
            aws_profile=args.aws_profile,
            aws_region=args.aws_region,
        )
    else:
        resume(
            args.runtime_dir,
            args.checkpoint,
            args.decision,
            args.report,
            provider=args.provider,
            model_id=args.model,
            aws_profile=args.aws_profile,
            aws_region=args.aws_region,
        )


if __name__ == "__main__":
    main()
