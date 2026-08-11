from dataclasses import asdict
from typing import Any

from strands import tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from production_orchestrator.approval import apply_production_plan
from production_orchestrator.models import ProductionPlan
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.planning import analyze_blockers, create_production_plan


class ShopService:
    """Narrow deterministic boundary exposed to the Strands agent."""

    def __init__(self, repository: SQLiteShopRepository) -> None:
        self.repository = repository
        self._proposals: dict[str, ProductionPlan] = {}

    def list_active_orders(self) -> dict[str, object]:
        state = self.repository.load_state()
        self.repository.record_audit(
            event_type="active_orders_read",
            proposal_hash=None,
            details={"state_revision": state.revision, "order_ids": sorted(state.orders)},
        )
        return {
            "state_revision": state.revision,
            "orders": [
                asdict(state.orders[order_id])
                for order_id in sorted(state.orders)
            ],
        }

    def get_inventory(self) -> dict[str, object]:
        state = self.repository.load_state()
        self.repository.record_audit(
            event_type="inventory_read",
            proposal_hash=None,
            details={
                "state_revision": state.revision,
                "material_ids": sorted(state.inventory),
            },
        )
        return {
            "state_revision": state.revision,
            "inventory": dict(sorted(state.inventory.items())),
        }

    def get_machine_capacity(self) -> dict[str, object]:
        state = self.repository.load_state()
        self.repository.record_audit(
            event_type="machine_capacity_read",
            proposal_hash=None,
            details={
                "state_revision": state.revision,
                "machine_ids": sorted(state.machines),
                "scheduled_order_ids": sorted(entry.order_id for entry in state.schedule),
            },
        )
        return {
            "state_revision": state.revision,
            "machines": [
                asdict(state.machines[machine_id])
                for machine_id in sorted(state.machines)
            ],
            "schedule": [
                asdict(entry)
                for entry in sorted(
                    state.schedule,
                    key=lambda entry: (entry.day, entry.machine_id, entry.order_id),
                )
            ],
        }

    def analyze_shop_blockers(self, target_order_id: str) -> dict[str, object]:
        state = self.repository.load_state()
        blockers = analyze_blockers(state, target_order_id)
        self.repository.record_audit(
            event_type="blockers_analyzed",
            proposal_hash=None,
            details={
                "state_revision": state.revision,
                "target_order_id": target_order_id,
                "blocker_kinds": [blocker.kind for blocker in blockers],
            },
        )
        return {
            "state_revision": state.revision,
            "target_order_id": target_order_id,
            "blockers": [asdict(blocker) for blocker in blockers],
        }

    def propose_schedule(self, target_order_id: str) -> dict[str, object]:
        proposal = create_production_plan(self.repository.load_state(), target_order_id)
        self._proposals[proposal.content_hash] = proposal
        self.repository.record_audit(
            event_type="proposal_created",
            proposal_hash=proposal.content_hash,
            details={
                "proposal_id": proposal.proposal_id,
                "base_revision": proposal.base_revision,
                "target_order_id": proposal.target_order_id,
                "blocker_kinds": [blocker.kind for blocker in proposal.evidence],
            },
        )
        return asdict(proposal)

    def draft_communications(self, proposal_hash: str) -> dict[str, object]:
        proposal = self.get_proposal(proposal_hash)
        self.repository.record_audit(
            event_type="communications_drafted",
            proposal_hash=proposal.content_hash,
            details={
                "audiences": [draft.audience for draft in proposal.communication_drafts],
            },
        )
        return {
            "proposal_hash": proposal.content_hash,
            "drafts": [asdict(draft) for draft in proposal.communication_drafts],
        }

    def get_proposal(self, proposal_hash: str) -> ProductionPlan:
        try:
            return self._proposals[proposal_hash]
        except KeyError as error:
            raise ValueError(f"Unknown proposal hash: {proposal_hash}") from error

    def proposal_summary(self, proposal_hash: str) -> dict[str, object]:
        proposal = self.get_proposal(proposal_hash)
        return {
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.content_hash,
            "base_revision": proposal.base_revision,
            "target_order_id": proposal.target_order_id,
            "schedule_changes": [asdict(change) for change in proposal.schedule_changes],
            "procurement_actions": [
                asdict(action) for action in proposal.procurement_actions
            ],
        }

    def apply_plan(self, proposal_hash: str) -> dict[str, object]:
        proposal = self.get_proposal(proposal_hash)
        result = apply_production_plan(self.repository, proposal)
        return {
            "status": "applied",
            "proposal_hash": result.proposal_hash,
            "applied_revision": result.applied_revision,
        }


class ProductionPlanApprovalHook(HookProvider):
    def __init__(self, service: ShopService, *, actor: str) -> None:
        self.service = service
        self.actor = actor

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.approve)

    def approve(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "apply_production_plan":
            return

        proposal_hash = event.tool_use["input"].get("proposal_hash")
        if not isinstance(proposal_hash, str):
            event.cancel_tool = "Missing exact proposal hash"
            return

        try:
            summary = self.service.proposal_summary(proposal_hash)
        except ValueError:
            event.cancel_tool = "Unknown proposal hash"
            return

        response = event.interrupt(
            "production-orchestrator-apply-plan",
            reason=summary,
        )
        approved = isinstance(response, str) and response.strip().lower() in {
            "y",
            "yes",
            "approve",
            "approved",
        }
        self.service.repository.record_decision(
            proposal_hash=proposal_hash,
            reviewed_hash=proposal_hash,
            approved=approved,
            actor=self.actor,
            reason=(
                "Approved through Strands interrupt"
                if approved
                else "Rejected through Strands interrupt"
            ),
        )
        if not approved:
            event.cancel_tool = "Human rejected the production plan"


def build_strands_tools(service: ShopService) -> list[Any]:
    @tool
    def list_active_orders() -> dict[str, object]:
        """List active orders with exact priorities, due dates, requirements, and durations."""
        return service.list_active_orders()

    @tool
    def get_inventory() -> dict[str, object]:
        """Return exact synthetic material availability for the current state revision."""
        return service.get_inventory()

    @tool
    def get_machine_capacity() -> dict[str, object]:
        """Return exact machine capabilities, capacities, and scheduled commitments."""
        return service.get_machine_capacity()

    @tool
    def analyze_shop_blockers(target_order_id: str) -> dict[str, object]:
        """Deterministically identify inventory and machine-capacity blockers.

        Args:
            target_order_id: Exact order identifier to analyze.
        """
        return service.analyze_shop_blockers(target_order_id)

    @tool
    def propose_schedule(target_order_id: str) -> dict[str, object]:
        """Create a deterministic, versioned production proposal with an immutable hash.

        Args:
            target_order_id: Exact order identifier to schedule.
        """
        return service.propose_schedule(target_order_id)

    @tool
    def draft_communications(proposal_hash: str) -> dict[str, object]:
        """Return customer, operator, and supplier drafts bound to a proposal hash.

        Args:
            proposal_hash: Immutable hash returned by propose_schedule.
        """
        return service.draft_communications(proposal_hash)

    @tool
    def apply_production_plan(proposal_hash: str) -> dict[str, object]:
        """Apply the exact reviewed production plan after human approval.

        Args:
            proposal_hash: Immutable hash returned by propose_schedule.
        """
        return service.apply_plan(proposal_hash)

    return [
        list_active_orders,
        get_inventory,
        get_machine_capacity,
        analyze_shop_blockers,
        propose_schedule,
        draft_communications,
        apply_production_plan,
    ]
