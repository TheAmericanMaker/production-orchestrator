import argparse
import hashlib
import json
import os
import re
from collections.abc import AsyncGenerator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models.model import Model
from strands.session import FileSessionManager

from production_orchestrator.fixtures import SCENARIOS
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.spike import (
    CONTAINER_ROLE_CREDENTIALS,
    CREDENTIAL_SOURCES,
    PROFILE_CREDENTIALS,
    build_model,
    utc_now,
)
from production_orchestrator.workflow import (
    ProductionPlanApprovalHook,
    ShopService,
    build_strands_tools,
)

SESSION_ID = "production-orchestrator-restart"
AGENT_ID_PREFIX = "production-orchestrator"

WORKFLOW_PROVIDER = "deterministic-workflow"
WORKFLOW_MODEL_ID = "deterministic-workflow-model"
BEDROCK_WORKFLOW_PROVIDER = "bedrock-workflow"
OLLAMA_WORKFLOW_PROVIDER = "ollama-workflow"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

_WORKFLOW_PROVIDERS = frozenset(
    {WORKFLOW_PROVIDER, BEDROCK_WORKFLOW_PROVIDER, OLLAMA_WORKFLOW_PROVIDER}
)
_AWS_PROVIDERS = frozenset({"bedrock", BEDROCK_WORKFLOW_PROVIDER})
_OLLAMA_PROVIDERS = frozenset({OLLAMA_WORKFLOW_PROVIDER})

WORKFLOW_SYSTEM_PROMPT = """You are executing the Production Orchestrator workflow.
Use tools for every factual claim. Do not calculate or invent shop facts yourself.
Extract only what the customer message states; the intake tool validates it.
Call these tools exactly once and in this order:
1. intake_customer_request with the fields extracted from the customer message
2. list_active_orders
3. get_inventory
4. get_machine_capacity
5. analyze_shop_blockers
6. propose_schedule
7. draft_communications using the exact content_hash from propose_schedule
8. apply_production_plan using that same exact content_hash
Do not ask for approval in prose; the apply tool has a human interrupt.
If the tool is rejected, do not retry it. Report the rejection and stop.
"""


def start_prompt(proposal_hash: str) -> str:
    return (
        "Call apply_production_plan with proposal_hash "
        f"{proposal_hash}. Do not call any other tool."
    )


def workflow_start_prompt(target_order_id: str) -> str:
    return f"Execute the complete workflow for {target_order_id} now."


def workflow_intake_prompt(spec) -> str:
    codes = ", ".join(sorted(spec.catalog))
    return (
        "Today is 2026-08-12. A customer request just arrived:\n"
        "---\n"
        f"{spec.customer_email}\n"
        "---\n"
        f"Assign it order id {spec.target_order_id}. "
        f"Available catalog product codes: {codes}. "
        "Extract the product code, quantity, requested completion day "
        "(YYYY-MM-DD), and a 1-100 rush priority from the message, then call "
        "intake_customer_request with exactly those fields. Then execute the "
        "complete workflow for the new order through the remaining tools, "
        "ending with apply_production_plan."
    )


def _provider_configuration(
    *,
    provider: str,
    model_id: str,
    aws_profile: str | None,
    aws_region: str | None,
    credential_source: str = PROFILE_CREDENTIALS,
    ollama_host: str | None = None,
) -> dict[str, str | None]:
    """The provider identity persisted at start and re-trusted at resume.

    start(), resume(), and agent_id_for() must all derive AWS fields from
    this one function so the checkpoint contract cannot drift by provider.

    `credential_source` records which credential chain a run is entitled to
    use — a named developer profile, or the task role a container receives on
    AgentCore Runtime. Swapping it changes the identity the agent acts as, so
    resume compares it like every other field.
    """
    is_aws = provider in _AWS_PROVIDERS
    return {
        "provider": provider,
        "model_id": model_id,
        "aws_profile": aws_profile if is_aws else None,
        "aws_region": aws_region if is_aws else None,
        "credential_source": credential_source if is_aws else PROFILE_CREDENTIALS,
        "ollama_host": ollama_host if provider in _OLLAMA_PROVIDERS else None,
    }


# Agent IDs address persisted Strands sessions, so the fields hashed into them
# are frozen: adding a configuration field must never change an existing ID, or
# checkpoints written by earlier versions would stop resuming. New fields still
# belong in _provider_configuration — they are compared at resume, just not
# hashed here. Guarded by test_agent_id_is_stable_for_the_historical_configuration_fields.
_AGENT_ID_CONFIGURATION_FIELDS = ("provider", "model_id", "aws_profile", "aws_region")


