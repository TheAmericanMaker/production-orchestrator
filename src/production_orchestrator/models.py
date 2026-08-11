from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    order_id: str
    priority: int
    requested_day: str
    machine_type: str
    duration_hours: int
    materials: Mapping[str, int]


@dataclass(frozen=True)
class Machine:
    machine_id: str
    machine_type: str
    daily_capacity: Mapping[str, int]


@dataclass(frozen=True)
class ScheduleEntry:
    order_id: str
    machine_id: str
    day: str
    duration_hours: int


@dataclass(frozen=True)
class ProcurementTask:
    material_id: str
    quantity: int
    proposal_hash: str
    status: str


@dataclass(frozen=True)
class ShopState:
    revision: int
    inventory: Mapping[str, int]
    orders: Mapping[str, Order]
    machines: Mapping[str, Machine]
    schedule: tuple[ScheduleEntry, ...]
    procurement_tasks: tuple[ProcurementTask, ...] = ()


@dataclass(frozen=True)
class Blocker:
    kind: str
    resource_id: str
    required: int
    available: int
    shortage: int
    related_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduleChange:
    order_id: str
    machine_id: str
    from_day: str | None
    to_day: str
    reason: str


@dataclass(frozen=True)
class ProcurementAction:
    material_id: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class CommunicationDraft:
    audience: str
    subject: str
    body: str


@dataclass(frozen=True)
class ProductionPlan:
    proposal_id: str
    content_hash: str
    base_revision: int
    target_order_id: str
    evidence: tuple[Blocker, ...]
    schedule_changes: tuple[ScheduleChange, ...]
    procurement_actions: tuple[ProcurementAction, ...]
    communication_drafts: tuple[CommunicationDraft, ...]
