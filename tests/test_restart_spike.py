import json
import subprocess
import sys
from pathlib import Path

import pytest

from production_orchestrator import restart_spike
from production_orchestrator.persistence import SQLiteShopRepository


def _run_phase(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "production_orchestrator.restart_spike", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _start(runtime_dir: Path) -> dict[str, object]:
    checkpoint = runtime_dir / "checkpoint.json"
    _run_phase("start", "--runtime-dir", str(runtime_dir), "--checkpoint", str(checkpoint))
    return json.loads(checkpoint.read_text())


def test_start_prompt_binds_exact_persisted_proposal_hash() -> None:
    proposal_hash = "a" * 64

    prompt = restart_spike.start_prompt(proposal_hash)

    assert proposal_hash in prompt
    assert "apply_production_plan" in prompt
    assert "Do not call any other tool" in prompt


@pytest.mark.parametrize(
    ("decision", "expected_revision", "expected_applied"),
    [("reject", 1, 0), ("approve", 2, 1)],
)
def test_fresh_process_resumes_real_strands_interrupt(
    tmp_path: Path,
    decision: str,
    expected_revision: int,
    expected_applied: int,
) -> None:
    runtime_dir = tmp_path / decision
    checkpoint = _start(runtime_dir)
    report = runtime_dir / "report.json"

    _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(runtime_dir / "checkpoint.json"),
        "--decision",
        decision,
        "--report",
        str(report),
    )

    result = json.loads(report.read_text())
    assert checkpoint["first_stop_reason"] == "interrupt"
    assert checkpoint["provider"] == "deterministic"
    assert checkpoint["model_id"] == "deterministic-apply-model"
    assert checkpoint["interrupt_id"] == result["interrupt_id"]
    assert checkpoint["proposal_hash"] == result["proposal_hash"]
    assert result["process_boundary_proven"] is True
    assert result["session_interrupt_restored"] is True
    assert result["official_interrupt_response_used"] is True
    assert result["provider"] == checkpoint["provider"]
    assert result["model_id"] == checkpoint["model_id"]
    assert result["final_state_revision"] == expected_revision
    assert result["plan_applied_count"] == expected_applied
    assert result["workflow_passed"] is True


@pytest.mark.parametrize(
    ("decision", "expected_revision", "expected_applied"),
    [("reject", 1, 0), ("approve", 2, 1)],
)
def test_full_workflow_provider_runs_seven_tools_and_resumes(
    tmp_path: Path,
    decision: str,
    expected_revision: int,
    expected_applied: int,
) -> None:
    runtime_dir = tmp_path / f"workflow-{decision}"
    checkpoint_path = runtime_dir / "checkpoint.json"
    _run_phase(
        "start",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--provider",
        "deterministic-workflow",
        "--model",
        "deterministic-workflow-model",
        "--scenario",
        "metallic-monogram",
    )
    checkpoint = json.loads(checkpoint_path.read_text())
    report_path = runtime_dir / "report.json"

    _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        decision,
        "--report",
        str(report_path),
        "--provider",
        "deterministic-workflow",
        "--model",
        "deterministic-workflow-model",
    )

    result = json.loads(report_path.read_text())
    assert checkpoint["scenario"] == "metallic-monogram"
    assert checkpoint["target_order_id"] == "GOLD-500"
    assert checkpoint["provider"] == "deterministic-workflow"
    assert result["scenario"] == "metallic-monogram"
    assert result["workflow_passed"] is True
    assert result["final_state_revision"] == expected_revision
    assert result["plan_applied_count"] == expected_applied
    expected_prefix = [
        "scenario_initialized",
        "request_intake",
        "active_orders_read",
        "inventory_read",
        "machine_capacity_read",
        "blockers_analyzed",
        "proposal_created",
        "communications_drafted",
    ]
    expected_events = expected_prefix + (
        ["approval_granted", "plan_applied"] if decision == "approve" else ["approval_rejected"]
    )
    assert result["audit_event_types"] == expected_events


