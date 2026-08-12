from production_orchestrator.fixtures import (
    SCENARIOS,
    metallic_monogram_scenario,
    rush_order_scenario,
    team_jerseys_scenario,
)
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


def test_scenario_catalog_builds_plannable_states() -> None:
    assert set(SCENARIOS) == {"rush-order", "team-jerseys", "metallic-monogram"}
    for spec in SCENARIOS.values():
        state = spec.build()
        assert spec.target_order_id in state.orders
        assert spec.title and spec.question and spec.summary
        plan = create_production_plan(state, target_order_id=spec.target_order_id)
        assert plan.evidence
        assert any(change.order_id == spec.target_order_id for change in plan.schedule_changes)


def test_capacity_shortage_displaces_multiple_lower_priority_orders() -> None:
    state = team_jerseys_scenario()

    blockers = analyze_blockers(state, target_order_id="JERSEY-310")
    plan = create_production_plan(state, target_order_id="JERSEY-310")

    capacity = [blocker for blocker in blockers if blocker.kind == "capacity_conflict"]
    assert len(capacity) == 1
    assert capacity[0].shortage == 4
    assert capacity[0].related_order_ids == ("CAPS-110", "TOTES-120")
    changes = {change.order_id: change for change in plan.schedule_changes}
    assert changes["CAPS-110"].to_day == "2026-08-13"
    assert changes["TOTES-120"].to_day == "2026-08-13"
    assert changes["JERSEY-310"].from_day is None
    assert changes["JERSEY-310"].to_day == "2026-08-12"
    assert plan.procurement_actions == ()


def test_metallic_scenario_yields_single_move_and_exact_procurement() -> None:
    state = metallic_monogram_scenario()

    plan = create_production_plan(state, target_order_id="GOLD-500")

    changes = {change.order_id: change for change in plan.schedule_changes}
    assert set(changes) == {"BATCH-220", "GOLD-500"}
    assert changes["BATCH-220"].from_day == "2026-08-12"
    assert changes["BATCH-220"].to_day == "2026-08-13"
    assert len(plan.procurement_actions) == 1
    assert plan.procurement_actions[0].material_id == "THREAD-GOLD-60"
    assert plan.procurement_actions[0].quantity == 1_100


def test_supplier_draft_exists_only_when_procurement_is_needed() -> None:
    with_procurement = create_production_plan(rush_order_scenario(), target_order_id="RUSH-200")
    without_procurement = create_production_plan(
        team_jerseys_scenario(), target_order_id="JERSEY-310"
    )

    assert [draft.audience for draft in with_procurement.communication_drafts] == [
        "customer",
        "operator",
        "supplier",
    ]
    assert [draft.audience for draft in without_procurement.communication_drafts] == [
        "customer",
        "operator",
    ]


def test_customer_draft_mentions_procurement_only_when_present() -> None:
    with_procurement = create_production_plan(rush_order_scenario(), target_order_id="RUSH-200")
    without_procurement = create_production_plan(
        team_jerseys_scenario(), target_order_id="JERSEY-310"
    )

    customer_with = next(
        draft for draft in with_procurement.communication_drafts if draft.audience == "customer"
    )
    customer_without = next(
        draft for draft in without_procurement.communication_drafts if draft.audience == "customer"
    )
    assert "RUSH-200" in customer_with.body
    assert "JERSEY-310" in customer_without.body
    assert "rush order" not in customer_with.body.lower()
    assert "rush order" not in customer_without.body.lower()
    assert "procurement" in customer_with.body
    assert "procurement" not in customer_without.body
