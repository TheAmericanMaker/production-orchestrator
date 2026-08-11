from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.planning import analyze_blockers, create_production_plan


def test_reports_exact_material_shortage_from_shop_facts() -> None:
    state = rush_order_scenario()

    blockers = analyze_blockers(state, target_order_id="RUSH-200")

    inventory = [blocker for blocker in blockers if blocker.kind == "inventory_shortage"]
    assert len(inventory) == 1
    assert inventory[0].resource_id == "THREAD-RED-40"
    assert inventory[0].required == 1_200
    assert inventory[0].available == 600
    assert inventory[0].shortage == 600


def test_reports_machine_capacity_conflict_and_lower_priority_work() -> None:
    state = rush_order_scenario()

    blockers = analyze_blockers(state, target_order_id="RUSH-200")

    capacity = [blocker for blocker in blockers if blocker.kind == "capacity_conflict"]
    assert len(capacity) == 1
    assert capacity[0].resource_id == "EMB-01"
    assert capacity[0].required == 10
    assert capacity[0].available == 8
    assert capacity[0].shortage == 2
    assert capacity[0].related_order_ids == ("STANDARD-100",)


def test_creates_stable_evidence_backed_production_plan() -> None:
    state = rush_order_scenario()

    first = create_production_plan(state, target_order_id="RUSH-200")
    second = create_production_plan(state, target_order_id="RUSH-200")

    assert first == second
    assert first.base_revision == 1
    assert len(first.content_hash) == 64
    assert first.proposal_id == f"plan-{first.content_hash[:12]}"

    changes = {change.order_id: change for change in first.schedule_changes}
    assert changes["RUSH-200"].from_day is None
    assert changes["RUSH-200"].to_day == "2026-08-12"
    assert changes["STANDARD-100"].from_day == "2026-08-12"
    assert changes["STANDARD-100"].to_day == "2026-08-13"

    assert len(first.procurement_actions) == 1
    assert first.procurement_actions[0].material_id == "THREAD-RED-40"
    assert first.procurement_actions[0].quantity == 600
    assert {draft.audience for draft in first.communication_drafts} == {
        "customer",
        "operator",
        "supplier",
    }
    assert {blocker.kind for blocker in first.evidence} == {
        "capacity_conflict",
        "inventory_shortage",
    }
