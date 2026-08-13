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
