import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import boto3
from strands import Agent
from strands.models import BedrockModel
from strands.models.ollama import OllamaModel
from strands.session import FileSessionManager

from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.workflow import (
    ProductionPlanApprovalHook,
    ShopService,
    build_strands_tools,
)

SYSTEM_PROMPT = """You are executing the Production Orchestrator feasibility scenario.
Use tools for every factual claim. Do not calculate or invent shop facts yourself.
For RUSH-200, call these tools exactly once and in this order:
1. list_active_orders
2. get_inventory
3. get_machine_capacity
4. analyze_shop_blockers
5. propose_schedule
6. draft_communications using the exact content_hash from propose_schedule
7. apply_production_plan using that same exact content_hash
Do not ask for approval in prose; the apply tool has a human interrupt.
If the tool is rejected, do not retry it. Report the rejection and stop.
If it succeeds, report the blockers, displaced work, procurement, drafts, and applied revision.
"""

REQUIRED_WORKFLOW_CHECKS = frozenset(
    {
        "all_required_strands_tools_observed",
        "approval_applied_exact_plan",
        "factual_audit_chain_complete",
        "file_session_manager_persisted_state",
        "no_unapproved_plan_application",
        "read_tools_preserved_domain_state",
        "real_strands_interrupt",
        "rejection_preserved_domain_state",
    }
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_model(
    *,
    provider: str,
    model_id: str,
    host: str | None,
    aws_profile: str | None,
    aws_region: str | None,
    session_factory: Callable[..., Any] = boto3.Session,
    bedrock_model_factory: Callable[..., Any] = BedrockModel,
    ollama_model_factory: Callable[..., Any] = OllamaModel,
) -> Any:
    """Build an explicitly configured judged or fallback Strands model."""

    if provider == "bedrock":
        if not aws_profile:
            raise ValueError("Bedrock requires an explicit AWS profile")
        if not aws_region:
            raise ValueError("Bedrock requires an explicit AWS region")
        session = session_factory(profile_name=aws_profile, region_name=aws_region)
        return bedrock_model_factory(
            boto_session=session,
            model_id=model_id,
            max_tokens=4096,
            temperature=0,
        )
    if provider == "ollama":
        if not host:
            raise ValueError("Ollama requires an explicit host")
        return ollama_model_factory(host=host, model_id=model_id, temperature=0)
    raise ValueError(f"Unknown provider: {provider}")


def provider_verdict(provider: str, *, workflow_passed: bool) -> dict[str, object]:
    """Report one path without claiming the paired Bedrock gate has passed."""

    is_bedrock = provider == "bedrock"
    provider_path_passed = is_bedrock and workflow_passed
    return {
        "bedrock_status": "executed"
        if provider_path_passed
        else ("failed" if is_bedrock else "not-executed"),
        "provider": "amazon-bedrock" if is_bedrock else "ollama-fallback",
        "provider_path_passed": provider_path_passed,
        "submission_gate_blocker": (
            "Paired Bedrock rejection and approval evidence not evaluated"
            if provider_path_passed
            else (
                "Bedrock workflow checks did not all pass"
                if is_bedrock
                else "Bedrock rejection and approval workflows not executed"
            )
        ),
        "submission_gate_passed": False,
        "verdict": "PARTIAL",
    }


def paired_bedrock_verdict(reports: list[dict[str, object]]) -> dict[str, object]:
    """Validate the judged-provider gate only from substantiated independent paths."""

    def is_passing_path(report: dict[str, object]) -> bool:
        checks = report.get("checks")
        return (
            report.get("provider") == "amazon-bedrock"
            and report.get("bedrock_status") == "executed"
            and report.get("provider_path_passed") is True
            and report.get("workflow_passed") is True
            and isinstance(report.get("model_id"), str)
            and bool(report["model_id"])
            and isinstance(report.get("aws_region"), str)
            and bool(report["aws_region"])
            and isinstance(checks, dict)
            and set(checks) == REQUIRED_WORKFLOW_CHECKS
            and all(value is True for value in checks.values())
        )

    passing_reports = [report for report in reports if is_passing_path(report)]
    passing_decisions = {report.get("decision") for report in passing_reports}
    missing = sorted({"reject", "approve"} - passing_decisions)
    if missing:
        return {
            "submission_gate_blocker": "Missing passing Bedrock path: " + ", ".join(missing),
            "submission_gate_passed": False,
            "verdict": "PARTIAL",
        }

    model_regions = {(report["model_id"], report["aws_region"]) for report in passing_reports}
    if len(model_regions) != 1:
        return {
            "submission_gate_blocker": "Passing Bedrock paths used inconsistent model or region",
            "submission_gate_passed": False,
            "verdict": "PARTIAL",
        }
    return {
        "submission_gate_blocker": None,
        "submission_gate_passed": True,
        "verdict": "VALIDATED",
    }


def _metrics(result) -> dict[str, object]:
    summary = result.metrics.get_summary()
    keys = (
        "total_cycles",
        "total_duration",
        "tool_usage",
        "accumulated_usage",
        "accumulated_metrics",
    )
    return {key: summary.get(key) for key in keys}


def _audit(repository: SQLiteShopRepository) -> list[dict[str, object]]:
    return [
        {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "proposal_hash": event.proposal_hash,
            "details": event.details,
            "created_at": event.created_at,
        }
        for event in repository.audit_events()
    ]


def run_spike(
    *,
    decision: str,
    runtime_dir: Path,
    report_path: Path,
    provider: str,
    model_id: str,
    host: str | None,
    aws_profile: str | None,
    aws_region: str | None,
) -> dict[str, object]:
    runtime_dir.mkdir(parents=True, exist_ok=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=utc_now)
    repository.initialize(rush_order_scenario())
    service = ShopService(repository)
    model = build_model(
        provider=provider,
        model_id=model_id,
        host=host,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )
    session_manager = FileSessionManager(
        session_id=f"production-orchestrator-{decision}",
        storage_dir=str(runtime_dir / "sessions"),
    )
    agent = Agent(
        model=model,
        tools=build_strands_tools(service),
        hooks=[ProductionPlanApprovalHook(service, actor="spike-operator")],
        session_manager=session_manager,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        name="Production Orchestrator",
        description="Human-supervised production scheduling agent",
    )

    initial_digest = repository.domain_digest()
    first_result = agent("Execute the complete workflow for RUSH-200 now.")
    first_metrics = _metrics(first_result)
    digest_at_interrupt = repository.domain_digest()
    interrupts = list(first_result.interrupts or [])
    interrupt_report = [
        {
            "id": interrupt.id,
            "name": interrupt.name,
            "reason": interrupt.reason,
        }
        for interrupt in interrupts
    ]

    if first_result.stop_reason != "interrupt" or len(interrupts) != 1:
        raise RuntimeError(
            f"Expected one interrupt, got stop_reason={first_result.stop_reason!r}, "
            f"count={len(interrupts)}"
        )

    interrupt_reason = interrupts[0].reason
    if not isinstance(interrupt_reason, dict) or not isinstance(
        interrupt_reason.get("proposal_hash"), str
    ):
        raise TypeError("Interrupt did not bind a proposal hash")
    reviewed_proposal_hash = interrupt_reason["proposal_hash"]

    response = "y" if decision == "approve" else "n"
    final_result = agent(
        [
            {
                "interruptResponse": {
                    "interruptId": interrupt.id,
                    "response": response,
                }
            }
            for interrupt in interrupts
        ]
    )

    final_state = repository.load_state()
    final_digest = repository.domain_digest()
    final_metrics = _metrics(final_result)
    audit = _audit(repository)
    event_types = [event["event_type"] for event in audit]
    plan_applied_events = [event for event in audit if event["event_type"] == "plan_applied"]
    tool_usage = final_metrics.get("tool_usage")
    observed_tools = set(tool_usage) if isinstance(tool_usage, dict) else set()
    required_tools = {
        "list_active_orders",
        "get_inventory",
        "get_machine_capacity",
        "analyze_shop_blockers",
        "propose_schedule",
        "draft_communications",
        "apply_production_plan",
    }
    session_files = sorted((runtime_dir / "sessions").rglob("*.json"))
    required_evidence_events = {
        "active_orders_read",
        "inventory_read",
        "machine_capacity_read",
        "blockers_analyzed",
        "proposal_created",
        "communications_drafted",
    }
    checks = {
        "real_strands_interrupt": (
            first_result.stop_reason == "interrupt"
            and interrupts[0].name == "production-orchestrator-apply-plan"
        ),
        "read_tools_preserved_domain_state": initial_digest == digest_at_interrupt,
        "factual_audit_chain_complete": required_evidence_events.issubset(event_types),
        "all_required_strands_tools_observed": required_tools.issubset(observed_tools),
        "file_session_manager_persisted_state": bool(session_files),
        "rejection_preserved_domain_state": (
            decision != "reject" or (initial_digest == final_digest and final_state.revision == 1)
        ),
        "approval_applied_exact_plan": (
            decision != "approve"
            or (
                final_state.revision == 2
                and "approval_granted" in event_types
                and len(plan_applied_events) == 1
                and plan_applied_events[0]["proposal_hash"] == reviewed_proposal_hash
                and all(
                    task.proposal_hash == reviewed_proposal_hash
                    for task in final_state.procurement_tasks
                )
            )
        ),
        "no_unapproved_plan_application": (
            decision != "reject" or "plan_applied" not in event_types
        ),
    }

    workflow_passed = all(checks.values())
    provider_status = provider_verdict(provider, workflow_passed=workflow_passed)
    report: dict[str, Any] = {
        "generated_at": utc_now(),
        "spike": "Production Orchestrator Strands interrupt loop",
        "decision": decision,
        "model_id": model_id,
        "aws_profile": aws_profile if provider == "bedrock" else None,
        "aws_region": aws_region if provider == "bedrock" else None,
        "strands_agents_version": version("strands-agents"),
        **provider_status,
        "first_stop_reason": first_result.stop_reason,
        "final_stop_reason": final_result.stop_reason,
        "interrupts": interrupt_report,
        "initial_domain_digest": initial_digest,
        "digest_at_interrupt": digest_at_interrupt,
        "final_domain_digest": final_digest,
        "final_state_revision": final_state.revision,
        "final_schedule": [
            {
                "order_id": entry.order_id,
                "machine_id": entry.machine_id,
                "day": entry.day,
                "duration_hours": entry.duration_hours,
            }
            for entry in final_state.schedule
        ],
        "procurement_tasks": [
            {
                "material_id": task.material_id,
                "quantity": task.quantity,
                "proposal_hash": task.proposal_hash,
                "status": task.status,
            }
            for task in final_state.procurement_tasks
        ],
        "first_metrics": first_metrics,
        "final_metrics": final_metrics,
        "persisted_session_file_count": len(session_files),
        "final_message": str(final_result),
        "audit_events": audit,
        "checks": checks,
        "workflow_passed": workflow_passed,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"DECISION={decision}")
    print(f"FIRST_STOP_REASON={first_result.stop_reason}")
    print(f"FINAL_STOP_REASON={final_result.stop_reason}")
    print(f"FINAL_STATE_REVISION={final_state.revision}")
    print("AUDIT_EVENTS=" + ",".join(str(event) for event in event_types))
    print("CHECKS=" + json.dumps(checks, sort_keys=True))
    print(f"WORKFLOW_PASSED={str(workflow_passed).lower()}")
    print("SUBMISSION_GATE_PASSED=" + str(provider_status["submission_gate_passed"]).lower())
    print(f"REPORT={report_path}")

    if not workflow_passed:
        raise RuntimeError("One or more spike checks failed")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Production Orchestrator spike")
    parser.add_argument("--decision", choices=("reject", "approve"), required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--provider", choices=("ollama", "bedrock"), default="ollama")
    parser.add_argument("--model", default="glm-5.2:cloud")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--aws-profile")
    parser.add_argument("--aws-region")
    args = parser.parse_args()
    run_spike(
        decision=args.decision,
        runtime_dir=args.runtime_dir,
        report_path=args.report,
        provider=args.provider,
        model_id=args.model,
        host=args.host,
        aws_profile=args.aws_profile,
        aws_region=args.aws_region,
    )


if __name__ == "__main__":
    main()
