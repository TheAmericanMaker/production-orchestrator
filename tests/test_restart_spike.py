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


def test_provider_configuration_contract_is_shared_across_providers() -> None:
    for provider, expects_aws in (
        ("deterministic", False),
        ("deterministic-workflow", False),
        ("bedrock", True),
        ("bedrock-workflow", True),
    ):
        config = restart_spike._provider_configuration(
            provider=provider,
            model_id="model",
            aws_profile="profile",
            aws_region="region",
        )
        assert config["provider"] == provider
        assert config["aws_profile"] == ("profile" if expects_aws else None)
        assert config["aws_region"] == ("region" if expects_aws else None)


def test_provider_configuration_carries_the_credential_source() -> None:
    """AgentCore Runtime has no named profile, so the credential source is part
    of the persisted contract and is re-verified at resume."""

    profile_config = restart_spike._provider_configuration(
        provider="bedrock-workflow",
        model_id="model",
        aws_profile="profile",
        aws_region="region",
    )
    role_config = restart_spike._provider_configuration(
        provider="bedrock-workflow",
        model_id="model",
        aws_profile=None,
        aws_region="region",
        credential_source="container-role",
    )

    assert profile_config["credential_source"] == "profile"
    assert role_config["credential_source"] == "container-role"
    assert role_config["aws_profile"] is None
    assert role_config["aws_region"] == "region"
    non_aws = restart_spike._provider_configuration(
        provider="deterministic-workflow",
        model_id="model",
        aws_profile="profile",
        aws_region="region",
        credential_source="container-role",
    )
    assert non_aws["credential_source"] == "profile"


def test_agent_id_is_stable_for_the_historical_configuration_fields() -> None:
    """Agent IDs address persisted sessions, so adding a configuration field
    must never silently change them — an existing checkpoint would stop
    resuming. These values were computed before credential_source existed."""

    assert (
        restart_spike.agent_id_for(
            provider="bedrock-workflow",
            model_id="amazon.nova-lite-v1:0",
            proposal_hash="6ef62d9f",
            aws_profile="production-orchestrator-bedrock",
            aws_region="us-east-1",
            scenario="rush-order",
        )
        == "production-orchestrator-49cd82a7db381a89"
    )
    assert (
        restart_spike.agent_id_for(
            provider="deterministic-workflow",
            model_id="deterministic-workflow-model",
            proposal_hash=None,
            aws_profile=None,
            aws_region=None,
            scenario="rush-order",
        )
        == "production-orchestrator-d7f0959601b7f31e"
    )


def test_checkpoint_without_credential_source_defaults_to_profile(tmp_path: Path) -> None:
    """Checkpoints written before this field existed must still resume."""

    runtime_dir = tmp_path / "legacy-checkpoint"
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
    checkpoint.pop("credential_source", None)
    checkpoint_path.write_text(json.dumps(checkpoint))

    loaded = restart_spike._load_checkpoint(checkpoint_path)

    assert loaded["credential_source"] == "profile"


def test_non_aws_checkpoint_cannot_claim_container_role_credentials(tmp_path: Path) -> None:
    """A provider that never touches AWS has no business naming a role."""

    runtime_dir = tmp_path / "non-aws-credential-source"
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
    checkpoint["credential_source"] = "container-role"
    checkpoint_path.write_text(json.dumps(checkpoint))

    with pytest.raises(ValueError, match="credential source"):
        restart_spike._load_checkpoint(checkpoint_path)


def test_resume_refuses_a_swapped_credential_source(tmp_path: Path) -> None:
    """A checkpoint entitled to container-role credentials must not be resumed
    with profile credentials, or a run could act as an unintended identity."""

    runtime_dir = tmp_path / "swapped-credential-source"
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
    checkpoint.update(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        aws_profile=None,
        aws_region="us-east-1",
        credential_source="container-role",
    )
    checkpoint["agent_id"] = restart_spike.agent_id_for(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        proposal_hash=str(checkpoint["proposal_hash"]),
        aws_profile=None,
        aws_region="us-east-1",
        scenario=str(checkpoint["scenario"]),
    )
    checkpoint_path.write_text(json.dumps(checkpoint))

    report_path = runtime_dir / "report.json"
    result = _run_phase(
        "resume",
        "--runtime-dir",
        str(runtime_dir),
        "--checkpoint",
        str(checkpoint_path),
        "--decision",
        "reject",
        "--report",
        str(report_path),
        "--provider",
        "bedrock-workflow",
        "--model",
        "amazon.nova-lite-v1:0",
        "--aws-profile",
        "regression-test-profile",
        "--aws-region",
        "us-east-1",
        check=False,
    )

    assert result.returncode != 0
    assert "trusted provider configuration" in result.stderr.lower()
    assert not report_path.exists()


def test_bedrock_workflow_resume_accepts_its_own_checkpoint_configuration(
    tmp_path: Path,
) -> None:
    """Regression for the PR #15 drift: resume's trusted configuration used a
    literal == "bedrock" comparison while start persisted AWS fields for
    bedrock-workflow too, so a genuine bedrock-workflow checkpoint could
    never resume. The trusted-config gate must now pass; the run should fail
    later, at real AWS credential resolution for the fake profile."""
    runtime_dir = tmp_path / "bedrock-workflow-resume"
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
    checkpoint.update(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        aws_profile="regression-test-profile",
        aws_region="us-east-1",
    )
    checkpoint["agent_id"] = restart_spike.agent_id_for(
        provider="bedrock-workflow",
        model_id="amazon.nova-lite-v1:0",
        proposal_hash=str(checkpoint["proposal_hash"]),
        aws_profile="regression-test-profile",
        aws_region="us-east-1",
        scenario=str(checkpoint["scenario"]),
    )
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
        "--provider",
        "bedrock-workflow",
        "--model",
        "amazon.nova-lite-v1:0",
        "--aws-profile",
        "regression-test-profile",
        "--aws-region",
        "us-east-1",
        check=False,
    )

    assert result.returncode != 0  # fake profile cannot reach AWS
    assert "trusted provider configuration" not in result.stderr.lower()
    assert "regression-test-profile" in result.stderr