def agent_id_for(
    *,
    provider: str,
    model_id: str,
    proposal_hash: str | None,
    aws_profile: str | None,
    aws_region: str | None,
    scenario: str | None = None,
) -> str:
    persisted = _provider_configuration(
        provider=provider,
        model_id=model_id,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )
    configuration = json.dumps(
        {
            **{field: persisted[field] for field in _AGENT_ID_CONFIGURATION_FIELDS},
            "proposal_hash": None if provider in _WORKFLOW_PROVIDERS else proposal_hash,
            "scenario": scenario,
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


class DeterministicWorkflowModel(Model):
    """Drive the complete seven-tool Strands loop without network inference.

    The scripted sequence mirrors the judged Bedrock workflow: every shop fact
    still comes from a real tool call, the proposal hash is read from the
    propose_schedule tool result, and the apply step stops at the real
    Strands interrupt.
    """

    SEQUENCE = (
        "list_active_orders",
        "get_inventory",
        "get_machine_capacity",
        "analyze_shop_blockers",
        "propose_schedule",
        "draft_communications",
        "apply_production_plan",
    )
    _HASH_PATTERNS = (
        re.compile(r'\\?"content_hash\\?"\s*:\s*\\?"([0-9a-f]{64})\\?"'),
        re.compile(r"'content_hash':\s*'([0-9a-f]{64})'"),
    )

    def __init__(self, target_order_id: str, extraction=None) -> None:
        self.target_order_id = target_order_id
        self.extraction = extraction
        self.config: dict[str, Any] = {"model_id": WORKFLOW_MODEL_ID}

    @property
    def sequence(self) -> tuple[str, ...]:
        if self.extraction is None:
            return self.SEQUENCE
        return ("intake_customer_request", *self.SEQUENCE)

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    async def structured_output(
        self, *args: Any, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError
        yield  # pragma: no cover

    @classmethod
    def _find_content_hash(cls, value: Any) -> str | None:
        if isinstance(value, dict):
            candidate = value.get("content_hash")
            if isinstance(candidate, str) and len(candidate) == 64:
                return candidate
            for child in value.values():
                found = cls._find_content_hash(child)
                if found is not None:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value:
                found = cls._find_content_hash(child)
                if found is not None:
                    return found
        elif isinstance(value, str):
            for pattern in cls._HASH_PATTERNS:
                match = pattern.search(value)
                if match:
                    return match.group(1)
        return None

    def _proposal_hash_from(self, messages: Any) -> str:
        for message in messages:
            for block in message.get("content", ()):
                if "toolResult" not in block:
                    continue
                found = self._find_content_hash(block["toolResult"])
                if found is not None:
                    return found
        raise RuntimeError("Proposal hash is not available from the propose_schedule result")

    def _input_for(self, tool_name: str, messages: Any) -> dict[str, Any]:
        if tool_name == "intake_customer_request":
            return asdict(self.extraction)
        if tool_name in {"analyze_shop_blockers", "propose_schedule"}:
            return {"target_order_id": self.target_order_id}
        if tool_name in {"draft_communications", "apply_production_plan"}:
            return {"proposal_hash": self._proposal_hash_from(messages)}
        return {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        seen = [
            block["toolUse"]["name"]
            for message in messages
            for block in message["content"]
            if "toolUse" in block
        ]
        next_tool = next((name for name in self.sequence if name not in seen), None)
        yield {"messageStart": {"role": "assistant"}}
        if next_tool is None:
            yield {"contentBlockStart": {"start": {}}}
            yield {
                "contentBlockDelta": {
                    "delta": {"text": "Workflow complete. The reviewed decision was recorded."}
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": next_tool,
                            "toolUseId": f"workflow-{len(seen) + 1}-{next_tool}",
                        }
                    }
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {"input": json.dumps(self._input_for(next_tool, messages))}
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
    proposal_hash: str | None,
    target_order_id: str | None,
    aws_profile: str | None,
    aws_region: str | None,
    scenario: str | None = None,
    credential_source: str = PROFILE_CREDENTIALS,
    ollama_host: str | None = None,
) -> Any:
    if provider == "deterministic":
        if model_id != "deterministic-apply-model":
            raise ValueError("Deterministic restart proof requires deterministic-apply-model")
        if not proposal_hash:
            raise ValueError("Deterministic restart proof requires a persisted proposal hash")
        return DeterministicApplyModel(proposal_hash)
    if provider == WORKFLOW_PROVIDER:
        if model_id != WORKFLOW_MODEL_ID:
            raise ValueError("Deterministic workflow requires deterministic-workflow-model")
        if not target_order_id:
            raise ValueError("Deterministic workflow requires a target order")
        if scenario not in SCENARIOS:
            raise ValueError("Deterministic workflow requires a known scenario")
        return DeterministicWorkflowModel(
            target_order_id, extraction=SCENARIOS[scenario].expected_extraction
        )
    if provider in _OLLAMA_PROVIDERS:
        return build_model(
            provider="ollama",
            model_id=model_id,
            host=ollama_host,
            aws_profile=None,
            aws_region=None,
        )
    if provider in _AWS_PROVIDERS:
        return build_model(
            provider="bedrock",
            model_id=model_id,
            host=None,
            aws_profile=aws_profile,
            aws_region=aws_region,
            credential_source=credential_source,
        )
    raise ValueError(f"Unsupported restart provider: {provider}")


def _build_agent(
    runtime_dir: Path,
    service: ShopService,
    proposal_hash: str | None,
    *,
    provider: str,
    model_id: str,
    aws_profile: str | None,
    aws_region: str | None,
    agent_id: str,
    target_order_id: str | None = None,
    scenario: str | None = None,
    credential_source: str = PROFILE_CREDENTIALS,
    ollama_host: str | None = None,
) -> Agent:
    return Agent(
        model=_configured_model(
            provider=provider,
            model_id=model_id,
            proposal_hash=proposal_hash,
            target_order_id=target_order_id,
            aws_profile=aws_profile,
            aws_region=aws_region,
            scenario=scenario,
            credential_source=credential_source,
            ollama_host=ollama_host,
        ),
        tools=build_strands_tools(service),
        hooks=[ProductionPlanApprovalHook(service, actor="restart-spike-operator")],
        session_manager=FileSessionManager(
            session_id=SESSION_ID,
            storage_dir=str(runtime_dir / "sessions"),
        ),
        agent_id=agent_id,
        system_prompt=(
            WORKFLOW_SYSTEM_PROMPT
            if provider in _WORKFLOW_PROVIDERS
            else "Apply only the supplied persisted production proposal."
        ),
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
    scenario: str = "rush-order",
    credential_source: str = PROFILE_CREDENTIALS,
    ollama_host: str | None = None,
) -> dict[str, object]:
    spec = SCENARIOS[scenario]
    target_order_id = spec.target_order_id
    runtime_dir.mkdir(parents=True, exist_ok=False)
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=utc_now)

    if provider in _WORKFLOW_PROVIDERS:
        repository.initialize(spec.build_initial())
        service = ShopService(repository, catalog=spec.catalog)
        initial_digest = repository.domain_digest()
        prompt = workflow_intake_prompt(spec)
        expected_proposal_hash = None
    else:
        repository.initialize(spec.build())
        service = ShopService(repository)
        initial_digest = repository.domain_digest()
        proposal = service.propose_schedule(target_order_id)
        expected_proposal_hash = str(proposal["content_hash"])
        prompt = start_prompt(expected_proposal_hash)

    agent_id = agent_id_for(
        provider=provider,
        model_id=model_id,
        proposal_hash=expected_proposal_hash,
        aws_profile=aws_profile,
        aws_region=aws_region,
        scenario=scenario,
    )
    agent = _build_agent(
        runtime_dir,
        service,
        expected_proposal_hash,
        provider=provider,
        model_id=model_id,
        aws_profile=aws_profile,
        aws_region=aws_region,
        agent_id=agent_id,
        target_order_id=target_order_id,
        scenario=scenario,
        credential_source=credential_source,
        ollama_host=ollama_host,
    )

    result = agent(prompt)
    interrupts = list(result.interrupts or [])
    if result.stop_reason != "interrupt" or len(interrupts) != 1:
        raise RuntimeError("Expected exactly one real Strands interrupt")
    interrupt = interrupts[0]
    reason = interrupt.reason
    if not isinstance(reason, dict) or not isinstance(reason.get("proposal_hash"), str):
        raise TypeError("Interrupt did not bind a persisted proposal hash")
    proposal_hash = reason["proposal_hash"]
    if expected_proposal_hash is not None and proposal_hash != expected_proposal_hash:
        raise RuntimeError("Interrupt did not bind the persisted proposal hash")
    if repository.load_proposal(proposal_hash) is None:
        raise RuntimeError("Interrupted proposal is not persisted")
    digest_at_interrupt = repository.domain_digest()
    intake_events = [
        event for event in repository.audit_events() if event.event_type == "request_intake"
    ]
    if provider in _WORKFLOW_PROVIDERS:
        if len(intake_events) != 1:
            raise RuntimeError("Expected exactly one intake event")
        if intake_events[0].details.get("order_id") != target_order_id:
            raise RuntimeError("Intake created an unexpected order")
        baseline_digest = str(intake_events[0].details.get("domain_digest_after") or "")
        if not baseline_digest:
            raise RuntimeError("Intake event did not record a domain digest")
    else:
        if intake_events:
            raise RuntimeError("Unexpected intake event")
        baseline_digest = initial_digest
    if digest_at_interrupt != baseline_digest:
        raise RuntimeError("Domain state changed before approval")

    checkpoint: dict[str, object] = {
        "agent_id": agent_id,
        "first_stop_reason": result.stop_reason,
        "interrupt_id": interrupt.id,
        "interrupt_name": interrupt.name,
        "proposal_hash": proposal_hash,
        "scenario": scenario,
        "target_order_id": target_order_id,
        "session_id": SESSION_ID,
        "start_process_id": os.getpid(),
        "initial_domain_digest": baseline_digest,
        "digest_at_interrupt": digest_at_interrupt,
        **_provider_configuration(
            provider=provider,
            model_id=model_id,
            aws_profile=aws_profile,
            aws_region=aws_region,
            credential_source=credential_source,
            ollama_host=ollama_host,
        ),
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    print(f"INTERRUPT_ID={interrupt.id}")
    print(f"PROPOSAL_HASH={proposal_hash}")
    return checkpoint


def _load_checkpoint(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    # Checkpoints written before credential_source existed name the only source
    # available then: a developer profile. Defaulting keeps them resumable.
    data.setdefault("credential_source", PROFILE_CREDENTIALS)
    # Checkpoints written before the local-model provider existed carry no host.
    data.setdefault("ollama_host", None)
    required = {
        "agent_id",
        "first_stop_reason",
        "interrupt_id",
        "interrupt_name",
        "proposal_hash",
        "scenario",
        "target_order_id",
        "session_id",
        "start_process_id",
        "initial_domain_digest",
        "digest_at_interrupt",
        "provider",
        "model_id",
        "aws_profile",
        "aws_region",
        "credential_source",
        "ollama_host",
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
    if data["provider"] not in {
        "deterministic",
        "bedrock",
        WORKFLOW_PROVIDER,
        BEDROCK_WORKFLOW_PROVIDER,
        OLLAMA_WORKFLOW_PROVIDER,
    }:
        raise ValueError("Checkpoint provider is invalid")
    if not isinstance(data["model_id"], str) or not data["model_id"]:
        raise ValueError("Checkpoint model identity is invalid")
    if data["scenario"] not in SCENARIOS:
        raise ValueError("Checkpoint scenario is invalid")
    if data["target_order_id"] != SCENARIOS[str(data["scenario"])].target_order_id:
        raise ValueError("Checkpoint target order does not match its scenario")
    if data["credential_source"] not in CREDENTIAL_SOURCES:
        raise ValueError("Checkpoint credential source is invalid")
    if data["provider"] in _AWS_PROVIDERS:
        if not isinstance(data["aws_region"], str) or not data["aws_region"]:
            raise ValueError("Checkpoint Bedrock configuration is incomplete")
        if data["credential_source"] == CONTAINER_ROLE_CREDENTIALS:
            if data["aws_profile"] is not None:
                raise ValueError("Checkpoint Bedrock configuration is incomplete")
        elif not isinstance(data["aws_profile"], str) or not data["aws_profile"]:
            raise ValueError("Checkpoint Bedrock configuration is incomplete")
    elif data["credential_source"] != PROFILE_CREDENTIALS:
        raise ValueError("Checkpoint credential source is invalid")
    if data["provider"] in _OLLAMA_PROVIDERS:
        if not isinstance(data["ollama_host"], str) or not data["ollama_host"]:
            raise ValueError("Checkpoint Ollama configuration is incomplete")
    elif data["ollama_host"] is not None:
        raise ValueError("Checkpoint Ollama configuration is invalid")
    expected_agent_id = agent_id_for(
        provider=str(data["provider"]),
        model_id=str(data["model_id"]),
        proposal_hash=str(data["proposal_hash"]),
        aws_profile=data["aws_profile"] if isinstance(data["aws_profile"], str) else None,
        aws_region=data["aws_region"] if isinstance(data["aws_region"], str) else None,
        scenario=str(data["scenario"]),
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
    credential_source: str = PROFILE_CREDENTIALS,
    ollama_host: str | None = None,
) -> dict[str, object]:
    checkpoint = _load_checkpoint(checkpoint_path)
    trusted_configuration = _provider_configuration(
        provider=provider,
        model_id=model_id,
        aws_profile=aws_profile,
        aws_region=aws_region,
        credential_source=credential_source,
        ollama_host=ollama_host,
    )
    persisted_configuration = {key: checkpoint[key] for key in trusted_configuration}
    if persisted_configuration != trusted_configuration:
        raise ValueError("Checkpoint does not match trusted provider configuration")
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=utc_now)
    proposal_hash = str(checkpoint["proposal_hash"])
    proposal = repository.load_proposal(proposal_hash)
    proposal_events = [
        event for event in repository.audit_events() if event.event_type == "proposal_created"
    ]
    if (
        proposal is None
        or proposal.target_order_id != checkpoint["target_order_id"]
        or len(proposal_events) != 1
        or proposal_events[0].proposal_hash != proposal_hash
        or proposal_events[0].details.get("proposal_id") != proposal.proposal_id
        or proposal_events[0].details.get("base_revision") != proposal.base_revision
        or proposal_events[0].details.get("target_order_id") != proposal.target_order_id
    ):
        raise ValueError("Checkpoint proposal does not match canonical persisted evidence")
    service = ShopService(
        repository,
        catalog=(
            SCENARIOS[str(checkpoint["scenario"])].catalog
            if provider in _WORKFLOW_PROVIDERS
            else None
        ),
    )
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
        target_order_id=str(checkpoint["target_order_id"]),
        scenario=str(checkpoint["scenario"]),
        credential_source=credential_source,
        ollama_host=ollama_host,
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
        "scenario": checkpoint["scenario"],
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
        "ollama_host": ollama_host if provider in _OLLAMA_PROVIDERS else None,
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
        "--provider",
        choices=(
            "deterministic",
            "bedrock",
            WORKFLOW_PROVIDER,
            BEDROCK_WORKFLOW_PROVIDER,
            OLLAMA_WORKFLOW_PROVIDER,
        ),
        default="deterministic",
    )
    start_parser.add_argument("--model", default="deterministic-apply-model")
    start_parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="rush-order")
    start_parser.add_argument("--aws-profile")
    start_parser.add_argument("--aws-region")
    start_parser.add_argument(
        "--ollama-host",
        default=DEFAULT_OLLAMA_HOST,
        help="Ollama server for the ollama-workflow provider; ignored otherwise.",
    )
    start_parser.add_argument(
        "--aws-credential-source",
        choices=sorted(CREDENTIAL_SOURCES),
        default=PROFILE_CREDENTIALS,
        help=(
            "Where Bedrock credentials come from: a named developer profile, or the "
            "task role a container receives on AgentCore Runtime."
        ),
    )
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--runtime-dir", type=Path, required=True)
    resume_parser.add_argument("--checkpoint", type=Path, required=True)
    resume_parser.add_argument("--decision", choices=("reject", "approve"), required=True)
    resume_parser.add_argument("--report", type=Path, required=True)
    resume_parser.add_argument(
        "--provider",
        choices=(
            "deterministic",
            "bedrock",
            WORKFLOW_PROVIDER,
            BEDROCK_WORKFLOW_PROVIDER,
            OLLAMA_WORKFLOW_PROVIDER,
        ),
        default="deterministic",
    )
    resume_parser.add_argument("--model", default="deterministic-apply-model")
    resume_parser.add_argument("--aws-profile")
    resume_parser.add_argument("--aws-region")
    resume_parser.add_argument(
        "--ollama-host",
        default=DEFAULT_OLLAMA_HOST,
        help="Must match the Ollama host recorded in the checkpoint; ignored otherwise.",
    )
    resume_parser.add_argument(
        "--aws-credential-source",
        choices=sorted(CREDENTIAL_SOURCES),
        default=PROFILE_CREDENTIALS,
        help="Must match the credential source recorded in the checkpoint.",
    )
    args = parser.parse_args()
    if args.phase == "start":
        start(
            args.runtime_dir,
            args.checkpoint,
            provider=args.provider,
            model_id=args.model,
            aws_profile=args.aws_profile,
            aws_region=args.aws_region,
            scenario=args.scenario,
            credential_source=args.aws_credential_source,
            ollama_host=args.ollama_host,
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
            credential_source=args.aws_credential_source,
            ollama_host=args.ollama_host,
        )


if __name__ == "__main__":
    main()