def test_wrong_interrupt_id_fails_closed_after_restart(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "wrong-id"
    checkpoint = _start(runtime_dir)
    checkpoint["interrupt_id"] = "forged-interrupt-id"
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "approve",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "interrupt" in result.stderr.lower()


def test_altered_proposal_binding_fails_closed_after_restart(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "altered-proposal"
    checkpoint = _start(runtime_dir)
    checkpoint["proposal_hash"] = "0" * 64
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "approve",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "proposal" in result.stderr.lower()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_workflow_provider_rejects_forged_proposal_before_mutation(
    tmp_path: Path, decision: str
) -> None:
    runtime_dir = tmp_path / "workflow-forged-proposal"
    checkpoint_path = runtime_dir / "checkpoint.json"
    _run_phase(
        "start",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--provider",
        "deterministic-workflow",
        "--model",
        "deterministic-workflow-model",
        "--scenario",
        "rush-order",
    )
    checkpoint = json.loads(checkpoint_path.read_text())
    genuine_hash = str(checkpoint["proposal_hash"])
    checkpoint["proposal_hash"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        decision,
        "--report",
        str(runtime_dir / "report.json"),
        "--provider",
        "deterministic-workflow",
        "--model",
        "deterministic-workflow-model",
        check=False,
    )

    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    assert result.returncode != 0
    assert repository.load_state().revision == 1
    assert [event.event_type for event in repository.audit_events()] == [
        "scenario_initialized",
        "request_intake",
        "active_orders_read",
        "inventory_read",
        "machine_capacity_read",
        "blockers_analyzed",
        "proposal_created",
        "communications_drafted",
    ]
    assert repository.load_proposal(genuine_hash) is not None


def test_wrong_session_identity_fails_closed_after_restart(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "wrong-session"
    checkpoint = _start(runtime_dir)
    checkpoint["session_id"] = "another-session"
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "reject",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "session" in result.stderr.lower()


def test_altered_provider_binding_fails_before_model_construction(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "altered-provider"
    checkpoint = _start(runtime_dir)
    checkpoint.update(
        provider="bedrock",
        model_id="amazon.nova-lite-v1:0",
        aws_profile="must-not-be-used",
        aws_region="us-east-1",
    )
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "reject",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "agent identity" in result.stderr.lower()
    assert "must-not-be-used" not in result.stderr


def test_recomputed_agent_id_cannot_override_trusted_resume_provider(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "recomputed-provider"
    checkpoint = _start(runtime_dir)
    checkpoint.update(
        provider="bedrock",
        model_id="amazon.nova-lite-v1:0",
        aws_profile="must-not-be-used",
        aws_region="us-east-1",
    )
    checkpoint["agent_id"] = restart_spike.agent_id_for(
        provider=str(checkpoint["provider"]),
        model_id=str(checkpoint["model_id"]),
        proposal_hash=str(checkpoint["proposal_hash"]),
        aws_profile=str(checkpoint["aws_profile"]),
        aws_region=str(checkpoint["aws_region"]),
        scenario=str(checkpoint["scenario"]),
    )
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "reject",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "trusted provider configuration" in result.stderr.lower()
    assert "must-not-be-used" not in result.stderr


def test_stale_domain_state_fails_closed_after_restart(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "stale"
    checkpoint = _start(runtime_dir)
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    proposal = repository.load_proposal(str(checkpoint["proposal_hash"]))
    assert proposal is not None
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=True,
        actor="test",
        reason="Advance state before resume",
    )
    repository.apply_approved_plan(proposal)

    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(runtime_dir / "checkpoint.json"),
        "--decision",
        "approve",
        "--report",
        str(runtime_dir / "report.json"),
        check=False,
    )

    assert result.returncode != 0
    assert "domain state" in result.stderr.lower()
    assert repository.load_state().revision == 2


def test_resumed_interrupt_cannot_be_replayed(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "replay"
    _start(runtime_dir)
    args = (
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(runtime_dir / "checkpoint.json"),
        "--decision",
        "approve",
        "--report",
        str(runtime_dir / "report.json"),
    )
    _run_phase(*args)

    replay = _run_phase(*args, check=False)

    assert replay.returncode != 0
    assert "interrupt" in replay.stderr.lower()
    repository = SQLiteShopRepository(runtime_dir / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    assert [event.event_type for event in repository.audit_events()].count("plan_applied") == 1


def test_bedrock_workflow_provider_validates_configuration_offline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="profile"):
        restart_spike._configured_model(
            provider="bedrock-workflow",
            model_id="amazon.nova-lite-v1:0",
            proposal_hash=None,
            target_order_id="RUSH-200",
            aws_profile=None,
            aws_region=None,
            scenario="rush-order",
        )

    with_hash = restart_spike.agent_id_for(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        proposal_hash="a" * 64,
        aws_profile="profile",
        aws_region="us-east-1",
        scenario="rush-order",
    )
    without_hash = restart_spike.agent_id_for(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        proposal_hash=None,
        aws_profile="profile",
        aws_region="us-east-1",
        scenario="rush-order",
    )
    assert with_hash == without_hash  # workflow identity binds scenario, not hash

    runtime_dir = tmp_path / "bedrock-workflow-checkpoint"
    checkpoint = _start(runtime_dir)  # deterministic classic run supplies a real shape
    checkpoint.update(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        aws_profile=None,
        aws_region=None,
    )
    checkpoint_path = runtime_dir / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="Bedrock configuration"):
        restart_spike._load_checkpoint(checkpoint_path)
