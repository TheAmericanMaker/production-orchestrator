from dataclasses import dataclass

from production_orchestrator.models import ProductionPlan
from production_orchestrator.persistence import SQLiteShopRepository, StaleStateError
from production_orchestrator.planning import calculate_production_plan_hash


class ApprovalRequired(RuntimeError):
    """Raised when no human decision exists for the exact proposal."""


class ApprovalRejected(RuntimeError):
    """Raised when the human rejected the exact proposal."""


class ProposalIntegrityError(RuntimeError):
    """Raised when proposal content differs from its claimed or reviewed hash."""


class StaleProposal(RuntimeError):
    """Raised when shop state changed after proposal generation."""


@dataclass(frozen=True)
class ApprovalResult:
    proposal_hash: str
    applied_revision: int


def apply_production_plan(
    repository: SQLiteShopRepository,
    proposal: ProductionPlan,
) -> ApprovalResult:
    calculated_hash = calculate_production_plan_hash(proposal)
    expected_id = f"plan-{calculated_hash[:12]}"
    if calculated_hash != proposal.content_hash or proposal.proposal_id != expected_id:
        raise ProposalIntegrityError("Proposal content does not match its immutable identity")

    decision = repository.latest_decision(proposal.content_hash)
    if decision is None:
        raise ApprovalRequired(f"No decision for {proposal.content_hash}")
    if decision.reviewed_hash != proposal.content_hash:
        raise ProposalIntegrityError("Approval does not bind to the exact proposal hash")
    if not decision.approved:
        raise ApprovalRejected(f"Proposal {proposal.content_hash} was rejected")
    try:
        applied_revision = repository.apply_approved_plan(proposal)
    except StaleStateError as error:
        raise StaleProposal(str(error)) from error
    return ApprovalResult(
        proposal_hash=proposal.content_hash,
        applied_revision=applied_revision,
    )
