"""Deterministic validation for customer-request intake.

The model may extract a structured request from a customer email; it may
never invent shop facts. Every extracted field is validated here against
the deterministic product catalog, and order facts (duration, materials)
are derived by catalog arithmetic — never taken from the model. Any
invalid extraction fails closed.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from production_orchestrator.models import Order

_ORDER_ID = re.compile(r"^[A-Z]+-\d+$")


class IntakeValidationError(ValueError):
    """An extracted customer request failed deterministic validation."""


@dataclass(frozen=True)
class CatalogItem:
    """Deterministic production facts for one product the shop offers."""

    product_code: str
    machine_type: str
    minutes_per_unit: int
    materials_per_unit: Mapping[str, int]


@dataclass(frozen=True)
class RequestExtraction:
    """Structured fields the model extracts from a customer email."""

    order_id: str
    product_code: str
    quantity: int
    requested_day: str
    priority: int


def validate_extraction(
    *,
    catalog: Mapping[str, CatalogItem],
    extraction: RequestExtraction,
    valid_days: Sequence[str],
) -> Order:
    """Derive an exact order from an extraction, failing closed on any doubt."""

    if _ORDER_ID.fullmatch(extraction.order_id) is None:
        raise IntakeValidationError(f"Invalid order identifier: {extraction.order_id!r}")
    item = catalog.get(extraction.product_code)
    if item is None:
        raise IntakeValidationError(f"Unknown product: {extraction.product_code!r}")
    if not isinstance(extraction.quantity, int) or extraction.quantity <= 0:
        raise IntakeValidationError(f"Invalid quantity: {extraction.quantity!r}")
    if extraction.requested_day not in valid_days:
        raise IntakeValidationError(f"Unknown requested day: {extraction.requested_day!r}")
    if not isinstance(extraction.priority, int) or not 1 <= extraction.priority <= 100:
        raise IntakeValidationError(f"Invalid priority: {extraction.priority!r}")

    duration_hours = math.ceil(extraction.quantity * item.minutes_per_unit / 60)
    materials = {
        material_id: per_unit * extraction.quantity
        for material_id, per_unit in sorted(item.materials_per_unit.items())
    }
    return Order(
        order_id=extraction.order_id,
        priority=extraction.priority,
        requested_day=extraction.requested_day,
        machine_type=item.machine_type,
        duration_hours=duration_hours,
        materials=materials,
    )
