from dataclasses import dataclass

import pytest

from production_orchestrator import spike

PASSING_CHECKS = {
    "all_required_strands_tools_observed": True,
    "approval_applied_exact_plan": True,
    "factual_audit_chain_complete": True,
    "file_session_manager_persisted_state": True,
    "no_unapproved_plan_application": True,
    "read_tools_preserved_domain_state": True,
    "real_strands_interrupt": True,
    "rejection_preserved_domain_state": True,
}


@dataclass
class FakeModel:
    kwargs: dict[str, object]


class FakeSession:
    def __init__(self, *, profile_name: str, region_name: str) -> None:
        self.profile_name = profile_name
        self.region_name = region_name


class FakeAmbientSession:
    """A session built from the ambient credential chain, with no named profile."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def test_build_model_constructs_bedrock_with_named_profile_and_region() -> None:
    captured: dict[str, object] = {}

    def bedrock_model_factory(*, boto_session, **kwargs):
        captured["session"] = boto_session
        captured["kwargs"] = kwargs
        return FakeModel(kwargs)

    model = spike.build_model(
        provider="bedrock",
        model_id="amazon.nova-lite-v1:0",
        host=None,
        aws_profile="production-orchestrator-bedrock",
        aws_region="us-east-1",
        session_factory=FakeSession,
        bedrock_model_factory=bedrock_model_factory,
    )

    session = captured["session"]
    assert isinstance(session, FakeSession)
    assert session.profile_name == "production-orchestrator-bedrock"
    assert session.region_name == "us-east-1"
    assert model.kwargs == {
        "max_tokens": 4096,
        "model_id": "amazon.nova-lite-v1:0",
        "temperature": 0,
    }


def test_build_model_keeps_ollama_fallback() -> None:
    def ollama_model_factory(**kwargs):
        return FakeModel(kwargs)

    model = spike.build_model(
        provider="ollama",
        model_id="glm-5.2:cloud",
        host="http://localhost:11434",
        aws_profile=None,
        aws_region=None,
        ollama_model_factory=ollama_model_factory,
    )

    assert model.kwargs == {
        "host": "http://localhost:11434",
        "model_id": "glm-5.2:cloud",
        "temperature": 0,
    }


def test_build_model_rejects_missing_bedrock_profile() -> None:
    with pytest.raises(ValueError, match="AWS profile"):
        spike.build_model(
            provider="bedrock",
            model_id="amazon.nova-lite-v1:0",
            host=None,
            aws_profile=None,
            aws_region="us-east-1",
        )


def test_container_role_credentials_build_bedrock_without_a_named_profile() -> None:
    """AgentCore Runtime supplies task-role credentials; no named profile exists there."""

    captured: dict[str, object] = {}

    def bedrock_model_factory(*, boto_session, **kwargs):
        captured["session"] = boto_session
        return FakeModel(kwargs)

    model = spike.build_model(
        provider="bedrock",
        model_id="amazon.nova-lite-v1:0",
        host=None,
        aws_profile=None,
        aws_region="us-east-1",
        credential_source="container-role",
        session_factory=FakeAmbientSession,
        bedrock_model_factory=bedrock_model_factory,
    )

    session = captured["session"]
    assert isinstance(session, FakeAmbientSession)
    assert session.kwargs == {"region_name": "us-east-1"}
    assert model.kwargs == {
        "max_tokens": 4096,
        "model_id": "amazon.nova-lite-v1:0",
        "temperature": 0,
    }


def test_container_role_credentials_still_require_an_explicit_region() -> None:
    with pytest.raises(ValueError, match="AWS region"):
        spike.build_model(
            provider="bedrock",
            model_id="amazon.nova-lite-v1:0",
            host=None,
            aws_profile=None,
            aws_region=None,
            credential_source="container-role",
            session_factory=FakeAmbientSession,
        )


def test_container_role_credentials_refuse_a_named_profile() -> None:
    """A profile alongside role credentials is ambiguous, so it fails closed."""

    with pytest.raises(ValueError, match="profile"):
        spike.build_model(
            provider="bedrock",
            model_id="amazon.nova-lite-v1:0",
            host=None,
            aws_profile="production-orchestrator-bedrock",
            aws_region="us-east-1",
            credential_source="container-role",
            session_factory=FakeAmbientSession,
        )


def test_unknown_credential_source_fails_closed() -> None:
    with pytest.raises(ValueError, match="credential source"):
        spike.build_model(
            provider="bedrock",
            model_id="amazon.nova-lite-v1:0",
            host=None,
            aws_profile=None,
            aws_region="us-east-1",
            credential_source="metadata-service",
            session_factory=FakeAmbientSession,
        )


def test_provider_verdict_marks_successful_bedrock_path_without_claiming_pair() -> None:
    assert spike.provider_verdict("bedrock", workflow_passed=True) == {
        "bedrock_status": "executed",
        "provider": "amazon-bedrock",
        "provider_path_passed": True,
        "submission_gate_blocker": "Paired Bedrock rejection and approval evidence not evaluated",
        "submission_gate_passed": False,
        "verdict": "PARTIAL",
    }
    assert spike.provider_verdict("ollama", workflow_passed=True) == {
        "bedrock_status": "not-executed",
        "provider": "ollama-fallback",
        "provider_path_passed": False,
        "submission_gate_blocker": "Bedrock rejection and approval workflows not executed",
        "submission_gate_passed": False,
        "verdict": "PARTIAL",
    }


def test_paired_bedrock_verdict_requires_rejection_and_approval() -> None:
    rejection = {
        "aws_region": "us-east-1",
        "bedrock_status": "executed",
        "checks": PASSING_CHECKS,
        "decision": "reject",
        "model_id": "amazon.nova-lite-v1:0",
        "provider": "amazon-bedrock",
        "provider_path_passed": True,
        "workflow_passed": True,
    }
    approval = {
        "aws_region": "us-east-1",
        "bedrock_status": "executed",
        "checks": PASSING_CHECKS,
        "decision": "approve",
        "model_id": "amazon.nova-lite-v1:0",
        "provider": "amazon-bedrock",
        "provider_path_passed": True,
        "workflow_passed": True,
    }

    assert spike.paired_bedrock_verdict([rejection]) == {
        "submission_gate_blocker": "Missing passing Bedrock path: approve",
        "submission_gate_passed": False,
        "verdict": "PARTIAL",
    }
    assert spike.paired_bedrock_verdict([rejection, approval]) == {
        "submission_gate_blocker": None,
        "submission_gate_passed": True,
        "verdict": "VALIDATED",
    }


@pytest.mark.parametrize(
    ("override", "expected_blocker"),
    [
        ({"workflow_passed": False}, "Missing passing Bedrock path: approve"),
        ({"checks": {"workflow": False}}, "Missing passing Bedrock path: approve"),
        ({"checks": {"workflow": True}}, "Missing passing Bedrock path: approve"),
        ({"bedrock_status": "failed"}, "Missing passing Bedrock path: approve"),
        ({"model_id": ""}, "Missing passing Bedrock path: approve"),
        ({"aws_region": ""}, "Missing passing Bedrock path: approve"),
    ],
)
def test_paired_bedrock_verdict_rejects_unsubstantiated_path_flags(
    override: dict[str, object], expected_blocker: str
) -> None:
    rejection = {
        "aws_region": "us-east-1",
        "bedrock_status": "executed",
        "checks": PASSING_CHECKS,
        "decision": "reject",
        "model_id": "amazon.nova-lite-v1:0",
        "provider": "amazon-bedrock",
        "provider_path_passed": True,
        "workflow_passed": True,
    }
    approval = {**rejection, "decision": "approve", **override}

    result = spike.paired_bedrock_verdict([rejection, approval])

    assert result["submission_gate_passed"] is False
    assert result["submission_gate_blocker"] == expected_blocker


def test_paired_bedrock_verdict_requires_consistent_model_and_region() -> None:
    rejection = {
        "aws_region": "us-east-1",
        "bedrock_status": "executed",
        "checks": PASSING_CHECKS,
        "decision": "reject",
        "model_id": "amazon.nova-lite-v1:0",
        "provider": "amazon-bedrock",
        "provider_path_passed": True,
        "workflow_passed": True,
    }
    approval = {**rejection, "decision": "approve", "aws_region": "us-west-2"}

    assert spike.paired_bedrock_verdict([rejection, approval]) == {
        "submission_gate_blocker": "Passing Bedrock paths used inconsistent model or region",
        "submission_gate_passed": False,
        "verdict": "PARTIAL",
    }
