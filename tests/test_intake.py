import pytest

from production_orchestrator.fixtures import SCENARIOS
from production_orchestrator.intake import (
    IntakeValidationError,
    RequestExtraction,
    validate_extraction,
)


def _spec(name: str):
    return SCENARIOS[name]


def test_every_scenario_carries_a_synthetic_customer_email_and_extraction() -> None:
    for spec in SCENARIOS.values():
        assert spec.customer_email.strip()
        assert "@" not in spec.customer_email  # synthetic prose, no addresses/PII
        extraction = spec.expected_extraction
        assert extraction.product_code in spec.catalog
        assert extraction.quantity > 0


def test_valid_extraction_derives_exactly_the_scenario_target_order() -> None:
    for spec in SCENARIOS.values():
        state = spec.build()

        order = validate_extraction(
            catalog=spec.catalog,
            extraction=spec.expected_extraction,
            valid_days=sorted(
                {
                    day
                    for machine in state.machines.values()
                    for day in machine.daily_capacity
                }
            ),
        )

        target = state.orders[spec.target_order_id]
        assert order.order_id == spec.target_order_id
        assert order.priority == target.priority
        assert order.requested_day == target.requested_day
        assert order.machine_type == target.machine_type
        assert order.duration_hours == target.duration_hours
        assert dict(order.materials) == dict(target.materials)


def test_unknown_product_fails_closed() -> None:
    spec = _spec("rush-order")
    bad = RequestExtraction(
        order_id=spec.expected_extraction.order_id,
        product_code="glitter-banners",
        quantity=10,
        requested_day=spec.expected_extraction.requested_day,
        priority=spec.expected_extraction.priority,
    )

    with pytest.raises(IntakeValidationError, match="product"):
        validate_extraction(
            catalog=spec.catalog, extraction=bad, valid_days=["2026-08-12", "2026-08-13"]
        )


@pytest.mark.parametrize("quantity", [0, -5])
def test_non_positive_quantity_fails_closed(quantity: int) -> None:
    spec = _spec("rush-order")
    bad = RequestExtraction(
        order_id=spec.expected_extraction.order_id,
        product_code=spec.expected_extraction.product_code,
        quantity=quantity,
        requested_day=spec.expected_extraction.requested_day,
        priority=spec.expected_extraction.priority,
    )

    with pytest.raises(IntakeValidationError, match="quantity"):
        validate_extraction(
            catalog=spec.catalog, extraction=bad, valid_days=["2026-08-12", "2026-08-13"]
        )


def test_unknown_requested_day_fails_closed() -> None:
    spec = _spec("rush-order")
    bad = RequestExtraction(
        order_id=spec.expected_extraction.order_id,
        product_code=spec.expected_extraction.product_code,
        quantity=spec.expected_extraction.quantity,
        requested_day="2031-01-01",
        priority=spec.expected_extraction.priority,
    )

    with pytest.raises(IntakeValidationError, match="day"):
        validate_extraction(
            catalog=spec.catalog, extraction=bad, valid_days=["2026-08-12", "2026-08-13"]
        )


@pytest.mark.parametrize("priority", [0, -1, 101])
def test_out_of_range_priority_fails_closed(priority: int) -> None:
    spec = _spec("rush-order")
    bad = RequestExtraction(
        order_id=spec.expected_extraction.order_id,
        product_code=spec.expected_extraction.product_code,
        quantity=spec.expected_extraction.quantity,
        requested_day=spec.expected_extraction.requested_day,
        priority=priority,
    )

    with pytest.raises(IntakeValidationError, match="priority"):
        validate_extraction(
            catalog=spec.catalog, extraction=bad, valid_days=["2026-08-12", "2026-08-13"]
        )


def test_malformed_order_id_fails_closed() -> None:
    spec = _spec("rush-order")
    bad = RequestExtraction(
        order_id="../escape; DROP TABLE",
        product_code=spec.expected_extraction.product_code,
        quantity=spec.expected_extraction.quantity,
        requested_day=spec.expected_extraction.requested_day,
        priority=spec.expected_extraction.priority,
    )

    with pytest.raises(IntakeValidationError, match="order"):
        validate_extraction(
            catalog=spec.catalog, extraction=bad, valid_days=["2026-08-12", "2026-08-13"]
        )


def test_derivation_is_deterministic_and_whole_hours() -> None:
    spec = _spec("team-jerseys")
    first = validate_extraction(
        catalog=spec.catalog,
        extraction=spec.expected_extraction,
        valid_days=["2026-08-12", "2026-08-13"],
    )
    second = validate_extraction(
        catalog=spec.catalog,
        extraction=spec.expected_extraction,
        valid_days=["2026-08-12", "2026-08-13"],
    )

    assert first == second
    assert isinstance(first.duration_hours, int)
    assert first.duration_hours >= 1


def test_initial_state_excludes_the_target_order_until_intake() -> None:
    for spec in SCENARIOS.values():
        initial = spec.build_initial()
        full = spec.build()

        assert spec.target_order_id not in initial.orders
        assert spec.target_order_id in full.orders
        assert set(initial.orders) == set(full.orders) - {spec.target_order_id}
        assert initial.schedule == full.schedule
        assert initial.revision == full.revision == 1


def test_persistence_add_order_is_atomic_and_audited(tmp_path) -> None:
    from production_orchestrator.persistence import SQLiteShopRepository

    spec = _spec("rush-order")
    repository = SQLiteShopRepository(tmp_path / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    repository.initialize(spec.build_initial())
    target = spec.build().orders[spec.target_order_id]

    repository.add_order(target)

    state = repository.load_state()
    assert state.revision == 1
    assert state.orders[spec.target_order_id] == target
    events = repository.audit_events()
    intake_events = [event for event in events if event.event_type == "request_intake"]
    assert len(intake_events) == 1
    assert intake_events[0].details["order_id"] == spec.target_order_id
    assert intake_events[0].details["domain_digest_after"] == repository.domain_digest()

    with pytest.raises(ValueError, match="exists"):
        repository.add_order(target)


def test_service_intake_tool_validates_and_creates_the_order(tmp_path) -> None:
    from dataclasses import asdict

    from production_orchestrator.persistence import SQLiteShopRepository
    from production_orchestrator.workflow import ShopService, build_strands_tools

    spec = _spec("rush-order")
    repository = SQLiteShopRepository(tmp_path / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    repository.initialize(spec.build_initial())
    service = ShopService(repository, catalog=spec.catalog)

    result = service.intake_customer_request(**asdict(spec.expected_extraction))

    assert result["order_id"] == spec.target_order_id
    state = repository.load_state()
    assert state.orders[spec.target_order_id] == spec.build().orders[spec.target_order_id]

    with pytest.raises(IntakeValidationError, match="product"):
        service.intake_customer_request(
            order_id="RUSH-201",
            product_code="unknown-product",
            quantity=5,
            requested_day="2026-08-12",
            priority=50,
        )
    assert "RUSH-201" not in repository.load_state().orders

    tool_names = {tool.tool_name for tool in build_strands_tools(service)}
    assert "intake_customer_request" in tool_names


def test_service_without_catalog_offers_no_intake_tool(tmp_path) -> None:
    from production_orchestrator.persistence import SQLiteShopRepository
    from production_orchestrator.workflow import ShopService, build_strands_tools

    spec = _spec("rush-order")
    repository = SQLiteShopRepository(tmp_path / "shop.db", clock=lambda: "2026-08-12T00:00:00Z")
    repository.initialize(spec.build())
    service = ShopService(repository)

    tool_names = {tool.tool_name for tool in build_strands_tools(service)}
    assert "intake_customer_request" not in tool_names
