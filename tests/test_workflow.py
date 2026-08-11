from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.workflow import ProductionPlanApprovalHook, ShopService


class FakeBeforeToolCallEvent:
    def __init__(self, proposal_hash: str, response: str) -> None:
        self.tool_use = {
            "name": "apply_production_plan",
            "input": {"proposal_hash": proposal_hash},
        }
        self.response = response
        self.cancel_tool: bool | str = False
        self.interrupt_reason = None

    def interrupt(self, name: str, reason=None):
        assert name == "production-orchestrator-apply-plan"
        self.interrupt_reason = reason
        return self.response


def _service(tmp_path):
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    return repository, ShopService(repository)


def test_rejection_hook_binds_hash_and_preserves_domain_state(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    before = repository.domain_digest()
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "n")

    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)

    assert event.interrupt_reason["proposal_hash"] == proposal["content_hash"]
    assert event.cancel_tool == "Human rejected the production plan"
    assert repository.domain_digest() == before
    assert repository.latest_decision(proposal["content_hash"]).approved is False


def test_approval_hook_allows_exact_registered_plan_to_apply(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "y")

    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)
    result = service.apply_plan(proposal["content_hash"])

    assert event.cancel_tool is False
    assert result == {
        "proposal_hash": proposal["content_hash"],
        "applied_revision": 2,
        "status": "applied",
    }
    assert repository.load_state().revision == 2


def test_service_records_complete_evidence_to_mutation_chain(tmp_path) -> None:
    repository, service = _service(tmp_path)

    service.list_active_orders()
    service.get_inventory()
    service.get_machine_capacity()
    service.analyze_shop_blockers("RUSH-200")
    proposal = service.propose_schedule("RUSH-200")
    service.draft_communications(proposal["content_hash"])
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "y")
    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)
    service.apply_plan(proposal["content_hash"])

    events = repository.audit_events()
    assert [event.event_type for event in events] == [
        "scenario_initialized",
        "active_orders_read",
        "inventory_read",
        "machine_capacity_read",
        "blockers_analyzed",
        "proposal_created",
        "communications_drafted",
        "approval_granted",
        "plan_applied",
    ]
    assert events[4].details["blocker_kinds"] == [
        "inventory_shortage",
        "capacity_conflict",
    ]
    assert events[5].proposal_hash == proposal["content_hash"]
    assert events[6].proposal_hash == proposal["content_hash"]
    assert events[-1].proposal_hash == proposal["content_hash"]
